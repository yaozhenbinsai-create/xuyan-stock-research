from __future__ import annotations
import asyncio,copy,io,json,queue,time,unittest,uuid
from unittest.mock import patch
import httpx
from fastapi import HTTPException
import test_research as fixtures
from foundation import configuration,database as store,record_store
from research import codex_native,contracts,engine,model_transport,rotation,usage
from tools.mysql_instance import create_isolated_database,drop_isolated_database

SCHEMA='stock_test_rotation_'+uuid.uuid4().hex[:8]
def setUpModule():create_isolated_database(SCHEMA)
def tearDownModule():drop_isolated_database(SCHEMA)

class FakeOutput:
    def __init__(self):self.q=queue.Queue();self.closed=False
    def readline(self,limit):return self.q.get()
    def close(self):self.closed=True;self.q.put('')
class FakeInput:
    def __init__(self,p):self.p=p;self.closed=False
    def write(self,line):self.p.handle(json.loads(line));return len(line)
    def flush(self):pass
    def close(self):self.closed=True
class FakeProcess:
    def __init__(self,mode='completed'):
        self.mode=mode;self.stdout=FakeOutput();self.stdin=FakeInput(self);self.returncode=None;self.sent=[]
    def emit(self,value):self.stdout.q.put(json.dumps(value)+'\n')
    def handle(self,msg):
        self.sent.append(msg);method=msg.get('method');rid=msg.get('id')
        if method=='initialize':self.emit({'id':rid,'result':{}})
        if method=='thread/start':
            assert msg['params']['environments']==[] and msg['params']['sandbox']=='read-only'
            assert msg['params']['allowProviderModelFallback'] is False
            self.emit({'id':rid,'result':{'thread':{'id':'isolated-thread'}}})
        if method=='turn/start':
            self.emit({'id':rid,'result':{'turn':{'id':'isolated-turn'}}})
            event={'method':'thread/tokenUsage/updated','params':{'threadId':'isolated-thread','turnId':'isolated-turn','tokenUsage':{'total':{'inputTokens':100,'outputTokens':20,'totalTokens':120,'cachedInputTokens':60,'reasoningOutputTokens':12}}}}
            self.emit(event);self.emit(event)
            if self.mode=='completed':
                self.emit({'method':'item/completed','params':{'threadId':'isolated-thread','turnId':'isolated-turn','item':{'id':'i1','type':'agentMessage','text':'{"ok":true}','phase':'final_answer'}}})
                self.emit({'method':'turn/completed','params':{'threadId':'isolated-thread','turn':{'id':'isolated-turn','status':'completed','items':[]}}})
            if self.mode=='capacity':self.emit({'method':'error','params':{'threadId':'isolated-thread','turnId':'isolated-turn','willRetry':False,'error':{'codexErrorInfo':'serverOverloaded'}}})
        if method=='turn/interrupt':
            self.emit({'id':rid,'result':{}})
            if self.mode=='confirmed_interrupt':self.emit({'method':'turn/completed','params':{'threadId':'isolated-thread','turn':{'id':'isolated-turn','status':'interrupted'}}})
    def poll(self):return self.returncode
    def terminate(self):self.returncode=0;self.stdout.q.put('')
    def kill(self):self.terminate()
    def wait(self,timeout=None):return self.returncode

class NativeProtocolTests(unittest.TestCase):
    def test_native_final_text_usage_and_no_environment(self):
        process=FakeProcess();updates=[]
        with patch.object(codex_native.shutil,'which',return_value='isolated-cli'):
            text,tokens,remote=codex_native.run({'model_identifier':'frozen-model','reasoning_effort':'xhigh'},'system','public',2,process_factory=lambda *a,**k:process,on_update=lambda **kw:updates.append(kw))
        self.assertEqual(json.loads(text),{'ok':True});self.assertEqual(tokens['totalTokens'],120)
        self.assertEqual(usage.normalize(tokens)['total'],120)
        self.assertEqual(remote['turn_id'],'isolated-turn');self.assertEqual(process.poll(),0)
        self.assertEqual(sum(x.get('method')=='turn/start' for x in process.sent),1)
    def test_interrupt_ack_without_terminal_remains_unknown(self):
        process=FakeProcess('unconfirmed_interrupt');updates=[]
        with patch.object(codex_native.shutil,'which',return_value='isolated-cli'):
            with self.assertRaises(codex_native.NativeFailure) as e:codex_native.run({'model_identifier':'frozen'},'s','p',10,process_factory=lambda *a,**k:process,
                on_update=lambda **kw:updates.append(kw),stopped=lambda:any(u.get('usage') for u in updates),interrupt_grace=.1)
        self.assertEqual(e.exception.kind,'unknown');self.assertEqual(e.exception.usage['totalTokens'],120)
        self.assertIn('turn/interrupt',[m.get('method') for m in process.sent])
    def test_confirmed_native_interruption_is_retryable_terminal(self):
        process=FakeProcess('confirmed_interrupt');updates=[]
        with patch.object(codex_native.shutil,'which',return_value='isolated-cli'):
            with self.assertRaises(codex_native.NativeFailure) as e:codex_native.run({'model_identifier':'frozen'},'s','p',10,process_factory=lambda *a,**k:process,
                on_update=lambda **kw:updates.append(kw),stopped=lambda:any(u.get('usage') for u in updates),interrupt_grace=2)
        self.assertEqual(e.exception.kind,'transient')
    def test_usage_subsets_are_not_double_counted(self):
        v=usage.normalize({'prompt_tokens':100,'completion_tokens':40,'total_tokens':140,'prompt_tokens_details':{'cached_tokens':80},'completion_tokens_details':{'reasoning_tokens':30}})
        self.assertEqual(v,{'input':100,'output':40,'total':140,'cached_input':80,'reasoning_output':30})
        self.assertIsNone(usage.normalize(None)['total'])
        self.assertEqual(usage.normalize({'input_tokens':0,'output_tokens':0})['total'],0)
    def test_native_terminal_capacity_error_can_rotate(self):
        process=FakeProcess('capacity')
        with patch.object(codex_native.shutil,'which',return_value='isolated-cli'):
            with self.assertRaises(codex_native.NativeFailure) as e:codex_native.run({'model_identifier':'frozen'},'s','p',1,process_factory=lambda *a,**k:process)
        self.assertEqual(e.exception.kind,'transient');self.assertEqual(e.exception.usage['totalTokens'],120)
    def test_stop_during_native_preflight_sends_no_turn(self):
        process=FakeProcess()
        with patch.object(codex_native.shutil,'which',return_value='isolated-cli'):
            with self.assertRaises(codex_native.NativeFailure) as e:codex_native.run({'model_identifier':'frozen'},'s','p',1,process_factory=lambda *a,**k:process,stopped=lambda:True)
        self.assertFalse(e.exception.dispatched)
        self.assertNotIn('turn/start',[m.get('method') for m in process.sent])

class RotationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ResearchTests('test_publish_once_idempotency_and_evidence_pin')
        with patch.object(fixtures,'SCHEMA',SCHEMA):self.fixture.setUp()
        original=configuration.list_items('models')[0]
        second=configuration.save('models',{'name':'隔离官方','model_identifier':'isolated-native','protocol':'codex','timeout_seconds':500})
        third=configuration.save('models',{'name':'隔离第三通道','model_identifier':'isolated-third','protocol':'openai','base_url':'https://example.invalid/v1','api_key':'isolated-key','timeout_seconds':500})
        plan=configuration.list_items('plans')[0]
        self.ids=[original['id'],second['id'],third['id']]
        configuration.save('plans',{'name':plan['name'],'model_ids':self.ids},plan['id'])
        self.run=engine.submit(self.fixture.request());self.bundle=self.fixture.bundle()
    def tearDown(self):self.fixture.tearDown()
    def execute(self,answer):
        with patch.object(model_transport,'complete',side_effect=answer),patch.object(rotation,'pause',lambda *a:None):
            return rotation.execute(self.run['id'],self.run['data']['plan'],'system',{'frozen':True},lambda text:json.loads(text),wait=lambda *a:None)
    def test_saved_order_rotates_into_second_cycle(self):
        seen=[]
        def answer(r,p,*args):
            seen.append(p['model']['id'])
            if len(seen)<5:raise model_transport.ModelFailure('http_503','临时',retryable=True)
            return '{"ok":true}','isolated'
        value,_=self.execute(answer)
        self.assertTrue(value['ok']);self.assertEqual(seen,self.ids+self.ids[:2])
        self.assertIsNone(self.run['data']['plan']['budget']['max_requests'])
        self.assertIsNone(self.run['data']['plan']['budget']['max_completion_tokens'])
    def test_three_cycles_cool_down_and_continue_fourth_without_new_input(self):
        seen=[]
        def fail(r,p,*args):seen.append(p['model']['id']);raise model_transport.ModelFailure('http_502','临时',retryable=True)
        with self.assertRaises(rotation.RetryLater):self.execute(fail)
        self.assertEqual(seen,self.ids*3)
        after=engine.get_run(self.run['id']);self.assertEqual(after['status'],'waiting');self.assertEqual(after['data']['rotation']['cycle'],4)
        def success(r,p,*args):seen.append(p['model']['id']);return '{"ok":true}','isolated'
        self.execute(success);self.assertEqual(seen[-1],self.ids[0])
    def test_unknown_stops_before_any_backup(self):
        seen=[]
        def answer(r,p,*args):seen.append(p['model']['id']);raise model_transport.ModelFailure('network','unknown',unknown=True)
        with self.assertRaises(model_transport.ModelFailure):self.execute(answer)
        self.assertEqual(seen,self.ids[:1])

    def test_signing_deadline_preserves_publication_time_and_uses_actual_remaining_for_backups(self):
        plan = self.run['data']['plan']
        plan['publish_until'] = '2026-11-10T14:45:00+08:00'
        budgets = []
        def answer(run_id,selected,*args):
            budgets.append(selected['budget']['timeout_seconds'])
            if len(budgets) < 3:
                raise model_transport.ModelFailure('http_503','临时',retryable=True)
            return '{"ok":true}','isolated'
        with patch.object(rotation.sessions,'remaining_seconds',side_effect=[501,195,105]):
            self.execute(answer)
        self.assertEqual(budgets,[486,180,90])
        self.assertEqual(plan['budget']['timeout_seconds'],500)

    def test_stop_with_request_in_flight_preserves_unknown_barrier(self):
        def answer(rid,*args):
            engine.stop(rid)
            raise model_transport.ModelFailure('transport_unknown','远端未知',unknown=True)
        with patch.object(model_transport,'complete',side_effect=answer) as call:engine.process(self.run['id'])
        call.assert_called_once()
        self.assertEqual(engine.get_run(self.run['id'])['status'],'unknown')
        with self.assertRaises(HTTPException) as conflict:engine.submit(self.fixture.request())
        self.assertEqual(conflict.exception.status_code,409)
    def test_restart_marks_sent_request_unknown_without_losing_partial_tokens(self):
        rid=self.run['id'];stamp=store.now()
        with store.db() as c:
            c.execute("UPDATE research_runs SET status='running' WHERE id=%s",(rid,))
            c.execute("INSERT INTO research_calls(id,run_id,model_id,started,status,data) VALUES(%s,%s,%s,%s,'sent',%s)",
                ('test-restart',rid,self.ids[0],stamp,store.dumps(usage.fields({'totalTokens':120},complete=False))))
            engine.state_store.sync_call(c,'test-restart')
        engine.recover_interrupted()
        self.assertEqual(engine.get_run(rid)['status'],'unknown')
        self.assertEqual(engine.unresolved_count(),1)
        self.assertEqual(usage.calls()['items'][0]['status'],'unknown')
        self.assertEqual(usage.summary()['reported']['total'],120)
        self.assertEqual(usage.summary()['usage_partial'],1)
    def test_permanent_account_error_is_skipped_in_later_cycles(self):
        seen=[]
        def answer(r,p,*args):
            mid=p['model']['id'];seen.append(mid)
            if mid==self.ids[0]:raise model_transport.ModelFailure('http_401','鉴权')
            if len(seen)<4:raise model_transport.ModelFailure('http_503','暂时',retryable=True)
            return '{}','isolated'
        self.execute(answer);self.assertEqual(seen,[self.ids[0],self.ids[1],self.ids[2],self.ids[1]])
    def test_waiting_keeps_single_active_slot_and_can_be_stopped(self):
        def fail(*args):raise model_transport.ModelFailure('503','暂时',retryable=True)
        with self.assertRaises(rotation.RetryLater):self.execute(fail)
        with self.assertRaises(HTTPException) as conflict:engine.submit(self.fixture.request())
        self.assertEqual(conflict.exception.status_code,409)
        self.assertEqual(engine.stop(self.run['id'])['status'],'stopped')
        with self.assertRaises(rotation.Stopped):self.execute(fail)
    def test_http_stream_preserves_partial_usage_on_disconnect(self):
        plan=self.run['data']['plan'];real=httpx.AsyncClient
        class Broken(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":30,"total_tokens":130}}\n\n'
                raise httpx.ReadError('fixture disconnected')
        response=lambda req:httpx.Response(200,stream=Broken(),headers={'content-type':'text/event-stream'})
        with patch.object(model_transport.httpx,'AsyncClient',side_effect=lambda **kw:real(**kw,transport=httpx.MockTransport(response))):
            with self.assertRaises(model_transport.ModelFailure) as e:model_transport.complete(self.run['id'],plan,'s',{'x':'y'})
        self.assertTrue(e.exception.unknown)
        totals=usage.summary();self.assertEqual(totals['reported']['total'],130);self.assertEqual(totals['usage_partial'],1);self.assertFalse(totals['actual_total_known'])

    def test_large_reasoning_stream_keeps_only_the_answer_and_final_usage(self):
        real=httpx.AsyncClient
        intermediate={'id':'isolated-reply','choices':[{'delta':{'reasoning_content':'discarded'*125}}]}
        final={'choices':[{'delta':{'content':'{"ok":true}'},'finish_reason':'stop'}]}
        counters={'choices':[],'usage':{'prompt_tokens':80,'completion_tokens':500,'total_tokens':580}}
        stream=''.join('data: '+json.dumps(item)+'\n\n' for item in [intermediate]*1000+[final,counters])+'data: [DONE]\n\n'
        self.assertGreater(len(stream.encode()),1_000_000)
        response=lambda req:httpx.Response(200,text=stream,headers={'content-type':'text/event-stream'})
        with patch.object(model_transport.httpx,'AsyncClient',side_effect=lambda **kw:real(**kw,transport=httpx.MockTransport(response))):
            text,_=model_transport.complete(self.run['id'],self.run['data']['plan'],'s',{})
        self.assertEqual(json.loads(text),{'ok':True})
        self.assertTrue(usage.summary()['actual_total_known'])
        self.assertEqual(usage.summary()['reported']['total'],580)

    def test_oversized_answer_waits_for_terminal_usage_and_is_a_known_failure(self):
        real=httpx.AsyncClient
        events=[{'choices':[{'delta':{'content':'x'*(model_transport.MAX_STREAM_ANSWER_BYTES+1)},'finish_reason':'stop'}]},
                {'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':20,'total_tokens':30}}]
        stream=''.join('data: '+json.dumps(item)+'\n\n' for item in events)+'data: [DONE]\n\n'
        response=lambda req:httpx.Response(200,text=stream,headers={'content-type':'text/event-stream'})
        with patch.object(model_transport.httpx,'AsyncClient',side_effect=lambda **kw:real(**kw,transport=httpx.MockTransport(response))):
            with self.assertRaises(model_transport.ModelFailure) as failed:
                model_transport.complete(self.run['id'],self.run['data']['plan'],'s',{})
        self.assertEqual(failed.exception.category,'answer_size')
        self.assertFalse(failed.exception.unknown)
        self.assertEqual(usage.summary()['reported']['total'],30)
    def test_completed_usage_exposes_input_cache_output_reasoning_and_total(self):
        real=httpx.AsyncClient;raw={'prompt_tokens':100,'completion_tokens':40,'total_tokens':140,'prompt_tokens_details':{'cached_tokens':80},'completion_tokens_details':{'reasoning_tokens':30}}
        response=lambda req:httpx.Response(200,json={'choices':[{'message':{'content':'{}'},'finish_reason':'stop'}],'usage':raw})
        with patch.object(model_transport.httpx,'AsyncClient',side_effect=lambda **kw:real(**kw,transport=httpx.MockTransport(response))):model_transport.complete(self.run['id'],self.run['data']['plan'],'s',{})
        totals=usage.summary();self.assertEqual(totals['reported']['total'],140);self.assertEqual(totals['reported']['reasoning_output'],30)
        self.assertTrue(totals['actual_total_known']);self.assertEqual(usage.calls(limit=1)['items'][0]['model_id'],self.ids[0])
    def test_engine_resumes_fourth_cycle_with_identical_frozen_inputs(self):
        from foundation import assets
        real_execute=rotation.execute;inputs=[]
        def execute(*a,**kw):return real_execute(*a,**kw,wait=lambda *args:None)
        def fail(r,p,s,payload):inputs.append(store.dumps(payload));raise model_transport.ModelFailure('503','暂时',retryable=True)
        with patch.object(rotation,'execute',side_effect=execute),patch.object(model_transport,'complete',side_effect=fail):engine.process(self.run['id'])
        after=engine.get_run(self.run['id']);self.assertEqual(after['status'],'waiting')
        with store.db() as c:
            row=c.execute("SELECT f.id FROM heads h JOIN facts f ON f.id=h.fact_id WHERE h.dataset='daily' AND h.day=%s LIMIT 1",(self.fixture.day,)).fetchone()
            bar=record_store.load(c,row[0]);bar['close']+=.001
            data=json.loads(c.execute('SELECT data FROM research_runs WHERE id=%s',(self.run['id'],)).fetchone()[0]);data['rotation']['retry_not_before']=0
            c.execute('UPDATE research_runs SET data=%s WHERE id=%s',(store.dumps(data),self.run['id']))
            from research import state_store
            state_store.sync_run(c,self.run['id'])
        assets.publish('daily',[bar],'bs_daily_all')
        def success(r,p,s,payload):inputs.append(store.dumps(payload));return store.dumps(fixtures.model_report(payload['bundle'])),'isolated-complete'
        with patch.object(rotation,'execute',side_effect=execute),patch.object(model_transport,'complete',side_effect=success),patch.object(engine.discovery,'snapshot',side_effect=AssertionError('不应重新取数')):engine.process(self.run['id'])
        self.assertEqual(engine.get_run(self.run['id'])['status'],'completed_limited')
        self.assertEqual(len(set(inputs)),1)
    def test_explicit_stream_capacity_error_rotates_without_inventing_usage(self):
        real=httpx.AsyncClient
        response=lambda req:httpx.Response(200,text='data: {"error":{"code":"server_overloaded"}}\n\n',headers={'content-type':'text/event-stream'})
        with patch.object(model_transport.httpx,'AsyncClient',side_effect=lambda **kw:real(**kw,transport=httpx.MockTransport(response))):
            with self.assertRaises(model_transport.ModelFailure) as e:model_transport.complete(self.run['id'],self.run['data']['plan'],'s',{})
        self.assertTrue(e.exception.retryable);self.assertFalse(e.exception.unknown)
        self.assertIsNone(usage.summary()['reported']['total'])

    def test_gateway_error_event_transfers_the_same_research_to_the_first_backup(self):
        client = httpx.AsyncClient
        inputs = []
        def primary_response(req):
            inputs.append(json.loads(req.content)['messages'][1]['content'])
            return httpx.Response(200,text='event: error\ndata: {"error":{"type":"api_error","message":"fixture internal response"}}\n\n',
                                  headers={'content-type':'text/event-stream','x-request-id':'fixture-request'})
        def backup(model,system,payload,timeout,*,on_update,stopped):
            self.assertEqual(model['id'],self.ids[1])
            self.assertFalse(stopped())
            inputs.append(payload)
            on_update(dispatched=True,remote={'turn_id':'fixture-backup'})
            report = fixtures.model_report(json.loads(payload)['bundle'])
            return store.dumps(report),{'inputTokens':90,'outputTokens':10,'totalTokens':100},{'turn_id':'fixture-backup'}
        with (patch.object(model_transport.httpx,'AsyncClient',side_effect=lambda **kw:client(**kw,transport=httpx.MockTransport(primary_response))),
              patch.object(codex_native,'run',side_effect=backup)):
            engine.process(self.run['id'])
        result=engine.get_run(self.run['id'])
        self.assertEqual(result['status'],'completed_limited')
        self.assertIsNotNone(result['report'])
        self.assertEqual(len(inputs),2)
        self.assertEqual(inputs[0],inputs[1])
        calls = usage.calls(run_id=self.run['id'])['items']
        self.assertEqual({row['model_id']:row['status'] for row in calls},{self.ids[0]:'failed',self.ids[1]:'completed'})
        call = next(row for row in calls if row['model_id']==self.ids[0])
        self.assertEqual((call['status'],call['data']['provider_request_id']),('failed','fixture-request'))
        self.assertEqual(call['data']['usage_state'],'unknown')
        self.assertNotIn('fixture internal response', store.dumps(call['data']))

    def test_total_stream_deadline_keeps_an_unconfirmed_request_unknown(self):
        client = httpx.AsyncClient
        plan = self.run['data']['plan']
        plan['budget']['timeout_seconds'] = .15
        class Slow(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b': keep-alive\n\n'
                await asyncio.sleep(10)
                yield b'data: [DONE]\n\n'
        response=lambda req:httpx.Response(200,stream=Slow(),
                                          headers={'content-type':'text/event-stream','x-request-id':'fixture-pending'})
        with patch.object(model_transport.httpx,'AsyncClient',side_effect=lambda **kw:client(**kw,transport=httpx.MockTransport(response))):
            with self.assertRaises(model_transport.ModelFailure) as failure:
                model_transport.complete(self.run['id'],plan,'s',{})
        self.assertEqual(failure.exception.category,'stream_deadline')
        self.assertTrue(failure.exception.unknown)
        self.assertEqual(usage.calls(run_id=self.run['id'])['items'][0]['data']['provider_request_id'],'fixture-pending')
