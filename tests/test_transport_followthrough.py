"""No provider requests or production database writes; real async SSE and slow-disk substitutes."""
import asyncio
from contextlib import contextmanager
import json
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from foundation import configuration, database as store
from research import model_transport, rotation


class Result:
    def __init__(self,row=None):self.row=row
    def fetchone(self):return self.row


class ReceiptDB:
    def __init__(self,*,slow=False,fail_checkpoint=False,fail_initial=False):
        self.calls={};self.slow=slow;self.fail_checkpoint=fail_checkpoint;self.fail_initial=fail_initial
        self.entered=threading.Event();self.release=threading.Event();self.finished=threading.Event()
        self.checkpoint_writes=0;self.lock=threading.Lock()

    @contextmanager
    def db(self,**kwargs):yield self

    def execute(self,sql,params=()):
        if sql.startswith('INSERT INTO research_calls'):
            if self.fail_initial:raise OSError('synthetic initial receipt failure')
            cid,rid,mid,started,status,data=params
            self.calls[cid]={'status':status,'data':data}
        elif sql.startswith('SELECT data FROM `research_calls`') or sql.startswith('SELECT data FROM research_calls'):
            with self.lock:
                current=self.calls.get(params[0])
                return Result(store.Row(['data'],[current['data']]) if current else None)
        elif sql.startswith('UPDATE `research_calls` SET'):
            fields=('attempt_no','recoverable','usage_state','input_tokens','output_tokens',
                    'total_tokens','cached_input_tokens','reasoning_output_tokens')
            if len(params)!=len(fields)+1:
                raise AssertionError('Unexpected typed receipt fields: '+sql)
            with self.lock:
                self.calls[params[-1]].update(zip(fields,params[:-1]))
        elif sql.startswith('UPDATE research_calls SET data='):
            self.assert_terminal_guard(sql)
            self.checkpoint_writes+=1;self.entered.set()
            if self.slow:self.release.wait(2)
            try:
                if self.fail_checkpoint:raise OSError('synthetic disk failure')
                with self.lock:
                    # Model the SQL predicate at execution, including a late writer.
                    if self.calls[params[1]]['status']=='sent':self.calls[params[1]]['data']=params[0]
            finally:self.finished.set()
        elif sql.startswith('UPDATE research_calls SET status='):
            status,finished,data,cid=params[:4]
            with self.lock:self.calls[cid].update(status=status,data=data)
        else:raise AssertionError('Unexpected SQL: '+sql)
        return Result()

    def assert_terminal_guard(self,sql):
        if "AND status='sent'" not in sql:raise AssertionError('Missing terminal guard')


class Stream(httpx.AsyncByteStream):
    def __init__(self,parts,*,delay=.002,hang=False,error=False,wait_enter=None):
        self.parts=parts;self.delay=delay;self.hang=hang;self.error=error;self.wait_enter=wait_enter
    async def __aiter__(self):
        if self.wait_enter:
            # The server can still send its terminal frame while diagnostics hit disk.
            await asyncio.to_thread(self.wait_enter.wait,.3)
        for part in self.parts:
            yield part.encode()
            await asyncio.sleep(self.delay)
        if self.error:raise httpx.ReadError('synthetic disconnected stream')
        if self.hang:await asyncio.sleep(1)


def event(value):return 'data: '+json.dumps(value,separators=(',',':'))+'\n\n'
def chunk(*,content=None,reasoning=None,finish=None,usage=None,id='chatcmpl_original'):
    delta={}
    if content is not None:delta['content']=content
    if reasoning is not None:delta['reasoning_content']=reasoning
    value={'id':id,'model':'actual-synthetic-model','choices':[{'delta':delta,'finish_reason':finish}]}
    if usage is not None:value['usage']=usage
    return value


class TransportFollowthroughTests(unittest.TestCase):
    def setUp(self):
        self.frozen={'model':{'id':6,'name':'synthetic','protocol':'openai','base_url':'https://synthetic.invalid/v1',
                            'model_identifier':'synthetic'},'config_hash':'frozen',
                     'budget':{'max_completion_tokens':None,'timeout_seconds':.15}}
        self.client_type=httpx.AsyncClient

    def invoke(self,database,stream,*,timeout=None,header='header-request-id'):
        if timeout is not None:self.frozen['budget']['timeout_seconds']=timeout
        requests=[]
        def handle(request):
            requests.append(request)
            return httpx.Response(200,headers={'content-type':'text/event-stream','x-request-id':header},stream=stream)
        transport=httpx.MockTransport(handle)
        with patch.object(store,'db',database.db),patch.object(model_transport,'_secret',return_value='synthetic-key'),\
             patch.object(httpx,'AsyncClient',side_effect=lambda **kw:self.client_type(transport=transport,**kw)):
            result=model_transport.complete('run',self.frozen,'system',{'public':'evidence'})
        return result,requests

    def test_slow_checkpoint_does_not_consume_stream_deadline_or_overwrite_terminal(self):
        database=ReceiptDB(slow=True)
        measured={'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}
        stream=Stream([event(chunk(content='{"ok":true}',finish='stop')),
                       event(chunk(usage=measured)),'data: [DONE]\n\n'],wait_enter=database.entered)
        try:
            started=time.monotonic();(text,cid),requests=self.invoke(database,stream)
            self.assertLess(time.monotonic()-started,.5)
            self.assertTrue(database.entered.is_set());self.assertFalse(database.finished.is_set())
            self.assertEqual(json.loads(text),{'ok':True});self.assertEqual(len(requests),1)
            self.assertEqual(database.calls[cid]['status'],'completed')
            final_data=database.calls[cid]['data']
            self.assertEqual(json.loads(final_data)['usage_normalized']['total'],12)
        finally:
            database.release.set();database.finished.wait(1)
        self.assertEqual(database.calls[cid]['data'],final_data)
        self.assertEqual(database.calls[cid]['status'],'completed')
        self.assertEqual(database.calls[cid]['total_tokens'],12)

    def test_header_and_first_completion_id_survive_unknown_without_private_reasoning(self):
        database=ReceiptDB()
        with self.assertRaises(model_transport.ModelFailure) as failure:
            self.invoke(database,Stream([event(chunk(reasoning='never-save-this-private-thinking'))],hang=True),
                        timeout=.05,header='header-one, header-two')
        self.assertTrue(failure.exception.unknown)
        receipt=json.loads(database.calls[failure.exception.call_id]['data'])
        self.assertEqual(receipt['provider_request_id'],'header-one, header-two')
        self.assertEqual(receipt['completion_id'],'chatcmpl_original')
        self.assertGreater(receipt['stream_progress']['reasoning_bytes'],0)
        self.assertEqual(receipt['stream_progress']['content_bytes'],0)
        self.assertIsNotNone(receipt['stream_progress']['first_event_ms'])
        self.assertNotIn('never-save-this-private-thinking',database.calls[failure.exception.call_id]['data'])

    def test_multiline_sse_data_and_eof_tail_are_one_event(self):
        database=ReceiptDB()
        value=chunk(content='{"ok":true}',finish='stop')
        serialized=json.dumps(value)
        before,after=serialized.split(', ',1)
        stream=Stream(['event: message\ndata: '+before+',\ndata: '+after+'\n\n','data: [DONE]'])
        (text,cid),_=self.invoke(database,stream)
        self.assertEqual(json.loads(text),{'ok':True})
        receipt=json.loads(database.calls[cid]['data'])
        self.assertTrue(receipt['stream_progress']['done_seen'])
        self.assertEqual(receipt['actual_model'],'actual-synthetic-model')

    def test_explicit_error_keeps_known_terminal_when_connection_later_breaks(self):
        database=ReceiptDB()
        stream=Stream([event({'error':{'message':'synthetic upstream failed'}})],error=True)
        with self.assertRaises(model_transport.ModelFailure) as failure:self.invoke(database,stream)
        self.assertFalse(failure.exception.unknown)
        self.assertEqual(failure.exception.category,'stream_error')
        self.assertEqual(database.calls[failure.exception.call_id]['status'],'failed')

    def test_explicit_error_keeps_known_terminal_when_stream_then_times_out(self):
        database=ReceiptDB()
        with self.assertRaises(model_transport.ModelFailure) as failure:
            self.invoke(database,Stream([event({'error':{'message':'synthetic upstream failed'}})],hang=True),timeout=.03)
        self.assertFalse(failure.exception.unknown)
        self.assertEqual(failure.exception.category,'stream_error')

    def test_usage_frames_are_coalesced_and_actual_terminal_usage_is_preserved(self):
        database=ReceiptDB()
        measured={'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}
        parts=[event(chunk(content='{"ok":true}'))]+[event(chunk(usage=measured)) for _ in range(80)]
        parts.extend([event(chunk(finish='stop')),'data: [DONE]\n\n'])
        (text,cid),_=self.invoke(database,Stream(parts,delay=0))
        self.assertLessEqual(database.checkpoint_writes,2)
        receipt=json.loads(database.calls[cid]['data'])
        self.assertEqual(receipt['usage_normalized']['total'],120)
        self.assertEqual(database.calls[cid]['total_tokens'],120)
        self.assertEqual(receipt['stream_progress']['received_events'],83)

    def test_initial_receipt_failure_never_dispatches_or_starts_checkpoint_writer(self):
        database=ReceiptDB(fail_initial=True)
        with patch.object(model_transport,'_CheckpointWriter') as writer,\
             patch.object(httpx,'AsyncClient') as client:
            with self.assertRaises(OSError):self.invoke(database,Stream([]))
        writer.assert_not_called();client.assert_not_called()

    def test_stream_switching_completion_identity_is_never_combined_or_signed(self):
        database=ReceiptDB()
        with self.assertRaises(model_transport.ModelFailure) as failure:
            self.invoke(database,Stream([event(chunk(content='{"ok":',id='first')),
                                         event(chunk(content='true}',finish='stop',id='second'))]))
        self.assertTrue(failure.exception.unknown)
        self.assertEqual(failure.exception.category,'stream_identity')
        self.assertEqual(json.loads(database.calls[failure.exception.call_id]['data'])['completion_id'],'first')

    def test_diagnostic_disk_failure_does_not_discard_completed_provider_output(self):
        database=ReceiptDB(fail_checkpoint=True)
        stream=Stream([event(chunk(content='{"ok":true}',finish='stop')),'data: [DONE]\n\n'],
                      wait_enter=database.finished)
        (text,cid),_=self.invoke(database,stream)
        self.assertEqual(json.loads(text),{'ok':True})
        self.assertEqual(database.calls[cid]['status'],'completed')
        self.assertGreaterEqual(json.loads(database.calls[cid]['data'])['stream_progress']['checkpoint_errors_observed'],1)


class PlanRequestTimeoutTests(unittest.TestCase):
    def configs(self,kind):
        return {'plans':[{'id':1,'name':'research','enabled':True,'model_ids':[6,11],
                           'request_timeout_seconds':600}],
                'models':[{'id':6,'name':'first','enabled':True,'timeout_seconds':180,'max_output_tokens':None},
                          {'id':11,'name':'second','enabled':True,'timeout_seconds':120,'max_output_tokens':None}],
                'agents':[]}[kind]

    def test_research_plan_timeout_overrides_each_channel_without_changing_source_model(self):
        with patch.object(configuration,'list_items',side_effect=self.configs):plan=model_transport.freeze_plan()
        self.assertEqual(plan['request_timeout_seconds'],600)
        self.assertEqual(plan['budget']['timeout_seconds'],600)
        self.assertEqual(model_transport.select_model(plan,1)['budget']['timeout_seconds'],600)
        self.assertEqual(plan['chain'][0]['model']['timeout_seconds'],180)
        self.assertEqual(plan['chain'][1]['model']['timeout_seconds'],120)
        self.assertIsNone(plan['budget']['max_requests'])

    def test_old_plan_retains_original_model_timeout(self):
        def configs(kind):
            values=self.configs(kind)
            if kind=='plans':values[0].pop('request_timeout_seconds')
            return values
        with patch.object(configuration,'list_items',side_effect=configs):plan=model_transport.freeze_plan()
        self.assertEqual(plan['budget']['timeout_seconds'],180)
        self.assertEqual(model_transport.select_model(plan,1)['budget']['timeout_seconds'],120)

    def test_plan_timeout_is_still_clipped_by_original_signing_deadline(self):
        from test_reliability_repair import ReceiptDB as RotationDB, Result as RotationResult
        class Database(RotationDB):
            def execute(self,sql,params=()):
                if sql.startswith('SELECT id,data FROM models'):
                    return RotationResult(rows=[{'id':i,'data':'{"enabled":true}'} for i in (6,11)])
                return super().execute(sql,params)
        database=Database();seen=[]
        with patch.object(configuration,'list_items',side_effect=self.configs):plan=model_transport.freeze_plan()
        plan['publish_until']='unchanged-original-deadline'
        def complete(run_id,selected,system,payload):
            seen.append(selected['budget']);return '{"ok":true}','call'
        with patch.object(store,'db',database.db),patch.object(rotation,'check'),patch.object(rotation,'save'),\
             patch.object(rotation.sessions,'remaining_seconds',return_value=120),\
             patch.object(model_transport,'complete',side_effect=complete):
            result,_=rotation.execute('run',plan,'system',{},json.loads)
        self.assertEqual(result,{'ok':True})
        self.assertEqual(seen[0]['configured_timeout_seconds'],600)
        self.assertGreater(seen[0]['timeout_seconds'],0)
        self.assertLessEqual(seen[0]['timeout_seconds'],105)


if __name__=='__main__':unittest.main()
