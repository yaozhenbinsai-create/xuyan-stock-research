"""Regression checks with HTTP substitutes and an in-memory receipt recorder.

Never opens production MySQL or issues a provider request.
"""
from contextlib import contextmanager
import copy
import json
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from foundation import configuration, collection, database as store, sources
from research import coverage, engine, history_fill, model_probe, model_transport, preparation, protocols, rotation, sessions, usage


class Result:
    def __init__(self,row=None,rows=None):self.row=row;self.rows=rows or []
    def fetchone(self):return self.row
    def fetchall(self):return self.rows
    def __iter__(self):return iter(self.rows)


class ReceiptDB:
    def __init__(self):self.calls={};self.run={'data':'{}'}
    @contextmanager
    def db(self,**kwargs):yield self
    def execute(self,sql,params=()):
        sql=sql.replace('`','')
        if sql.startswith('INSERT INTO research_calls'):
            cid,rid,mid,started,status,data=params
            self.calls[cid]={'id':cid,'run_id':rid,'model_id':mid,'started':started,'status':status,'data':data}
        elif sql.startswith('SELECT * FROM research_calls'):
            return Result(self.calls.get(params[0]))
        elif sql.startswith('SELECT data FROM research_calls'):
            row=self.calls.get(params[0])
            return Result(store.Row(['data'],[row['data']]) if row else None)
        elif sql.startswith('UPDATE research_calls SET data='):
            self.calls[params[1]]['data']=params[0]
        elif sql.startswith('UPDATE research_calls SET status=%s,finished='):
            status,finished,data,cid=params[:4]
            if len(params)==4 or self.calls[cid]['status']==params[4]:
                self.calls[cid].update(status=status,finished=finished,data=data)
        elif sql.startswith('UPDATE research_calls SET attempt_no='):
            fields=[part.split('=')[0].strip('`') for part in sql.split(' SET ',1)[1].split(' WHERE ',1)[0].split(',')]
            self.calls[params[-1]].update(zip(fields,params[:-1]))
        elif sql.startswith('SELECT data FROM research_runs'):
            return Result(store.Row(['data'],[self.run['data']]))
        elif sql.startswith('SELECT id,data FROM models'):
            return Result(rows=[{'id':i,'data':'{"enabled":true}'} for i in (1,2)])
        else:raise AssertionError('Unexpected SQL: '+sql)
        return Result()


class ProtocolTests(unittest.TestCase):
    def test_native_root_and_explicit_proxy_prefix(self):
        self.assertEqual(protocols.base_url('https://relay.invalid','responses'),'https://relay.invalid/v1')
        self.assertEqual(protocols.base_url('https://relay.invalid/proxy/v2','gemini'),'https://relay.invalid/proxy/v2')
        self.assertEqual(protocols.base_url('https://relay.invalid/v1/chat/completions','openai'),'https://relay.invalid/v1')

    def test_credentials_and_query_are_rejected_as_base_url(self):
        for address in ('https://key@relay.invalid','https://relay.invalid?key=secret','file:///tmp'):
            with self.assertRaises(ValueError):protocols.base_url(address,'responses')

    def test_gemini_native_auth_and_json_contract(self):
        model={'protocol':'gemini','base_url':'https://relay.invalid','model_identifier':'models/gemini-test'}
        url,headers,body=protocols.request(model,'test-key','system','user',123)
        self.assertEqual(url,'https://relay.invalid/v1beta/models/gemini-test:generateContent')
        self.assertNotIn('Authorization',headers)
        self.assertEqual(headers['x-goog-api-key'],'test-key')
        self.assertEqual(body['generationConfig'],{'responseMimeType':'application/json','maxOutputTokens':123})
        self.assertEqual(body['systemInstruction']['parts'][0]['text'],'system')

    def test_gemini_ignores_thoughts_and_requires_terminal(self):
        response={'candidates':[{'finishReason':'STOP','content':{'parts':[
            {'text':'private reasoning','thought':True},{'text':'{"ok":true}'}]}}]}
        self.assertEqual(json.loads(protocols.gemini_result(response)),{'ok':True})
        response['candidates'][0]['finishReason']='MAX_TOKENS'
        with self.assertRaises(ValueError):protocols.gemini_result(response)

    def test_gemini_usage_counts_thinking_once(self):
        measured=usage.normalize({'promptTokenCount':100,'candidatesTokenCount':20,'thoughtsTokenCount':5,
                                  'totalTokenCount':125,'cachedContentTokenCount':40},'gemini')
        self.assertEqual(measured,{'input':100,'output':25,'total':125,'cached_input':40,'reasoning_output':5})

    def test_receipts_drop_arbitrary_upstream_strings_and_keys(self):
        measured=protocols.numeric_usage({'input_tokens':4,'api-key-echo':19,'error':'secret',
                                         'output_tokens_details':{'reasoning_tokens':3,'echo':'secret'}})
        self.assertEqual(measured,{'input_tokens':4,'output_tokens_details':{'reasoning_tokens':3}})

    def test_xai_native_root_uses_bearer_chat(self):
        url,headers,body=protocols.request({'protocol':'xai','base_url':'https://relay.invalid',
                                          'model_identifier':'grok-test'},'test-key','s','u',None)
        self.assertEqual(url,'https://relay.invalid/v1/chat/completions')
        self.assertEqual(headers['Authorization'],'Bearer test-key')
        self.assertEqual(body['model'],'grok-test')

    def test_recovery_must_be_explicit_and_responses_only(self):
        base={'name':'test','model_identifier':'test','base_url':'https://relay.invalid'}
        with self.assertRaises(ValidationError):configuration.ModelSave(**base,recoverable_responses=True)
        model=configuration.ModelSave(**base,protocol='responses',recoverable_responses=True)
        _,_,body=protocols.request(model.model_dump(),'test-key','s','u',None)
        self.assertTrue(body['background']);self.assertTrue(body['store'])
        _,_,ordinary=protocols.request({**model.model_dump(),'recoverable_responses':False},'k','s','u',None)
        self.assertFalse(ordinary['store']);self.assertNotIn('background',ordinary)

    def test_official_account_cannot_retain_api_key(self):
        model=configuration.ModelSave(name='official',model_identifier='test',protocol='codex',
                                      api_key='should-not-survive',api_key_env='TEMP_SECRET')
        self.assertIsNone(model.api_key);self.assertEqual(model.api_key_env,'')
        self.assertEqual(model.connection_type,'account')

    def test_saved_key_cannot_follow_a_changed_connection(self):
        old={'id':1,'data':json.dumps({'name':'old','model_identifier':'model','protocol':'openai',
                                     'base_url':'https://a.invalid'}),'secret':b'encrypted-placeholder'}
        executed=[]
        class Conn:
            def execute(self,sql,params=()):executed.append(sql);return Result(old)
        @contextmanager
        def db(**kwargs):yield Conn()
        with patch.object(store,'db',db):
            with self.assertRaises(HTTPException):
                configuration.save('models',{'name':'old','model_identifier':'model',
                                             'base_url':'https://b.invalid'},1)
        self.assertFalse(any(sql.startswith('UPDATE') for sql in executed))

    def test_gemini_catalog_follows_only_same_endpoint_page_tokens(self):
        seen=[];client_type=httpx.Client
        model={'id':0,'protocol':'gemini','timeout_seconds':20,'base_url':'https://relay.invalid','model_identifier':'catalog'}
        def handler(request):
            seen.append(request)
            if len(seen)==1:return httpx.Response(200,json={'models':[{'name':'models/one'}],'nextPageToken':'next'})
            return httpx.Response(200,json={'models':[{'name':'models/two'}]})
        transport=httpx.MockTransport(handler)
        with patch.object(model_probe,'_resolve',return_value=(model,'test-secret')),\
             patch.object(httpx,'Client',side_effect=lambda **kw:client_type(transport=transport,**kw)):
            result=model_probe.catalog({})
        self.assertEqual(result['models'],['models/one','models/two']);self.assertFalse(result['more'])
        self.assertEqual(len(seen),2)
        self.assertTrue(all(r.method=='GET' and r.url.host=='relay.invalid' for r in seen))
        self.assertEqual(seen[1].url.params['pageToken'],'next')

    def test_changed_endpoint_cannot_borrow_saved_key_for_probe(self):
        saved={'name':'saved','model_identifier':'test','protocol':'openai','base_url':'https://a.invalid',
               'enabled':True,'timeout_seconds':30,'max_output_tokens':None,'api_key_env':''}
        row={'id':1,'data':json.dumps(saved),'secret':b'not-a-real-secret'}
        with patch.object(configuration,'model_row',return_value=row),patch.object(configuration,'decrypt_model_key') as decrypt:
            with self.assertRaises(HTTPException):model_probe._resolve({'model_id':1,'base_url':'https://b.invalid'})
            decrypt.assert_not_called()


class ResponseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.db=ReceiptDB();self.records=[]
        self.patch=patch.object(store,'db',self.db.db);self.patch.start();self.addCleanup(self.patch.stop)
        self.secret=patch.object(model_transport,'_secret',return_value='test-secret');self.secret.start();self.addCleanup(self.secret.stop)
        self.model={'id':1,'name':'test','protocol':'responses','base_url':'https://relay.invalid',
                    'model_identifier':'test','recoverable_responses':True}
        self.frozen={'model':self.model,'config_hash':'test-hash','budget':{'max_completion_tokens':None,'timeout_seconds':5}}
        self.async_client=httpx.AsyncClient;self.client=httpx.Client

    def clients(self,handler):
        transport=httpx.MockTransport(handler)
        return patch.object(httpx,'AsyncClient',side_effect=lambda **kw:self.async_client(transport=transport,**kw)),\
               patch.object(httpx,'Client',side_effect=lambda **kw:self.client(transport=transport,**kw))

    def completed(self,id='resp_test'):
        return {'id':id,'status':'completed','model':'provider-actual','output':[{'type':'message',
            'content':[{'type':'output_text','text':'{"ok":true}'}]}],
            'usage':{'input_tokens':10,'output_tokens':2,'total_tokens':12}}

    def test_one_generation_then_original_get_and_measured_usage(self):
        def handler(req):
            self.records.append((req.method,str(req.url)))
            if req.method=='POST':
                self.assertTrue(json.loads(req.content)['background'])
                return httpx.Response(200,json={'id':'resp_test','status':'queued'})
            self.assertTrue(str(req.url).endswith('/responses/resp_test'))
            return httpx.Response(200,json=self.completed())
        a,b=self.clients(handler)
        with a,b,patch.object(model_transport.time,'sleep'):
            text,cid=model_transport.complete('run',self.frozen,'s',{'public':'only'})
        self.assertEqual(json.loads(text),{'ok':True})
        self.assertEqual([v[0] for v in self.records],['POST','GET'])
        data=json.loads(self.db.calls[cid]['data'])
        self.assertEqual(data['usage_normalized']['total'],12)
        self.assertEqual(data['actual_model'],'provider-actual')

    def test_pending_response_preserves_handle_and_get_only_recovery(self):
        def queued(req):return httpx.Response(200,json={'id':'resp_test','status':'queued'})
        a,b=self.clients(queued)
        original_poll=model_transport._poll_response
        def expired(*args):return original_poll(*args[:-1],model_transport.time.monotonic()-10)
        with a,b,patch.object(model_transport,'_poll_response',side_effect=expired):
            with self.assertRaises(model_transport.ModelFailure) as failure:
                model_transport.complete('run',self.frozen,'s',{})
        self.assertTrue(failure.exception.unknown)
        cid=failure.exception.call_id
        self.assertEqual(json.loads(self.db.calls[cid]['data'])['response_id'],'resp_test')
        def done(req):
            self.records.append(req.method)
            self.assertEqual(req.method,'GET')
            return httpx.Response(200,json=self.completed())
        _,b=self.clients(done)
        with b:recovered=model_transport.reconcile_call(cid)
        self.assertEqual(recovered['state'],'completed')
        self.assertEqual(self.records,['GET'])
        self.assertEqual(self.db.calls[cid]['status'],'completed')

    def test_query_identity_mismatch_never_accepts_other_request(self):
        with self.client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=self.completed('resp_wrong')))) as client:
            with self.assertRaises(model_transport.ModelFailure):
                model_transport._response_get(client,'https://relay.invalid/v1','resp_expected',{})

    def test_unqueryable_legacy_call_is_retained_without_network(self):
        self.db.calls['legacy']={'id':'legacy','model_id':1,'status':'unknown','data':'{"protocol":"openai"}'}
        with patch.object(httpx,'Client') as client:
            self.assertEqual(model_transport.reconcile_call('legacy')['state'],'unqueryable')
            client.assert_not_called()
        self.assertEqual(self.db.calls['legacy']['status'],'unknown')

    def test_response_handle_rejects_paths_and_credentials(self):
        for value in ('../secrets','https://bad.invalid/x','resp_test-secret'):
            self.assertIsNone(model_transport._response_id(value,'test-secret'))

    def test_explicit_response_failure_is_not_unknown_even_without_handle(self):
        with self.assertRaises(model_transport.ModelFailure) as failure:
            model_transport._poll_response({'status':'failed'},'https://relay.invalid/v1',{},
                                           self.frozen,'call',{},'test-secret',0)
        self.assertFalse(failure.exception.unknown)
        self.assertTrue(failure.exception.retryable)

    def test_unknown_primary_stops_rotation_without_duplicate_backup(self):
        self.db.run={'data':'{}'}
        chain=[{'model':{'id':i,'name':str(i)},'config_hash':'h'} for i in (1,2)]
        plan={'chain':chain,'budget':{'max_requests':None},'model':chain[0]['model']}
        with patch.object(rotation,'check'),patch.object(rotation,'save'),\
             patch.object(rotation.sessions,'remaining_seconds',return_value=None),\
             patch.object(model_transport,'complete',side_effect=model_transport.ModelFailure('transport_unknown','unknown',unknown=True)) as complete:
            with self.assertRaises(model_transport.ModelFailure):rotation.execute('run',plan,'s',{},lambda x:x)
        self.assertEqual(complete.call_count,1)


class DataPreparationTests(unittest.TestCase):
    def test_capture_starts_inside_age_window_with_vendor_lag_margin(self):
        from datetime import datetime,timedelta
        cutoff=datetime.fromisoformat('2026-11-12T11:30:00+08:00')
        lead=sessions.capture_lead(300)
        start=cutoff-timedelta(seconds=lead)
        vendor_time=start-timedelta(seconds=25)
        self.assertEqual(start.isoformat(),'2026-11-12T11:26:00+08:00')
        self.assertLessEqual((cutoff-vendor_time).total_seconds(),300)
        self.assertEqual(sessions.capture_lead(300,True),60)
    def test_suspension_window_can_use_existing_older_valid_bars(self):
        from collections import deque
        bars=[{'day':f'2026-10-{i+1:02}','close':10,'suspended':i<10} for i in range(30)]
        history={'sh.600000':deque(bars,maxlen=65)}
        rows=[{'symbol':'sh.600000','id':i,'first_seen':'2026-11-01T00:00:00+08:00',
               'payload':json.dumps({'day':str(i),'close':10,'suspended':False})} for i in range(65,0,-1)]
        class Conn:
            def execute(self,sql,params):
                self.params=params
                assert 'first_seen<=%s' in sql
                return Result(rows=rows)
        from foundation import record_store
        with patch.object(record_store,'hydrate',side_effect=lambda conn,items:[{**r,'payload':json.loads(r['payload'])} for r in items]):
            result=history_fill.expand(Conn(),history,'2026-11-11','2026-11-12T11:30:00+08:00')
        self.assertEqual(result['recovered'],1)
        self.assertEqual(len(history['sh.600000']),65)
        self.assertEqual([b['_id'] for b in history['sh.600000']],list(range(1,66)))

    def test_new_stock_with_fewer_real_bars_remains_insufficient(self):
        from collections import deque
        bars=[{'day':'2026-11-01','close':10} for _ in range(10)]
        history={'sh.600000':deque(bars,maxlen=65)}
        class Conn:
            def execute(self,*args):return Result(rows=[])
        result=history_fill.expand(Conn(),history,'2026-11-11','2026-11-12T11:30:00+08:00')
        self.assertEqual(result['recovered'],0)
        self.assertEqual(len(history['sh.600000']),10)
        self.assertEqual(result['symbols']['sh.600000']['after'],10)

    def test_existing_risk_excerpt_does_not_hide_pending_financial_original(self):
        class Conn:
            def execute(self,*args):
                return Result(rows=[{'id':1,'payload':json.dumps({'title':'风险警示公告','text':'already read'})},
                                    {'id':2,'payload':json.dumps({'title':'年度报告'})},
                                    {'id':3,'payload':json.dumps({'title':'交易异常波动公告'})}])
        @contextmanager
        def db(**kwargs):yield Conn()
        from foundation import record_store
        with patch.object(store,'db',db),patch.object(record_store,'hydrate',side_effect=lambda conn,items:[{**r,'payload':json.loads(r['payload'])} for r in items]):
            ids=preparation._original_ids(['sh.600000'],'2026-10-01','2026-11-12')
        self.assertEqual(set(ids),{2,3})

    def test_prewarm_queues_sources_without_model_or_live_database(self):
        state={};submitted=[]
        class Conn:
            def execute(self,*args):return Result(rows=[store.Row(['symbol'],['sh.600000'])])
        @contextmanager
        def db(**kwargs):yield Conn()
        def setting(key,default=None,**kwargs):
            return False if key=='automation_paused' else state.get(key,default)
        def submit(raw,**kwargs):submitted.append(raw['source']);return {'id':raw['source']}
        with patch.object(store,'now',return_value='2026-11-12T09:00:00+08:00'),\
             patch.object(store,'db',db),patch.object(store,'setting',side_effect=setting),\
             patch.object(store,'set_setting',side_effect=lambda key,value:state.update({key:copy.deepcopy(value)})),\
             patch.object(preparation.sessions,'config',return_value={'enabled':['noon']}),\
             patch.object(preparation.sessions,'trading_day',return_value=True),\
             patch.object(collection,'submit',side_effect=submit),\
             patch.object(collection,'get_job',return_value={'status':'completed'}),\
             patch.object(preparation,'_original_ids',return_value=[]),\
             patch.object(model_transport,'complete') as model:
            preparation.warm_recent();preparation.warm_recent()
        self.assertEqual(set(submitted),{'cninfo_announcements','bs_actions','bs_profit','bs_cashflow'})
        self.assertEqual(len(submitted),4);model.assert_not_called()
        self.assertEqual(state['research_evidence_warm:2026-11-12']['phase'],'completed')

    def test_financial_report_pairs_handle_year_boundary(self):
        self.assertEqual(preparation.financial_periods('2026-11-12'),['2026Q3','2025Q3'])
        self.assertEqual(preparation.financial_periods('2026-01-03'),['2025Q4','2024Q4'])

    def test_financial_targeting_does_not_fetch_every_quarter(self):
        items=collection.plan_items({'operation':'profit'},'2025-11-12','2026-11-12',
                                    ['sh.600000','sz.000001'],financial_periods=['2026Q3','2025Q3'])
        self.assertEqual(len(items),4)
        self.assertEqual({(v['year'],v['quarter']) for v in items},{(2025,3),(2026,3)})
        self.assertEqual(items[0]['year'],2026)

    def test_shortlist_preparation_includes_profit_cashflow_and_originals(self):
        submissions=[]
        def submit(raw,**kwargs):
            submissions.append((raw,kwargs));return {'id':raw['source']}
        job={'status':'completed','done':1,'total':1,'received':1}
        with patch.object(store,'now',return_value='2026-11-12T14:36:00+08:00'),\
             patch.object(store,'setting',return_value=False),\
             patch.object(preparation.sessions,'remaining_seconds',return_value=540),\
             patch.object(preparation,'_original_ids',return_value=[1]),\
             patch.object(collection,'submit',side_effect=submit),patch.object(collection,'get_job',return_value=job):
            preparation.acquire(['sh.600000'],'2026-11-12','tail','2026-11-12T14:45:00+08:00')
        self.assertEqual([v[0]['source'] for v in submissions],
                         ['cninfo_text','cninfo_announcements','bs_actions','bs_profit','bs_cashflow'])
        self.assertEqual(submissions[3][1]['financial_periods'],['2026Q3','2025Q3'])
        self.assertEqual(submissions[0][1]['expires_at'],'2026-11-12T14:39:00+08:00')

    def test_coverage_distinguishes_risk_exclusion_from_missing_data(self):
        symbols=['sh.600000','sh.600001','sz.000001']
        directory={s:{'status':'1'} for s in symbols}
        bars={s:{'is_st':s=='sh.600001'} for s in symbols}
        summary,_=coverage.freeze(symbols,directory,bars,{'sh.600000':{'volume':10}},
                                  {'sh.600000':{}},{'sh.600000'},True,{},[])
        self.assertEqual(summary['readiness']['policy_excluded'],1)
        self.assertEqual(summary['readiness']['data_gaps']['missing_quote'],1)
        self.assertEqual(summary['readiness']['eligible'],1)

    def test_scheduled_detail_network_failure_defers_original_unit(self):
        worker=collection.Engine()
        job={'id':'test','source':'bs_basics','trigger':'schedule','schedule_id':'basic_details',
             'retries':3,'done':0,'total':1,'failed':0,'items':[{'end':'2026-11-12'}]}
        src={'id':'bs_basics','operation':'basics','dataset':'basics','provider':'bs','enabled':True,'automatic':True,'min_interval':0}
        with patch.object(collection.assets,'source',return_value=src),\
             patch.object(collection.assets,'local_now',return_value=__import__('datetime').datetime.fromisoformat('2026-11-12T16:40:00+08:00')),\
             patch.object(collection.assets,'patch_source'),patch.object(worker,'persist'),\
             patch.object(sources,'collect',side_effect=sources.SourceError('network','network')):
            worker.execute_unit(job)
        self.assertEqual(job['status'],'waiting');self.assertEqual(job['done'],0)
        self.assertEqual(job['deferred_retries'],1);self.assertEqual(job['failed'],0)


if __name__=='__main__':unittest.main()
