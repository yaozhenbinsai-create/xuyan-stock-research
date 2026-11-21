from datetime import datetime
import json
import unittest
import uuid
from unittest.mock import patch

from fastapi import HTTPException

from foundation import assets, database as store
from research import contracts, discovery, engine, evidence, learning, ledger, model_transport, preferences, preparation, reassessment, recaps, sessions, state_store
from tools.mysql_instance import create_isolated_database, drop_isolated_database
import test_research as fixtures


SCHEMA = 'stock_test_session_' + uuid.uuid4().hex[:8]
SYMBOL = 'sh.600000'


def setUpModule():
    create_isolated_database(SCHEMA)


def tearDownModule():
    drop_isolated_database(SCHEMA)


class SessionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ResearchTests('test_publish_once_idempotency_and_evidence_pin')
        with patch.object(fixtures, 'SCHEMA', SCHEMA):
            self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.day = self.fixture.days[65]
        self.previous = self.fixture.day

    def at(self, clock, day=None):
        return f'{day or self.day}T{clock}+08:00'

    def quote(self, clock, volume=260, price=13, *, day=None):
        day = day or self.day
        stamp = self.at(clock, day)
        assets.publish('quotes', [{'day':day,'symbol':SYMBOL,'key':datetime.fromisoformat(stamp).strftime('%Y%m%d%H%M%S'),
                        'source_time':stamp,'open':12.5,'close':price,'low':10,'high':max(price,12.5)+.02,
                        'preclose':(10+64*.03)*1.05,'volume':volume,'volume_unit':'shares','amount':3100}], 'tx_quotes')

    def test_report_decisions_and_unknown_usage_are_queryable_as_relational_fields(self):
        run_id='a'*16
        call_id='b'*16
        data={'session':'close','bundle':{'market':{'coverage':.8},'candidates':[]}}
        report={'title':'隔离关系式报告','mode':'close_research','published_at':self.at('18:00:00',self.previous),
                'forecast_target':{'version':sessions.TARGET_VERSION},'market_analysis':'隔离样本',
                'decisions':[{'symbol':SYMBOL,'name':'隔离公司','action':'watch','rating':{'stars':3}}]}
        measured={'attempt':12,'recoverable':True,'usage_state':'partial',
                  'usage_normalized':{'input':7,'output':None,'total':None,'cached_input':0,'reasoning_output':None}}
        with store.db() as c:
            c.execute('INSERT INTO research_runs(id,request_key,status,stage,created,updated,market_day,kind,data,report) '
                      'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                      (run_id,'a'*64,'completed','隔离核查',report['published_at'],report['published_at'],
                       self.previous,'discovery',store.dumps(data),store.dumps(report)))
            state_store.sync_run(c,run_id,data,report)
            c.execute('INSERT INTO research_calls(id,run_id,model_id,started,status,data) VALUES(%s,%s,%s,%s,%s,%s)',
                      (call_id,run_id,1,report['published_at'],'unknown',store.dumps(measured)))
            state_store.sync_call(c,call_id,measured)
        with store.db(readonly=True) as c:
            row=c.execute('SELECT session,has_forecast_target,decision_count,market_coverage FROM research_runs WHERE id=%s',(run_id,)).fetchone()
            self.assertEqual(tuple(row),('close',1,1,.8))
            decision=c.execute('SELECT symbol,action,stars FROM research_decisions WHERE run_id=%s',(run_id,)).fetchone()
            self.assertEqual(tuple(decision),(SYMBOL,'watch',3))
            call=c.execute('SELECT attempt_no,recoverable,input_tokens,output_tokens,total_tokens,cached_input_tokens FROM research_calls WHERE id=%s',(call_id,)).fetchone()
            self.assertEqual(tuple(call),(12,1,7,None,None,0))
            close=next(item for item in recaps._session_summaries(c,self.previous,self.previous) if item['session']=='close')
            self.assertEqual((close['issued'],close['targeted'],close['watch']),(1,1,1))

    def test_document_and_relational_state_sync_rejects_a_mismatched_write(self):
        run_id='c'*16
        data={'session':'tail','stop_requested':False}
        with store.db() as c:
            c.execute('INSERT INTO research_runs(id,request_key,status,stage,created,updated,market_day,kind,data) '
                      'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                      (run_id,'c'*64,'failed','隔离核查',self.at('14:36:00'),self.at('14:36:00'),
                       self.day,'discovery',store.dumps(data)))
            state_store.sync_run(c,run_id,data)
        with self.assertRaises(ValueError):
            with store.db() as c:
                c.execute('UPDATE research_runs SET data=%s WHERE id=%s',
                          (store.dumps({'session':'noon'}),run_id))
                state_store.sync_run(c,run_id,{'session':'close'})
        with store.db(readonly=True) as c:
            row=c.execute('SELECT session,data FROM research_runs WHERE id=%s',(run_id,)).fetchone()
            self.assertEqual(row['session'],'tail')
            self.assertEqual(json.loads(row['data']),data)

    def contract_cycle(self,data,*,synchronize=True):
        """A contract fixture, without pretending to issue a research report."""
        identifier='d'*16
        with store.db() as c:
            c.execute('INSERT INTO research_cycles(id,identity_key,run_id,symbol,market_day,created,updated,state,formal,data) '
                      'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                      (identifier,'d'*64,'e'*16,SYMBOL,self.day,data['published_at'],data['published_at'],
                       'waiting',1,store.dumps(data)))
            if synchronize:
                state_store.sync_cycle(c,identifier,data)
        return identifier

    def test_quote_registration_uses_relational_contract_and_keeps_null_endpoint(self):
        endpoints={'D1':self.fixture.days[66],'D3':None}
        data={'published_at':self.at('12:20:00'),'endpoints':endpoints,'dependency_ids':[],
              'session_policy':{'reference_from':self.at('13:00:00'),'action_valid_until':self.at('14:55:00')},
              'entry':{'low':12.4,'high':13.5,'invalidate_below':12.2,'condition':'核验原条件'},
              'invalidation':'跌破原失效位','structure_guard':{'below':12.5},
              'rise':{'trigger_above':12.9,'invalidation_price':12.5},
              'entry_reference':None,'adjustment_verified':False,'benchmark_symbols':[]}
        identifier=self.contract_cycle(data)
        for key,replacement in (
                ('entry',{**data['entry'],'low':13}),
                ('endpoints',{'D1':self.fixture.days[67],'D3':None}),
                ('endpoints',{'D1':self.fixture.days[66],'D3':self.fixture.days[68]}),
                ('session_policy',{**data['session_policy'],'reference_from':self.at('13:10:00')})):
            with self.subTest(frozen_field=key,replacement=replacement):
                with self.assertRaises(ValueError):
                    with store.db() as c:
                        changed={**data,key:replacement}
                        c.execute('UPDATE research_cycles SET data=%s WHERE id=%s',(store.dumps(changed),identifier))
                        state_store.sync_cycle(c,identifier,changed)
                with store.db(readonly=True) as c:
                    self.assertEqual(state_store.load_cycle_data(c,identifier),data)
        # The retained document is not the source of executable prices/endpoints.
        changed={**data,'entry':{'low':99,'high':100},'endpoints':{'D3':'2099-12-31'}}
        with store.db() as c:
            c.execute('UPDATE research_cycles SET data=%s WHERE id=%s',(store.dumps(changed),identifier))
        with patch.object(store,'now',return_value=self.at('13:15:00')):
            self.quote('13:00:05',price=12.7)
            self.quote('13:00:10',price=12.9)
            self.quote('13:00:15',price=13)
            reference=ledger.available_reference(identifier,'entry')['items'][0]
            self.assertEqual(reference['at'],self.at('13:00:15'))
            with self.assertRaises(HTTPException):
                ledger.record_reference(identifier,reference['fact_id'],False)
            result=ledger.record_reference(identifier,reference['fact_id'],True)
            self.assertEqual(result['data']['entry'],data['entry'])
            self.assertEqual(result['data']['endpoints'],endpoints)
            self.assertNotIn('removal_reference',result['data'])
            self.assertFalse(result['data']['adjustment_verified'])
            with store.db(readonly=True) as c:
                stored=c.execute('SELECT fact_id,price,conditions_confirmed,adjustment_verified,price_basis '
                                 "FROM research_cycle_references WHERE cycle_id=%s AND reference_kind='entry'",(identifier,)).fetchone()
                self.assertEqual(tuple(stored),(reference['fact_id'],13,1,0,'quote_reference_not_trade'))
                self.assertEqual(c.execute('SELECT endpoint FROM research_cycle_endpoints WHERE cycle_id=%s AND horizon=%s',
                                           (identifier,'D3')).fetchone()[0],None)
            with self.assertRaises(HTTPException):
                ledger.record_reference(identifier,reference['fact_id'],True)
            for replacement in (None,{**result['data']['entry_reference'],'price':14}):
                with self.assertRaises(ValueError):
                    with store.db() as c:
                        changed={**result['data'],'entry_reference':replacement}
                        c.execute('UPDATE research_cycles SET data=%s WHERE id=%s',(store.dumps(changed),identifier))
                        state_store.sync_cycle(c,identifier,changed)
            with store.db(readonly=True) as c:
                self.assertEqual(state_store.load_cycle_data(c,identifier)['entry_reference'],result['data']['entry_reference'])

    def test_missing_relational_contract_and_missing_endpoint_do_not_fall_back_to_document(self):
        data={'published_at':self.at('12:20:00'),'endpoints':{'D1':None},
              'entry':{'low':None,'high':None,'invalidate_below':None,'condition':None},
              'entry_reference':None,'adjustment_verified':None}
        identifier=self.contract_cycle(data,synchronize=False)
        with store.db(readonly=True) as c:
            with self.assertRaises(RuntimeError):
                state_store.load_cycle_data(c,identifier)
        with store.db() as c:
            state_store.sync_cycle(c,identifier,data)
        with store.db(readonly=True) as c:
            restored=state_store.load_cycle_data(c,identifier)
            self.assertEqual(restored,data)
        with store.db() as c:
            c.execute('DELETE FROM research_cycle_endpoints WHERE cycle_id=%s',(identifier,))
            with self.assertRaises(RuntimeError):
                state_store.load_cycle_data(c,identifier)

    def test_noon_uses_causal_snapshot_and_same_time_volume(self):
        with patch.object(store,'now',return_value=self.at('11:33:00')):
            for day in self.fixture.days[62:65]:
                self.quote('11:29:20',volume=130,day=day)
            self.quote('11:29:35')
            self.quote('14:34:40',volume=500,price=20)
            slot=sessions.choose('noon',self.day)
            b=discovery.snapshot(self.day,[SYMBOL],slot=slot)
        candidate=b['candidates'][0]
        self.assertEqual(b['mode'],'noon_research')
        self.assertEqual(b['market_data_cutoff'],self.at('11:30:00'))
        self.assertEqual(candidate['metrics']['close'],13)
        self.assertEqual(candidate['metrics']['relative_volume20'],2)
        self.assertEqual(candidate['metrics']['volume_samples'],3)
        self.assertTrue(candidate['bars'][-1]['partial_bar'])

    def test_absent_snapshot_is_a_data_limit_without_dispatch(self):
        with patch.object(store,'now',return_value=self.at('11:33:00')):
            run=engine.submit(self.fixture.request(market_day=self.day,session='noon',symbols=[SYMBOL]))
            with patch.object(model_transport,'complete') as model:
                engine.process(run['id'])
            model.assert_not_called()
        self.assertEqual(engine.get_run(run['id'])['status'],'data_limited')

    def test_noon_can_sign_at_lunch_but_expires_before_afternoon_open(self):
        with patch.object(store,'now',return_value=self.at('12:20:00')):
            self.quote('11:29:35')
            slot=sessions.choose('noon',self.day)
            self.assertFalse(slot['retrospective'])
            self.assertEqual(slot['launch_until'],self.at('12:35:00'))
            self.assertEqual(slot['publish_until'],self.at('12:50:00'))
            self.assertEqual(slot['reference_from'],self.at('13:00:00'))
            run,_=self.fixture.finish(self.fixture.request(market_day=self.day,session='noon',symbols=[SYMBOL]))
            self.assertEqual(run['report']['mode'],'noon_research')
        with patch.object(store,'now',return_value=self.at('12:50:00')):
            self.assertTrue(sessions.choose('noon',self.day)['retrospective'])

    def test_shortlisted_evidence_pass_only_queues_selected_sources_and_fact_ids(self):
        store.set_setting('automation_paused',False)
        assets.publish('announcements',[{'day':self.previous,'symbol':SYMBOL,'key':'focus-pdf','title':'交易异常波动公告',
                        'published':self.at('18:00:00',self.previous),
                        'url':'https://static.cninfo.com.cn/example.PDF'}],'cninfo_announcements')
        fid=assets.rows('announcements',stock=SYMBOL)['items'][0]['_id']
        with (patch.object(store,'now',return_value=self.at('12:00:00')),
              patch.object(preparation.collection,'submit',side_effect=lambda raw,**kw:{'id':raw['source']}) as submitted,
              patch.object(preparation.collection,'get_job',return_value={'status':'completed','done':1,'total':1,'received':1})):
            result=preparation.acquire([SYMBOL],self.day,'noon',self.at('12:50:00'))
        self.assertEqual([row['source'] for row in result],['cninfo_text','cninfo_announcements','bs_actions','bs_profit','bs_cashflow'])
        self.assertTrue(all(call.args[0]['symbols']==[SYMBOL] for call in submitted.call_args_list))
        original_calls=[call for call in submitted.call_args_list if call.args[0]['source']=='cninfo_text']
        self.assertEqual(len(original_calls),1)
        self.assertEqual(original_calls[0].kwargs['records'],[fid])

    def test_automatic_slot_is_idempotent_and_late_slot_does_not_submit(self):
        with patch.object(store,'now',return_value=self.at('10:50:00')):
            sessions.save({'enabled':['noon','tail'],'symbols':[SYMBOL],'confirm':True})
        with patch.object(store,'now',return_value=self.at('11:33:00')):
            self.quote('11:29:10')
            engine.schedule_tick()
            engine.schedule_tick()
            run=engine.list_runs(5)['items'][0]
            self.assertEqual(engine.list_runs(5)['total'],1)
            self.assertIsNone(engine.get_run(run['id'])['data']['plan']['budget']['max_requests'])
            engine.stop(run['id'])
        with patch.object(store,'now',return_value=self.at('14:50:00')):
            engine.schedule_tick()
            self.assertTrue(any(s['session']=='tail' and s['state']=='missed' for s in sessions.status()['recent']))
        self.assertEqual(engine.list_runs(5)['total'],1)

    def test_previous_unknown_blocks_same_slot_but_next_automatic_slot_can_start(self):
        store.set_setting('automation_paused',False)
        with patch.object(store,'now',return_value=self.at('17:32:00',self.previous)):
            sessions.save({'enabled':['noon','close'],'confirm':True})
            old=engine.submit(self.fixture.request())
            with store.db() as conn:
                conn.execute("UPDATE research_runs SET status='unknown' WHERE id=%s",(old['id'],))
            with self.assertRaises(HTTPException) as conflict:
                engine.submit(self.fixture.request(),automatic=sessions.config())
            self.assertEqual(conflict.exception.status_code,409)
            sessions.remember_slot(self.previous,'close','submitted','已登记独立时段研究',old['id'])
        self.assertEqual(sessions.captures(self.at('11:24:59')),[])
        self.assertEqual(sessions.captures(self.at('11:25:59')),[])
        self.assertEqual(sessions.captures(self.at('11:26:00')),[(self.day,'noon',[SYMBOL])])
        with patch.object(store,'now',return_value=self.at('11:33:00')):
            self.quote('11:29:10')
            engine.schedule_tick()
            engine.schedule_tick()
            receipt={row['run_id']:row for row in sessions.status()['recent'] if row['run_id']}
            self.assertEqual(receipt[old['id']]['result']['status'],'unknown')
            self.assertEqual(len(receipt),2)
            today=next(row for row in receipt.values() if row['day']==self.day)
            self.assertEqual(today['result']['status'],'queued')
        self.assertEqual(engine.list_runs(5)['total'],2)

    def market_daily_receipt(self,day):
        with store.db() as conn:
            conn.execute('INSERT INTO collection_units VALUES(%s,%s,%s,%s)',
                         ('bs_daily_all',store.dumps({'start':day,'end':day,'symbols':[],'period':5}),self.at('18:00:00',day),1))

    def test_daily_recap_waits_for_full_daily_and_archives_once(self):
        day=self.previous
        with patch.object(store,'now',return_value=self.at('18:50:00',day)):
            recaps.save_schedule({'daily':True,'monthly':False})
        with patch.object(store,'now',return_value=self.at('19:05:00',day)):
            recaps.schedule_tick()
            self.assertIsNone(recaps.daily(day)['archive'])
            self.market_daily_receipt(day)
            recaps.schedule_tick()
            self.assertIsNone(recaps.daily(day)['archive'])
        with patch.object(store,'now',return_value=self.at('21:01:00',day)),patch.object(model_transport,'complete') as model:
            recaps.schedule_tick()
            archived=recaps.daily(day)
            self.assertEqual(archived['archive']['news_status'],'unconfirmed')
            self.assertFalse(archived['archive']['late'])
            model.assert_not_called()
        with patch.object(store,'now',return_value=self.at('21:15:00',day)):
            recaps.schedule_tick()
            self.assertEqual(recaps.daily(day),archived)
            self.assertEqual(recaps.schedule_status()['daily']['latest'],day)

    def test_monthly_recap_waits_for_third_following_trading_day_and_archives_once(self):
        day='2026-11-04'
        month='2026-10'
        with patch.object(store,'now',return_value=self.at('16:00:00','2026-10-30')):
            recaps.save_schedule({'daily':False,'monthly':True})
        self.market_daily_receipt(day)
        for stamp in (self.at('20:00:00','2026-11-03'),self.at('19:29:59',day)):
            with patch.object(store,'now',return_value=stamp):
                recaps.schedule_tick()
                self.assertIsNone(recaps.monthly(month)['archive'])
        with patch.object(store,'now',return_value=self.at('19:31:00',day)):
            recaps.schedule_tick()
            archived=recaps.monthly(month)
            self.assertEqual(archived['archive']['settled_day'],day)
            self.assertEqual(len(archived['sessions']),3)
            self.assertTrue(archived['market_days'])
        with patch.object(store,'now',return_value=self.at('19:32:00',day)):
            recaps.schedule_tick()
            self.assertEqual(recaps.monthly(month),archived)
            self.assertEqual(recaps.schedule_status()['monthly']['latest'],month)

    def test_preference_revocation_applies_before_queued_dispatch(self):
        p=preferences.save({'note':'先核对风险','confirm':True,'candidate_limit':3})
        run=engine.submit(self.fixture.request())
        self.assertEqual(run['data']['preference_revision'],p['revision'])
        self.assertNotIn('先核对风险',json.dumps(run,ensure_ascii=False))
        preferences.save({'confirm':True,'revoke':True})
        with patch.object(model_transport,'complete') as model:
            engine.process(run['id'])
        model.assert_not_called()
        self.assertIn('撤销',engine.get_run(run['id'])['data']['error'])

    def test_noon_formal_decision_requires_evidence_and_uses_first_afternoon_quote(self):
        with patch.object(store,'now',return_value=self.at('11:10:00')),patch.object(store,'today',return_value=self.day):
            announcement={'day':self.previous,'symbol':SYMBOL,'key':'risk-source','title':'公司经营和风险核查说明',
                          'text':'测试公告原文。用于核验程序路径。','published':self.at('17:00:00',self.previous)}
            assets.publish('announcements',[announcement],'cninfo_text')
            coverage={'day':self.previous,'symbol':SYMBOL,'key':'coverage:'+self.fixture.days[0],'record_kind':'coverage',
                      'coverage_from':self.fixture.days[0],'coverage_through':self.previous,'event_count':0}
            assets.publish('actions',[coverage],'bs_actions')
            with store.db(readonly=True) as conn:
                ids={r['dataset']:r['id'] for r in conn.execute("SELECT dataset,id FROM facts WHERE dataset IN ('announcements','actions')")}
            for kind,dataset in evidence.CHECKS.items():
                evidence.save_check({'symbol':SYMBOL,'kind':kind,'fact_ids':[ids[dataset]],'state':'verified',
                                     'note':'隔离样本核验依据','valid_until':self.fixture.days[66]})
            preferences.save({'confirm':True})

        with patch.object(store,'now',return_value=self.at('11:33:00')):
            self.quote('11:29:10')
            run=engine.submit(self.fixture.request(market_day=self.day,session='noon',symbols=[SYMBOL]))

            def answer(rid, plan, system, payload):
                report=fixtures.model_report(payload['bundle'],action='select')
                report['decisions'][0]['entry'].update(low=12.5,high=13.5,invalidate_below=12)
                return store.dumps(report),'isolated-session'

            with patch.object(model_transport,'complete',side_effect=answer):
                engine.process(run['id'])
        report=engine.get_run(run['id'])['report']
        self.assertEqual(report['decisions'][0]['action'],'select')
        self.assertEqual(report['horizons'],{'PM':self.day})
        self.assertEqual(report['decisions'][0]['outlook']['upside']['horizon'],'PM')
        cyid=report['decisions'][0]['cycle_id']
        with patch.object(store,'now',return_value=self.at('13:15:00')):
            from research import stock_library
            buy=stock_library.page(6,pool='buy')
            self.assertEqual(buy['items'][0]['card']['counts'],{'selected_count':1,'recommended_count':1})
            self.assertEqual(stock_library.page(6,pool='watch')['total'],0)
            self.quote('12:00:00')
            self.quote('13:00:05')
            self.quote('13:00:10')
            available=ledger.available_reference(cyid,'entry')['items'][0]
            self.assertEqual(available['at'],self.at('13:00:05'))
            changed=ledger.record_reference(cyid,available['fact_id'],True)
            self.assertEqual(changed['state'],'active')
            self.assertEqual(changed['data']['endpoints'],report['horizons'])
            with self.assertRaises(HTTPException):
                ledger.record_reference(cyid,available['fact_id'],True)
        with patch.object(store,'now',return_value=self.at('16:00:00')):
            assets.publish('daily',[{'day':self.day,'symbol':SYMBOL,'open':12.8,'high':13.5,'low':12.5,
                                     'preclose':(10+64*.03)*1.05,'close':13.4,'volume':300,'amount':3900}],'bs_daily_all')
            ledger.refresh_outcomes(cycle_ids=[cyid])
            recap=recaps.monthly(self.day[:7])
            noon=next(row for row in recap['sessions'] if row['session']=='noon')
            self.assertEqual(noon['targeted'],1)
            self.assertIn({'horizon':'PM','cohort':'selected','total':1,'scored':1,'up':1,'missing':0},noon['outcomes'])
            self.assertEqual(recaps.daily(self.day)['market']['up'],1)

    def test_tail_signs_only_next_trading_day_and_close_keeps_one_to_three_days(self):
        with patch.object(store,'now',return_value=self.at('14:38:00')):
            self.quote('14:34:50')
            following=self.fixture.days[66]
            close=sessions.choose('close',self.previous)
            self.assertEqual(close['target']['horizons'],{'D1':self.day,'D3':self.fixture.days[67]})
            run=engine.submit(self.fixture.request(market_day=self.day,session='tail',symbols=[SYMBOL]))
            def answer(rid,plan,system,payload):
                bundle=payload['bundle']
                self.assertEqual(payload['research_plan']['target']['horizons'],{'D1':following})
                self.assertIn('尾盘寻找下一交易日',system)
                report=fixtures.model_report(bundle)
                case=report['decisions'][0]['outlook']['upside']
                case['horizon']='D3'
                with self.assertRaisesRegex(ValueError,'预测窗口'):
                    contracts.validate_report(store.dumps(report),bundle)
                case['horizon']='D1'
                return store.dumps(report),'isolated-tail'
            with patch.object(model_transport,'complete',side_effect=answer):
                engine.process(run['id'])
            published=engine.get_run(run['id'])['report']
            self.assertEqual(published['horizons'],{'D1':following})

    def test_setup_uses_first_quote_strictly_above_trigger_and_checks_daily_floor(self):
        cycle={'symbol':SYMBOL}
        data={'published_at':self.at('11:33:00'),'endpoints':{'D3':self.fixture.days[67]},
              'session_policy':{'reference_from':self.at('13:00:00'),'action_valid_until':self.at('14:55:00')},
              'entry':{'low':12.4,'high':13.5},'rise':{'trigger_above':12.9,'invalidation_price':12.5}}
        with patch.object(store,'now',return_value=self.at('13:15:00')):
            self.quote('13:00:05',price=12.7)
            self.quote('13:00:10',price=12.9)
            self.quote('13:00:15',price=13.0)
            with store.db(readonly=True) as c:
                result=ledger.quote_window(c,cycle,data,'entry')
            self.assertEqual(result[0]['at'],self.at('13:00:15'))
        with patch.object(store,'now',return_value=self.at('15:20:00')):
            assets.publish('daily',[{'day':self.day,'symbol':SYMBOL,'close':12.45,
                                    'open':12.6,'high':13.05,'low':12.35,'volume':100,'amount':1200}],
                           'bs_daily_all')
            with store.db(readonly=True) as c:
                flags=reassessment.flags(c,cycle,{**data,'dependency_ids':[]})
            self.assertEqual([(row['kind'],row['threshold']) for row in flags],[('structure_invalidation',12.5)])

    def test_experiment_requires_time_out_and_shadow_evidence(self):
        e=learning.create({'title':'隔离影子实验','threshold':1.6,'train_start':self.fixture.days[0],
                           'train_end':self.fixture.days[10],'validation_end':self.fixture.days[20],'test_end':self.fixture.days[30]})
        self.assertEqual(learning.shadow_candidates()[0]['id'],e['id'])
        self.assertFalse(learning.detail(e['id'])['evaluation']['qualified'])
        with self.assertRaises(HTTPException):
            learning.change(e['id'],'publish',True)
        self.assertEqual(learning.active(),learning.BASE)
