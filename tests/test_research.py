from __future__ import annotations
import copy
from datetime import date,timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
import httpx
from unittest.mock import patch
from fastapi import HTTPException
from authenticated_client import admin_client
from foundation import assets,configuration,database as store,record_store
from foundation.backups import TABLES
from research import contracts,discovery,engine,ledger,methods,model_transport
from tools.mysql_instance import create_isolated_database,drop_isolated_database

SCHEMA='stock_test_research_'+uuid.uuid4().hex[:8]
def setUpModule():create_isolated_database(SCHEMA)
def tearDownModule():drop_isolated_database(SCHEMA)

def model_report(bundle,*,action='watch',review_state='observe',stars=3):
    decisions=[]
    for c in bundle['candidates']:
        fid=c.get('bar_ids',c['evidence_ids'])[-2]
        decisions.append({'symbol':c['symbol'],'action':action,'thesis':'隔离样本：价量产生线索，等待风险与公司行动核验',
          'supports':[{'text':'已核本截点收盘与相对成交量','fact_ids':[fid]}],
          'counterarguments':[{'text':'这份价量资料不能证明风险已解除','fact_ids':[fid]}],
          'relations':[{'dimensions':['F04','F06'],'claim':'价量与结构共同形成待核线索','condition':'风险与结构复核','counterexample':'重新跌回区间','fact_ids':[fid]}],
          'dimensions_used':['F04','F06'],'entry':{'low':10,'high':11,'invalidate_below':9,'condition':'取得有效报价并完成风险核验'},
          'invalidation':'证据推翻原论点时退出观察','next_checks':['核对公告原件'],'review_state':review_state})
        if reference := c.get('outlook_reference'):
            horizon,spans = next(iter(reference['horizons'].items()))
            decisions[-1]['outlook'] = {
                'upside':{'horizon':horizon,'condition':'完整日线维持区间上沿之上',
                          'text':'需要后续量价共同确认','fact_ids':[fid],'use_history':bool(spans['upside'])},
                'downside':{'horizon':horizon,'condition':'完整日线重新跌回原区间',
                            'text':'原突破假设减弱','fact_ids':[fid],'use_history':bool(spans['downside'])},
                'chase_risk':{'text':'追高时留意回到突破位下方','watch_for':'完整日线失守原上沿','fact_ids':[fid]},
            }
        if bundle.get('stock_library_version'):
            decisions[-1]['rating'] = {'stars':stars,'text':'隔离样本评分，价量有依据，风险核验待补','fact_ids':[fid]}
            reference = c['counterparty_reference']
            if 'text' in reference:
                decisions[-1]['counterparty'] = reference
            else:
                decisions[-1]['counterparty_gap'] = reference['gap']
    return {'title':'隔离研究样本','summary':'明确观察，不冒充可买入','market_analysis':'仅隔离证券范围','decisions':decisions,'comparison':'比较同口径线索与反证','limitations':['风险资料不足']}

class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='stock-research-test-')
        self.env=patch.dict(os.environ,{'STOCK_DATA_ROOT':self.temp.name,'STOCK_MYSQL_DATABASE':SCHEMA,'STOCK_DISABLE_WORKER':'1'})
        self.env.start();discovery._cache.clear()
        with store.db(admin=True) as c:
            for table in reversed(TABLES):
                if table!='schema_versions':c.execute('DELETE FROM `'+table+'`')
        self.days=[];d=date(2026,8,3)
        while len(self.days)<70:
            if d.weekday()<5:self.days.append(d.isoformat())
            d+=timedelta(days=1)
        self.day=self.days[64]
        with patch.object(store,'now',return_value=self.day+'T15:00:00+08:00'):
            assets.publish('calendar',[{'day':d,'symbol':'*','trading':True,'markets':['sh','sz']} for d in self.days],'bs_calendar')
        basics=[{'day':self.day,'symbol':'sh.600000','name':'隔离公司','status':'1','board':'主板'}]
        with patch.object(store,'now',return_value=self.day+'T15:00:00+08:00'):
            assets.publish('basics',basics,'bs_universe')
        bars=[]
        for i,d in enumerate(self.days[:65]):
            previous=10+(i-1)*.03 if i else 10;close=10+i*.03
            if i==64:close*=1.05
            bars.append({'day':d,'symbol':'sh.600000','open':previous,'preclose':previous,'high':max(close,previous)+.01,
              'low':min(close,previous)-.01,'close':close,'volume':300 if i==64 else 100,'amount':3000 if i==64 else 1000,
              'is_st':False,'suspended':False,'adjust':'raw','volume_unit':'shares'})
        with patch.object(store,'now',return_value=self.day+'T15:00:00+08:00'):
            assets.publish('daily',bars,'bs_daily_all')
        saved=configuration.save('models',{'name':'隔离首选','model_identifier':'isolated-model','protocol':'openai','base_url':'https://example.invalid/v1','api_key':'isolated-secret','timeout_seconds':500})
        configuration.save('plans',{'name':'隔离方案','model_ids':[saved['id']]})
    def tearDown(self):self.env.stop();self.temp.cleanup()
    def bundle(self):return discovery.snapshot(self.day)
    def request(self,**overrides):return {'idempotency_key':uuid.uuid4().hex,'market_day':self.day,**overrides}
    def finish(self,raw=None,review_state='observe',stars=3):
        run=engine.submit(raw or self.request())
        def answer(rid,plan,system,payload):return store.dumps(model_report(payload['bundle'],review_state=review_state,stars=stars)),'isolated-call'
        with patch.object(model_transport,'complete',side_effect=answer) as call:engine.process(run['id'])
        return engine.get_run(run['id']),call
    def test_bounded_real_fact_discovery_and_asof_windows(self):
        b=self.bundle();self.assertEqual(b['market']['with_60_bars'],1)
        c=b['candidates'][0];self.assertIn('O03',c['paths']);self.assertEqual(len(c['dimensions']),14)
        self.assertTrue(all(x['day']<=self.day for x in c['bars']))
        self.assertEqual(c['metrics']['relative_volume20'],3)
        self.assertEqual(c['metrics']['day'],self.day)
    def test_raw_discontinuity_blocks_structure_and_negative_clusters_remain(self):
        b=self.bundle()['candidates'][0]['bars'];b=copy.deepcopy(b)
        b[-1]['preclose']=b[-2]['close']*.8
        for x in b:x.setdefault('preclose',x['open'])
        b[10]['close']=b[10]['preclose']*.95
        m=discovery.describe_bars(b)
        self.assertFalse(m['comparable']);self.assertEqual(m['paths'],[])
        self.assertGreaterEqual(m['negative_clusters'],1)
    def test_formal_action_is_blocked_and_foreign_citations_rejected(self):
        b=self.bundle();r=model_report(b,action='select')
        validated=contracts.validate_report(store.dumps(r),b)
        self.assertEqual(validated['decisions'][0]['action'],'watch')
        self.assertEqual(validated['decisions'][0]['requested_action'],'select')
        self.assertGreater(len(validated['decisions'][0]['formal_blockers']),0)
        r['decisions'][0]['supports'][0]['fact_ids']=[999999999]
        with self.assertRaises(ValueError):contracts.validate_report(store.dumps(r),b)
    def test_publish_once_idempotency_and_evidence_pin(self):
        raw=self.request();run,call=self.finish(raw)
        self.assertEqual(run['status'],'completed_limited');call.assert_called_once()
        self.assertEqual(engine.submit(raw)['id'],run['id'])
        with patch.object(model_transport,'complete') as again:engine.process(run['id'])
        again.assert_not_called()
        self.assertEqual(ledger.list_cycles()['total'],1)
        with store.db(readonly=True) as c:self.assertGreater(c.execute("SELECT count(*) FROM evidence_refs WHERE owner_type='research'").fetchone()[0],0)

    def test_frozen_structure_and_coverage_through_api(self):
        from foundation.main import app
        run,call=self.finish(self.request(rise_type='breakout'))
        model_candidate=call.call_args.args[3]['bundle']['candidates'][0]
        self.assertIn(['F04','F05','F06'],[row['dimensions'] for row in model_candidate['joint_evidence']])
        self.assertEqual(model_candidate['outlook_reference']['horizons']['D1']['samples'],60)
        self.assertTrue(model_candidate['counterparty_reference']['fact_ids'])
        self.assertIn('outlook 字段',call.call_args.args[2])
        with admin_client(app) as client:
            root='/api/v2/research/runs/'+run['id']
            detail=client.get(root).json()
            self.assertNotIn('scope_pack',detail['data']['bundle'])
            self.assertNotIn('rise_preview',detail['data']['bundle'])
            self.assertNotIn('joint_evidence',detail['data']['bundle']['candidates'][0])
            self.assertEqual(detail['report']['decisions'][0]['rise']['type'],'breakout')
            self.assertEqual(detail['report']['decisions'][0]['outlook']['downside']['horizon'],'D1')
            self.assertEqual(detail['report']['decisions'][0]['rating']['stars'],3)
            self.assertEqual(detail['report']['decisions'][0]['counterparty']['watch_for'],model_candidate['counterparty_reference']['watch_for'])
            self.assertEqual(detail['data']['bundle']['market']['rise_scope'],'breakout')
            page=client.get(root+'/coverage',params={'state':'covered','limit':1}).json()
            self.assertEqual(page['items'],[{'symbol':'sh.600000'}])
            self.assertTrue(page['available'])
            self.assertIsNone(page['next'])
            screened=client.get(root+'/screening',params={'q':'隔离公司','rise_type':'breakout','limit':1}).json()
            self.assertEqual(screened['basis'],'frozen')
            self.assertEqual([row['symbol'] for row in screened['items']],['sh.600000'])
            self.assertEqual(screened['items'][0]['selected'],1)
            self.assertEqual(screened['items'][0]['rise_type'],'breakout')
            self.assertEqual(client.get(root+'/screening',params={'rise_type':'setup'}).json()['items'],[])
            preview=client.get('/api/v2/opportunities/rises',params={'day':self.day,'rise_type':'breakout','limit':1}).json()
            self.assertEqual(preview['items'][0]['signal']['type'],'breakout')
            self.assertEqual(preview['counts']['breakout'],1)
            self.assertIsNone(preview['next'])
            assets.publish('basics',[{'day':self.day,'symbol':'sz.000001','name':'行情对照','status':'1','board':'主板'}],'bs_universe')
            assets.publish('daily',[{'day':self.day,'symbol':'sz.000001','preclose':10,'close':9.9,'open':10,
                                     'high':10.1,'low':9.8,'volume':100,'amount':990}],'bs_daily_all')
            analysis=client.get('/api/v2/opportunities/sh.600000/analysis',params={'day':self.day}).json()
            self.assertEqual(analysis['rise']['type'],'breakout')
            self.assertEqual(analysis['outlook']['horizons']['D1']['samples'],60)
            self.assertEqual(analysis['outlook_source'],'local')
            self.assertEqual(analysis['counterparty_reference'],model_candidate['counterparty_reference'])
            self.assertEqual(len(analysis['analysis']['items']),14)
            self.assertEqual(len(analysis['analysis']['combinations']),6)
            dimensions={row['id']:row for row in analysis['analysis']['items']}
            self.assertEqual(dimensions['F07']['state'],'missing')
            self.assertEqual(dimensions['F07']['readings'],[])
            self.assertEqual(next(r['value'] for r in dimensions['F04']['readings'] if r['label']=='相对量'),3)
            self.assertEqual(next(r['value'] for r in dimensions['F01']['readings'] if r['label']=='有效市场样本'),2)
            cycles=client.get('/api/v2/cycles',params={'rise_type':'breakout','limit':1}).json()
            self.assertEqual(cycles['items'][0]['data']['rise']['type'],'breakout')
            self.assertEqual(client.get('/api/v2/cycles',params={'rise_type':'early'}).json()['total'],0)
            performance=client.get('/api/v2/performance',params={'rise_type':'breakout'}).json()
            self.assertEqual(performance['counts']['mentions'],1)
            self.assertEqual(performance['counts']['cycles'],1)
            self.assertTrue(all(group['hit_rate'] is None for group in performance['groups']))
            replay=client.get(root+'/stocks/sh.600000/structure').json()
            self.assertEqual(len(replay['bars']),65)
            self.assertEqual(replay['replay']['frames'][0]['as_of'][:10],self.days[0])
            self.assertEqual(replay['replay']['frames'][-1]['as_of'][:10],self.day)

    def test_stock_library_pages_counts_and_hides_withdrawal(self):
        from foundation.main import app
        with patch.object(store,'now',return_value=self.day+'T18:00:00+08:00'):
            first,_=self.finish(stars=3)
        with patch.object(store,'now',return_value=self.day+'T18:01:00+08:00'),admin_client(app) as client:
            second,_=self.finish(stars=2)
            endpoint='/api/v2/stock-library'
            latest=client.get(endpoint,params={'q':'600000','start':self.day,'end':self.day,'session':'close','state':'wait'}).json()
            self.assertEqual(latest['total'],1)
            self.assertEqual((latest['items'][0]['run_id'],latest['items'][0]['stars']),(second['id'],2))
            scoped=client.get(endpoint,params={'run_id':first['id'],'pool':'watch'}).json()
            self.assertEqual([row['run_id'] for row in scoped['items']],[first['id']])
            progress=client.get('/api/v2/research/runs/'+first['id'],params={'progress':True}).json()
            self.assertEqual((progress['market_day'],progress['session'],bool(progress['has_bundle'])),(self.day,'close',True))
            self.assertEqual(latest['pools'],{'buy':0,'watch':1})
            self.assertEqual(client.get(endpoint,params={'q':'隔离公司','pool':'watch'}).json()['total'],1)
            self.assertEqual(latest['items'][0]['card']['counts'],{'selected_count':2,'recommended_count':0})
            self.assertEqual(len(latest['items'][0]['card']['quote']['trend']),20)
            self.assertGreater(latest['items'][0]['card']['quote']['ma20'],0)
            self.assertEqual(client.get(endpoint,params={'session':'noon'}).json()['total'],0)
            history_params={'symbol':'sh.600000','limit':1}
            history=client.get(endpoint,params=history_params).json()
            self.assertEqual(history['total'],2)
            self.assertEqual(history['items'][0]['run_id'],second['id'])
            self.assertEqual(client.get(endpoint,params={'run_id':first['id'],'before':history['next']}).status_code,422)
            older=client.get(endpoint,params={**history_params,'before':history['next']}).json()
            self.assertEqual(older['items'][0]['run_id'],first['id'])
            self.assertIsNone(older['next'])
            newer=client.get(endpoint,params={**history_params,'after':older['previous']}).json()
            self.assertEqual(newer['items'][0]['run_id'],second['id'])
            self.assertIsNone(newer['previous'])
            self.assertEqual(client.get(endpoint,params={'stars':'3'}).json()['items'][0]['run_id'],first['id'])
            self.assertEqual(client.get(endpoint,params={'stars':'unrated'}).json()['total'],0)
            original='/api/v2/research/runs/'+first['id']
            correction=client.post(original+'/corrections',json={'symbol':None,'action':'withdraw','reason':'隔离股票库','confirm':True})
            self.assertEqual(correction.status_code,200)
            withdrawn=client.get(endpoint,params={'state':'withdrawn'}).json()
            self.assertEqual(withdrawn['items'],[])
            self.assertEqual(client.get(original).json()['report'],first['report'])
            self.assertEqual(client.get(original+'/stocks/sh.600000/bars').status_code,404)

    def test_daily_pool_review_removes_only_after_comparable_close_and_hides_analysis(self):
        from foundation.main import app
        with patch.object(store,'now',return_value=self.day+'T18:00:00+08:00'):
            run,_=self.finish()
        stock=run['report']['decisions'][0]
        close=run['data']['bundle']['candidates'][0]['metrics']['close']
        following=self.days[65]
        with patch.object(store,'now',return_value=following+'T14:30:00+08:00'):
            ledger.maintain_pools()
            self.assertEqual(ledger.cycle_detail(stock['cycle_id'])['state'],'observe')
        with patch.object(store,'now',return_value=following+'T16:00:00+08:00'):
            low=stock['rise']['invalidation_price']-.5
            assets.publish('daily',[{'day':following,'symbol':stock['symbol'],'open':close,'high':close+.02,'low':low-.02,
                                     'close':low,'preclose':close,'volume':100,'amount':1000,'adjust':'raw'}],'bs_daily_all')
            ledger.maintain_pools()
            ledger.maintain_pools()
            with admin_client(app) as client:
                review=client.get('/api/v2/pool-review').json()
                self.assertEqual((review['day'],review['checked'],review['removed']),(following,1,1))
                self.assertEqual(client.get('/api/v2/stock-library').json()['total'],0)
                root='/api/v2/research/runs/'+run['id']
                self.assertEqual(client.get(root).json()['hidden_stocks'][0]['symbol'],stock['symbol'])
                self.assertEqual(client.get(root+'/stocks/sh.600000/bars').status_code,404)
                self.assertEqual(client.get(root+'/screening').json()['items'],[])
                self.assertEqual(client.get(root+'/coverage',params={'state':'covered'}).json()['items'],[])
                self.assertEqual(client.get('/api/v2/opportunities/sh.600000/analysis').status_code,404)
                self.assertEqual(client.get(root).json()['report'],run['report'])
            events=ledger.cycle_detail(stock['cycle_id'])['events']
            self.assertEqual(sum(event['kind']=='daily_pool_review' for event in events),1)

    def test_waiting_for_breakout_cannot_be_signed_as_ready_to_buy(self):
        bundle=self.bundle()
        bundle['mode']='noon_research'
        bundle['session_policy'].update(session='noon',action_valid_until='2026-11-02T14:55:00+08:00')
        c=bundle['candidates'][0]
        c['metrics'].update(action_adjustment_verified=True,rise={
            'type':'setup','state':'awaiting_trigger','trigger_above':10.25,'invalidation_price':9.5,'facts':c['bar_ids'][-2:]})
        for dim in c['dimensions']:
            if dim['id'] in ('F13','F14'):
                dim['state']='verified'
        report=model_report(bundle,action='select')
        decision=report['decisions'][0]
        decision['dimensions_used']=['F04']
        result=contracts.validate_report(store.dumps(report),bundle)
        self.assertEqual(result['decisions'][0]['action'],'conditional')
        decision['entry']['high']=10.25
        result=contracts.validate_report(store.dumps(report),bundle)
        self.assertEqual(result['decisions'][0]['action'],'watch')
        self.assertIn('参与区间未覆盖有效突破报价',result['decisions'][0]['formal_blockers'])

    def test_local_tool_round_and_safe_restart_from_validated_report(self):
        run=engine.submit(self.request(allow_evidence_round=True,max_requests=2))
        def answer(rid,plan,system,payload):
            candidate=payload['bundle']['candidates'][0]
            if 'evidence_round' not in payload:
                return store.dumps({'tool_requests':[{'tool':'fact_detail','symbol':candidate['symbol'],
                    'fact_id':candidate['bar_ids'][-1],'question':'核查收盘价'}]}),'tool-call'
            self.assertEqual(payload['evidence_round'][0]['evidence']['dataset'],'daily')
            return store.dumps(model_report(payload['bundle'])),'final-call'
        with patch.object(model_transport,'complete',side_effect=answer) as model,patch.object(ledger,'publish',side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                engine.process(run['id'])
        self.assertEqual(model.call_count,2)
        self.assertNotIn('validated_report',engine.get_run(run['id'])['data'])
        engine.recover_interrupted()
        with patch.object(model_transport,'complete') as model:
            engine.process(run['id'])
        model.assert_not_called()
        finished=engine.get_run(run['id'])
        self.assertIsNotNone(finished['report'])
        self.assertEqual(finished['data']['tool_receipt'][0]['state'],'available')

    def test_correction_and_notice_leave_original_and_trace_fact_revision(self):
        from foundation.main import app
        stamp=self.day+'T18:00:00+08:00'
        with store.db() as c:
            c.execute("UPDATE facts SET first_seen=CONCAT(day,'T15:10:00+08:00'),last_seen=CONCAT(day,'T15:10:00+08:00')")
        with patch.object(store,'now',return_value=stamp):
            run,_=self.finish()
            decision=run['report']['decisions'][0]
            original_id=decision['supports'][0]['fact_ids'][0]
            with store.db(readonly=True) as c:
                source=record_store.load(c,original_id)
            assets.publish('daily',[{**source,'amount':source['amount']+10}],'bs_daily_all')
            cycle=ledger.cycle_detail(decision['cycle_id'])
            self.assertTrue(any(f['kind']=='fact_revised' and f['prior_fact_id']==original_id for f in cycle['review_flags']))
            with admin_client(app) as client:
                endpoint='/api/v2/research/runs/'+run['id']+'/corrections'
                data={'symbol':decision['symbol'],'action':'withdraw','reason':'隔离更正','confirm':True}
                correction=client.post(endpoint,json=data).json()
                self.assertEqual(client.post(endpoint,json=data).json()['id'],correction['id'])
                notices=client.get('/api/v2/research-notices').json()
                self.assertEqual([n['kind'] for n in notices['items']],['correction','report'])
                self.assertTrue(client.post(f"/api/v2/research-notices/{correction['id']}/read").json()['read'])
        self.assertEqual(engine.get_run(run['id'])['report'],run['report'])
        self.assertEqual(ledger.cycle_detail(decision['cycle_id'])['state'],'removed')

    def test_whole_report_withdrawal_leaves_archive_and_clears_current_report(self):
        from foundation.main import app
        with patch.object(store,'now',return_value=self.day+'T18:00:00+08:00'):
            run,_=self.finish()
        cycle_id=run['report']['decisions'][0]['cycle_id']
        with admin_client(app) as client:
            root='/api/v2/research/runs/'+run['id']
            data={'symbol':None,'action':'withdraw','reason':'隔离样本重跑','confirm':True}
            response=client.post(root+'/corrections',json=data)
            self.assertEqual(response.status_code,200)
            self.assertEqual(client.post(root+'/corrections',json=data).json()['id'],response.json()['id'])
            dashboard=client.get('/api/v2/research?session=close').json()
            self.assertIsNone(dashboard['latest'])
            self.assertIsNone(dashboard['attempt'])
            archive=client.get('/api/v2/research/runs?outcome=published').json()
            self.assertEqual(archive['items'][0]['id'],run['id'])
            self.assertTrue(archive['items'][0]['withdrawn_at'])
            saved=client.get(root).json()
            self.assertEqual(saved['report'],run['report'])
            self.assertEqual(len(saved['corrections']),1)
            self.assertEqual(client.get('/api/v2/cycles/'+cycle_id).json()['state'],'removed')

    def test_extended_horizon_does_not_create_d1_d3_outcomes(self):
        run,_=self.finish(self.request(intent='以 D5 研究指定股票 sh.600000'))
        self.assertEqual(run['report']['study_horizon'],'D5')
        cycle=ledger.cycle_detail(run['report']['decisions'][0]['cycle_id'])
        self.assertEqual({o['horizon'] for o in cycle['outcomes']},{'D5'})
    def test_review_preserves_original_contract_and_original_report(self):
        with patch.object(store,'now',return_value=self.day+'T18:00:00+08:00'):
            initial,_=self.finish();decision=initial['report']['decisions'][0];cid=decision['cycle_id']
            before=ledger.cycle_detail(cid);old_report=copy.deepcopy(initial['report'])
            reviewed,_=self.finish(self.request(kind='review',cycle_ids=[cid]),review_state='remove')
        self.assertEqual(reviewed['status'],'completed_limited')
        self.assertEqual(reviewed['report']['decisions'][0]['tracking_state'],'removed')
        self.assertEqual(reviewed['report']['decisions'][0]['original_endpoints'],before['data']['endpoints'])
        self.assertEqual(reviewed['report']['decisions'][0]['original_entry'],before['data']['entry'])
        after=ledger.cycle_detail(cid)
        self.assertEqual(after['state'],'removed')
        for k in ('entry','endpoints','published_at','first_run','thesis'):self.assertEqual(before['data'][k],after['data'][k])
        self.assertEqual(engine.get_run(initial['id'])['report'],old_report)
        self.assertTrue(any(e['kind']=='review' for e in after['events']))
    def test_unknown_call_is_not_retried_or_recorded_empty_success(self):
        run=engine.submit(self.request())
        with patch.object(model_transport,'complete',side_effect=model_transport.ModelFailure('transport_unknown','远端未知',unknown=True)) as call:
            engine.process(run['id']);engine.process(run['id'])
        call.assert_called_once();result=engine.get_run(run['id'])
        self.assertEqual(result['status'],'unknown');self.assertIsNone(result['report'])
    def test_stop_before_call_and_intraday_block(self):
        run=engine.submit(self.request());engine.stop(run['id'])
        with patch.object(model_transport,'complete') as call:engine.process(run['id'])
        call.assert_not_called();self.assertEqual(engine.get_run(run['id'])['status'],'stopped')
        with patch.object(store,'now',return_value=self.day+'T11:20:00+08:00'):
            with self.assertRaises(HTTPException):engine.submit(self.request(session='noon'))
    def test_outcome_contract_missing_prices_and_fixed_endpoint(self):
        data={'entry_reference':None,'adjustment_verified':False}
        future='2026-11-20';self.assertEqual(ledger.evaluate_contract(data,True,future,'D1',None,'2026-11-19T16:00:00+08:00')['status'],'not_due')
        self.assertEqual(ledger.evaluate_contract(data,True,'2026-11-05','D1',{'close':11},'2026-11-06T16:00:00+08:00')['status'],'untriggered')
        data.update(entry_reference={'at':'2026-11-04T10:00:00+08:00','price':10},adjustment_verified=True)
        value=ledger.evaluate_contract(data,True,'2026-11-05','D1',{'close':11},'2026-11-06T16:00:00+08:00')
        self.assertAlmostEqual(value['return'],.1)
        self.assertEqual(ledger.evaluate_contract(data,False,'2026-11-05','D1',None,'2026-11-06T16:00:00+08:00')['status'],'observation')
    def test_api_has_shared_report_tracking_methods_and_zero_denominator(self):
        from foundation.main import app
        run,_=self.finish()
        with admin_client(app) as client:
            d=client.get('/api/v2/research').json();self.assertEqual(d['latest']['id'],run['id'])
            self.assertEqual(client.get('/api/v2/methods').json()['version'],methods.METHOD_VERSION)
            self.assertEqual(client.get('/api/v2/cycles?limit=1').json()['items'][0]['run_id'],run['id'])
            metrics=client.get('/api/v2/performance').json();self.assertIsNone(metrics['groups'][0]['hit_rate'])
            self.assertEqual(client.get('/api/v2/cycles?limit=201').status_code,422)
            self.assertEqual(client.get('/api/ai').json()['automatic_research_enabled'],False)

    def test_stream_final_text_usage_and_secret_boundary(self):
        run=engine.submit(self.request());plan=run['data']['plan'];real=httpx.AsyncClient
        def response(req):
            body=json.loads(req.content);self.assertTrue(body['stream']);self.assertTrue(body['stream_options']['include_usage'])
            events=[{'id':'isolated-stream','choices':[{'delta':{'reasoning_content':'private-thinking-not-stored','content':'{"ok":'}}]},
              {'choices':[{'delta':{'content':'true}'},'finish_reason':'stop'}]}, {'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':5}}]
            text=''.join('data: '+json.dumps(v)+'\n\n' for v in events)+'data: [DONE]\n\n'
            return httpx.Response(200,text=text,headers={'content-type':'text/event-stream'})
        with patch.object(model_transport.httpx,'AsyncClient',side_effect=lambda **kw:real(**kw,transport=httpx.MockTransport(response))):
            text,call=model_transport.complete(run['id'],plan,'JSON only',{'public':'fixture'})
        self.assertTrue(json.loads(text)['ok'])
        with store.db(readonly=True) as c:row=c.execute('SELECT status,data FROM research_calls WHERE id=%s',(call,)).fetchone()
        self.assertEqual(row['status'],'completed');self.assertNotIn('private-thinking',row['data']);self.assertNotIn('isolated-secret',row['data'])
        self.assertEqual(json.loads(row['data'])['usage']['completion_tokens'],5)

    def test_stream_interruption_requires_explicit_new_request(self):
        run=engine.submit(self.request());real=httpx.AsyncClient
        response=lambda req:httpx.Response(200,text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',headers={'content-type':'text/event-stream'})
        with patch.object(model_transport.httpx,'AsyncClient',side_effect=lambda **kw:real(**kw,transport=httpx.MockTransport(response))):
            engine.process(run['id'])
        self.assertEqual(engine.get_run(run['id'])['status'],'unknown')
        with self.assertRaises(HTTPException):engine.submit(self.request())
        new=engine.submit(self.request(acknowledge_unknown=True));self.assertEqual(new['status'],'queued')

    def test_chart_reads_frozen_fact_versions_after_revision(self):
        from foundation.main import app
        run,_=self.finish();candidate=run['data']['bundle']['candidates'][0]
        self.assertNotIn('bars',candidate)
        with store.db(readonly=True) as c:
            row=c.execute("SELECT f.id FROM heads h JOIN facts f ON f.id=h.fact_id WHERE h.dataset='daily' AND h.day=%s AND h.symbol='sh.600000'",(self.day,)).fetchone()
            bar=record_store.load(c,row[0])
        original=bar['close'];bar['close']+=.001
        assets.publish('daily',[bar],'bs_daily_all')
        with admin_client(app) as client:
            result=client.get('/api/v2/research/runs/'+run['id']+'/stocks/sh.600000/bars').json()
            self.assertEqual(result['items'][-1]['close'],original)
            self.assertEqual(len(result['items']),60)
            self.assertEqual(result['outlook']['reference_price'],original)
            self.assertEqual(result['outlook_source'],'sent')
            dimensions={row['id']:row for row in result['analysis']['items']}
            self.assertEqual(next(r['value'] for r in dimensions['F04']['readings'] if r['label']=='截点价'),original)

if __name__=='__main__':unittest.main()
