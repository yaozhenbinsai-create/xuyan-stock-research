"""Recovery regressions use isolated MySQL or in-memory data and substitute providers."""
from contextlib import contextmanager
import copy
import json
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

import httpx

from foundation import database as store
from research import engine, model_probe, model_transport, protocols, state_store, usage
import test_reliability_repair as original_tests
from tools.mysql_instance import create_isolated_database, drop_isolated_database


class RecoveryRunDB:
    def __init__(self, count=12, *, checkpoint=False, blocked_id=None, latest_queryable=True):
        self.run={'id':'run','status':'unknown','stage':'unknown','created':'2026-11-13T00:00:00+08:00',
                  'updated':'2026-11-13T00:00:00+08:00','market_day':'2026-11-12','kind':'discovery','report':None}
        data={'bundle':{'input_hash':'frozen'}}
        if checkpoint:data['validated_report']={'decisions':[]}
        if blocked_id:data['unknown_call_id']=blocked_id
        self.run['data']=json.dumps(data)
        self.calls=[{'id':f'call{i:02}','run_id':'run','status':'unknown' if i==count else 'failed',
                     'data':json.dumps({'attempt':i,'recoverable':latest_queryable if i==count else True})}
                    for i in range(1,count+1)]

    @contextmanager
    def db(self,**kwargs):yield self

    def execute(self,sql,params=()):
        if sql.startswith('SELECT * FROM research_runs'):
            return original_tests.Result(row=copy.deepcopy(self.run))
        if sql.startswith('SELECT * FROM research_calls'):
            return original_tests.Result(rows=copy.deepcopy(self.calls[:10]))
        if sql.startswith('SELECT id,status,data FROM research_calls'):
            selected=next((c for c in self.calls if c['id']==params[0]),None) if 'WHERE id=' in sql else self.calls[-1]
            return original_tests.Result(row=copy.deepcopy(selected))
        raise AssertionError('Unexpected SQL: '+sql)


class RunRecoveryReviewTests(unittest.TestCase):
    def test_signed_checkpoint_reused_without_querying_provider(self):
        database=RecoveryRunDB(checkpoint=True)
        with patch.object(store,'db',database.db),patch.object(engine.ledger,'publish') as publish,\
             patch.object(model_transport,'reconcile_call') as query:
            result=engine.reconcile('run')
        self.assertEqual(result['state'],'published')
        publish.assert_called_once_with('run',{'decisions':[]})
        query.assert_not_called()

    def test_recovery_finds_attempt_beyond_public_first_ten_calls(self):
        database=RecoveryRunDB()
        with patch.object(store,'db',database.db),patch.object(engine,'_write',return_value=True) as write,\
             patch.object(model_transport,'reconcile_call',return_value={'state':'pending'}) as query:
            self.assertEqual(len(engine.get_run('run')['calls']),10)
            result=engine.reconcile('run')
        self.assertEqual(result['state'],'pending')
        query.assert_called_once_with('call12')
        self.assertNotEqual(write.call_args.kwargs.get('status'),'failed')

    def test_unqueryable_latest_attempt_does_not_use_earlier_receipt(self):
        database=RecoveryRunDB(latest_queryable=False)
        with patch.object(store,'db',database.db),patch.object(engine,'_write') as write,\
             patch.object(model_transport,'reconcile_call') as query:
            result=engine.reconcile('run')
        self.assertEqual(result['state'],'unqueryable')
        query.assert_not_called();write.assert_not_called()

    def test_recorded_blocked_identity_has_priority_over_timestamp_order(self):
        database=RecoveryRunDB(blocked_id='call11')
        with patch.object(store,'db',database.db),patch.object(engine,'_write',return_value=True),\
             patch.object(model_transport,'reconcile_call',return_value={'state':'pending'}) as query:
            engine.reconcile('run')
        query.assert_called_once_with('call11')


class RecoveryMySQLReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema='stock_test_recovery_review_'+uuid.uuid4().hex[:8]
        cls.temp=tempfile.TemporaryDirectory(prefix='stock-recovery-review-')
        cls.env=patch.dict(os.environ,{'STOCK_DATA_ROOT':cls.temp.name,
            'STOCK_MYSQL_DATABASE':cls.schema,'STOCK_DISABLE_WORKER':'1'})
        cls.env.start()
        try:create_isolated_database(cls.schema)
        except Exception:
            cls.env.stop();cls.temp.cleanup();raise

    @classmethod
    def tearDownClass(cls):
        try:drop_isolated_database(cls.schema)
        finally:cls.env.stop();cls.temp.cleanup()

    def test_same_second_attempts_recover_latest_past_ten_without_generation(self):
        run_id='isolated_run0001';stamp='2026-11-13T01:00:00+08:00'
        run_data={'bundle':{'input_hash':'frozen'}}
        with store.db() as conn:
            conn.execute('INSERT INTO research_runs(id,request_key,status,stage,created,updated,market_day,kind,data) '
                         'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                         (run_id,uuid.uuid4().hex,'unknown','unknown',stamp,stamp,'2026-11-12','discovery',
                          store.dumps(run_data)))
            state_store.sync_run(conn,run_id,run_data)
            for attempt in range(1,13):
                call_id=f'call{20-attempt:012}'
                call_data={'attempt':attempt,'recoverable':True}
                conn.execute('INSERT INTO research_calls(id,run_id,model_id,started,status,data) VALUES(%s,%s,%s,%s,%s,%s)',
                             (call_id,run_id,1,stamp,'unknown' if attempt==12 else 'failed',store.dumps(call_data)))
                state_store.sync_call(conn,call_id,call_data)
        with patch.object(model_transport,'reconcile_call',return_value={'state':'pending'}) as query,\
             patch.object(model_transport,'complete') as generate:
            result=engine.reconcile(run_id)
        self.assertEqual(result['state'],'pending')
        query.assert_called_once_with('call000000000008');generate.assert_not_called()
        current=engine.get_run(run_id)
        self.assertEqual(current['status'],'unknown')
        self.assertIn('reconcile_checked_at',current['data'])


class TransportRecoveryReviewTests(unittest.TestCase):
    def test_malformed_terminal_output_cannot_crash_recovery_scheduler(self):
        for output in (None,{},[None], [{'type':'message','content':None}],
                       [{'type':'message','content':[None,{'type':'output_text','text':None}]}]):
            with self.subTest(output=output):
                self.assertEqual(model_transport._response_text({'output':output}),'')

    def test_completed_background_response_retains_query_endpoint(self):
        fixture=original_tests.ResponseRecoveryTests(methodName='test_one_generation_then_original_get_and_measured_usage')
        fixture.setUp()
        try:
            a,b=fixture.clients(lambda request:httpx.Response(200,json=fixture.completed()))
            with a,b:
                _,call_id=model_transport.complete('run',fixture.frozen,'system',{})
            receipt=json.loads(fixture.db.calls[call_id]['data'])
            self.assertEqual(receipt['recovery_base'],'https://relay.invalid/v1')
            with a,b:
                result=model_transport.reconcile_call(call_id)
            self.assertEqual(result['state'],'completed')
        finally:fixture.doCleanups()

    def test_missing_frozen_endpoint_stays_unqueryable(self):
        database=original_tests.ReceiptDB()
        database.calls['call']={'id':'call','model_id':1,'status':'unknown','data':json.dumps({
            'protocol':'responses','recoverable':True,'response_id':'resp_test','config_hash':'hash'})}
        with patch.object(store,'db',database.db),patch.object(model_transport,'_secret',return_value='synthetic-key'),\
             patch.object(httpx,'Client') as client:
            result=model_transport.reconcile_call('call')
        self.assertEqual(result['state'],'unqueryable');client.assert_not_called()

    def test_pending_response_preserves_measured_usage(self):
        database=original_tests.ReceiptDB()
        database.calls['call']={'id':'call','model_id':1,'status':'unknown','data':json.dumps({
            'protocol':'responses','recoverable':True,'response_id':'resp_test','config_hash':'hash',
            'recovery_base':'https://relay.invalid/v1'})}
        client_type=httpx.Client
        transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'id':'resp_test','status':'in_progress',
            'usage':{'input_tokens':10,'output_tokens':3}}))
        with patch.object(store,'db',database.db),patch.object(model_transport,'_secret',return_value='synthetic-key'),\
             patch.object(httpx,'Client',side_effect=lambda **kw:client_type(transport=transport,**kw)):
            result=model_transport.reconcile_call('call')
        self.assertEqual(result['state'],'pending')
        receipt=json.loads(database.calls['call']['data'])
        self.assertEqual(receipt['usage_normalized']['input'],10)
        self.assertEqual(receipt['usage_state'],'partial')
        self.assertEqual(database.calls['call']['status'],'unknown')
        self.assertEqual(database.calls['call']['input_tokens'],10)
        self.assertEqual(database.calls['call']['output_tokens'],3)


class AnthropicEndpointReviewTests(unittest.TestCase):
    def test_existing_gateway_prefix_and_explicit_endpoints_remain_compatible(self):
        cases={
            'https://gateway.invalid':'https://gateway.invalid/v1/messages',
            'https://gateway.invalid/anthropic':'https://gateway.invalid/anthropic/v1/messages',
            'https://gateway.invalid/anthropic/v1':'https://gateway.invalid/anthropic/v1/messages',
            'https://gateway.invalid/custom/messages':'https://gateway.invalid/custom/messages',
        }
        for base,expected in cases.items():
            with self.subTest(base=base):
                url,_,_=protocols.request({'protocol':'anthropic','base_url':base,'model_identifier':'synthetic'},
                                           'synthetic-key','system','user',32)
                self.assertEqual(url,expected)

    def test_catalog_and_generation_share_gateway_prefix(self):
        seen=[];client_type=httpx.Client
        model={'protocol':'anthropic','base_url':'https://gateway.invalid/anthropic','timeout_seconds':20}
        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200,json={'data':[{'id':'synthetic'}]})
        transport=httpx.MockTransport(handler)
        with patch.object(model_probe,'_resolve',return_value=(model,'synthetic-key')),\
             patch.object(httpx,'Client',side_effect=lambda **kw:client_type(transport=transport,**kw)):
            result=model_probe.catalog({})
        self.assertEqual(result['models'],['synthetic'])
        self.assertEqual(seen,['https://gateway.invalid/anthropic/v1/models'])


class XaiUsageReviewTests(unittest.TestCase):
    def test_native_xai_chat_output_includes_separately_reported_reasoning(self):
        value=usage.normalize({'prompt_tokens':32,'completion_tokens':9,'total_tokens':135,
            'completion_tokens_details':{'reasoning_tokens':94}},'xai')
        self.assertEqual((value['input'],value['output'],value['reasoning_output'],value['total']),
                         (32,103,94,135))

    def test_normalized_relay_or_responses_usage_does_not_add_reasoning_twice(self):
        for raw in ({'prompt_tokens':32,'completion_tokens':103,'total_tokens':135,
                     'completion_tokens_details':{'reasoning_tokens':94}},
                    {'input_tokens':32,'output_tokens':103,'total_tokens':135,
                     'output_tokens_details':{'reasoning_tokens':94}}):
            self.assertEqual(usage.normalize(raw,'xai')['output'],103)

    def test_xai_missing_total_uses_measured_visible_and_reasoning_counts(self):
        raw={'prompt_tokens':32,'completion_tokens':9,'completion_tokens_details':{'reasoning_tokens':94}}
        self.assertEqual(usage.normalize(raw,'xai')['total'],135)
        self.assertEqual(usage.normalize(raw,'openai')['output'],9)


if __name__=='__main__':unittest.main()
