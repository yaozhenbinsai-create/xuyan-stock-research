"""Provider omissions and malformed output preserve measured usage, without I/O."""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import json
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from foundation import database as store
from research import model_transport, usage


TOKENS = {'input_tokens':10,'output_tokens':2,'total_tokens':12}
OUTPUT = [{'type':'message','content':[{'type':'output_text','text':'{"ok":true}'}]}]


class MemoryCalls:
    """Only the call-receipt SQL used by transport and real sync_call is accepted."""
    def __init__(self):self.rows={}

    @contextmanager
    def db(self, **options):
        yield self

    def execute(self, sql, params=()):
        cleaned=sql.replace('`','')
        row=None;count=0
        if cleaned.startswith('INSERT INTO research_calls'):
            identifier,run_id,model_id,started,status,data=params
            self.rows[identifier]={'id':identifier,'run_id':run_id,'model_id':model_id,
                                   'started':started,'finished':None,'status':status,'data':data}
            count=1
        elif cleaned.startswith('SELECT data FROM research_calls'):
            found=self.rows.get(params[0])
            if found:row=store.Row(['data'],[found['data']])
        elif cleaned.startswith('SELECT * FROM research_calls'):
            found=self.rows.get(params[0])
            if found:row=store.Row(list(found),list(found.values()))
        elif cleaned.startswith('UPDATE research_calls SET status=%s,finished=%s,data=%s'):
            status,finished,data,identifier,*condition=params
            found=self.rows[identifier]
            if not condition or found['status']==condition[0]:
                found.update(status=status,finished=finished,data=data);count=1
        elif cleaned.startswith('UPDATE research_calls SET data=%s'):
            data,identifier,*condition=params
            found=self.rows[identifier]
            if (not condition or found['status']==condition[0]) and ("status='sent'" not in cleaned or found['status']=='sent'):
                found['data']=data;count=1
        elif sql.startswith('UPDATE `research_calls` SET '):
            columns=re.findall(r'`([a-z_]+)`=%s',sql)
            self.rows[params[-1]].update(zip(columns,params[:-1]));count=1
        else:
            raise AssertionError('Unexpected SQL in no-I/O transport test: '+cleaned)
        return SimpleNamespace(fetchone=lambda:row,rowcount=count)

    def seed(self, complete=False, raw=TOKENS):
        identifier='usage_fixture'
        receipt={'protocol':'responses','recoverable':True,'response_id':'resp_usage_fixture',
                 'recovery_base':'https://usage.invalid/v1','config_hash':'frozen-config',
                 **usage.fields(raw,complete=complete)}
        self.rows[identifier]={'id':identifier,'run_id':'fixture_run','model_id':1,
                              'started':'2026-11-13T15:00:00+08:00','finished':None,
                              'status':'unknown','data':store.dumps(receipt)}
        return identifier


class QuietCheckpoint:
    def __init__(self,*args):self.errors=0
    def submit(self,data):pass
    def close(self):pass


class UsagePreservationTests(unittest.TestCase):
    def setUp(self):
        self.database=MemoryCalls()
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(store,'db',self.database.db))
        self.stack.enter_context(patch.object(model_transport,'_CheckpointWriter',QuietCheckpoint))
        self.stack.enter_context(patch.object(model_transport,'_secret',return_value='isolated-placeholder'))
        self.stack.enter_context(patch.object(model_transport.time,'sleep',return_value=None))

    def transport(self, callback):
        real,async_real=httpx.Client,httpx.AsyncClient
        transport=httpx.MockTransport(callback)
        self.stack.enter_context(patch.object(httpx,'Client',side_effect=lambda **kw:real(**kw,transport=transport)))
        self.stack.enter_context(patch.object(httpx,'AsyncClient',side_effect=lambda **kw:async_real(**kw,transport=transport)))

    def plan(self, protocol='responses'):
        return {'model':{'id':1,'protocol':protocol,'base_url':'https://usage.invalid/v1',
                         'model_identifier':'fixture-model','recoverable_responses':protocol=='responses'},
                'config_hash':'frozen-config','budget':{'timeout_seconds':10,'max_completion_tokens':64}}

    def complete(self, protocol='responses'):
        return model_transport.complete(None,self.plan(protocol),'fixture',{},probe_key='isolated-placeholder')

    def receipt(self, identifier):
        row=self.database.rows[identifier]
        return row,json.loads(row['data'])

    def test_poll_completion_without_usage_retains_measurement_as_partial(self):
        requests=[]
        def respond(request):
            requests.append(request.method)
            body={'id':'resp_usage_fixture','status':'in_progress','usage':TOKENS,'output':[]} if request.method=='POST' else {
                'id':'resp_usage_fixture','status':'completed','output':OUTPUT}
            return httpx.Response(200,json=body)
        self.transport(respond)
        text,identifier=self.complete()
        row,receipt=self.receipt(identifier)
        self.assertEqual(json.loads(text),{'ok':True})
        self.assertEqual(row['status'],'completed')
        self.assertEqual(receipt['usage_state'],'partial')
        self.assertEqual(receipt['usage_normalized']['total'],12)
        self.assertEqual((row['input_tokens'],row['output_tokens'],row['total_tokens']),(10,2,12))
        self.assertEqual(requests,['POST','GET'])

    def test_poll_failure_without_usage_does_not_promote_intermediate_measurement(self):
        requests=[]
        def respond(request):
            requests.append(request.method)
            return httpx.Response(200,json={'id':'resp_usage_fixture','status':'in_progress','usage':TOKENS} if request.method=='POST' else {
                'id':'resp_usage_fixture','status':'failed'})
        self.transport(respond)
        with self.assertRaises(model_transport.ModelFailure) as failed:self.complete()
        self.assertFalse(failed.exception.unknown)
        row,receipt=self.receipt(failed.exception.call_id)
        self.assertEqual(row['status'],'failed')
        self.assertEqual(receipt['usage_state'],'partial')
        self.assertEqual(receipt['usage_normalized']['total'],12)
        self.assertEqual(requests,['POST','GET'])

    def test_reconcile_omission_retains_partial_and_already_complete_counters(self):
        self.transport(lambda request:httpx.Response(200,json={'id':'resp_usage_fixture','status':'completed','output':OUTPUT}))
        for previously_complete in (False,True):
            with self.subTest(previously_complete=previously_complete):
                identifier=self.database.seed(complete=previously_complete)
                result=model_transport.reconcile_call(identifier)
                row,receipt=self.receipt(identifier)
                self.assertEqual(result['state'],'completed')
                self.assertEqual(receipt['usage_state'],'complete' if previously_complete else 'partial')
                self.assertEqual(receipt['usage_normalized']['total'],12)
                self.assertEqual(row['total_tokens'],12)

    def test_unknown_usage_stays_null_when_terminal_receipt_has_no_counters(self):
        self.transport(lambda request:httpx.Response(200,json={'id':'resp_usage_fixture','status':'completed','output':OUTPUT}))
        identifier=self.database.seed(raw=None)
        model_transport.reconcile_call(identifier)
        row,receipt=self.receipt(identifier)
        self.assertEqual(receipt['usage_state'],'unknown')
        self.assertIsNone(row['total_tokens'])
        self.assertTrue(all(value is None for value in receipt['usage_normalized'].values()))

    def test_malformed_choices_keeps_parsed_usage_and_unknown_outcome_barrier(self):
        requests=[]
        def respond(request):
            requests.append(request.method)
            return httpx.Response(200,json={'id':'fixture_completion','usage':TOKENS,
                'choices':[{'message':None,'finish_reason':'stop'}]})
        self.transport(respond)
        with self.assertRaises(model_transport.ModelFailure) as failed:self.complete('openai')
        self.assertTrue(failed.exception.unknown)
        self.assertEqual(failed.exception.usage,TOKENS)
        row,receipt=self.receipt(failed.exception.call_id)
        self.assertEqual(row['status'],'unknown')
        self.assertEqual(receipt['usage_state'],'partial')
        self.assertEqual(row['total_tokens'],12)
        self.assertEqual(requests,['POST'])

    def test_final_measured_zero_is_valid_and_does_not_fall_back_to_earlier_counters(self):
        def respond(request):
            return httpx.Response(200,json={'id':'resp_usage_fixture','status':'in_progress','usage':TOKENS} if request.method=='POST' else {
                'id':'resp_usage_fixture','status':'completed','output':OUTPUT,
                'usage':{'input_tokens':0,'output_tokens':0,'total_tokens':0}})
        self.transport(respond)
        _,identifier=self.complete()
        row,receipt=self.receipt(identifier)
        self.assertEqual(receipt['usage_state'],'complete')
        self.assertEqual(row['total_tokens'],0)


if __name__=='__main__':unittest.main()
