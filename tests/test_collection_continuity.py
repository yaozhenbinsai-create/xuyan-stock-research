"""Collection regressions use an isolated MySQL schema and temporary data root."""
from datetime import datetime
import json
import time
import unittest
import uuid
from unittest.mock import patch

import httpx

from foundation import assets, collection, database as store, queue_state, retention, sources
from tools.mysql_instance import create_isolated_database, drop_isolated_database
import test_foundation as fixtures

SCHEMA = 'stock_test_collection_' + uuid.uuid4().hex[:10]
DAY = '2026-11-19'


def setUpModule():
    create_isolated_database(SCHEMA)


def tearDownModule():
    drop_isolated_database(SCHEMA)


class CollectionContinuityTests(fixtures.IsolatedCase):
    @classmethod
    def setUpClass(cls):
        cls.database = SCHEMA

    def setUp(self):
        super().setUp()
        store.set_setting('automation_paused', False)

    def candidate(self, symbol='sz.000001'):
        rid='a'*16
        with store.db() as c:
            c.execute('INSERT INTO research_runs(id,request_key,status,stage,created,updated,market_day,kind,data,report) '
                      'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                      (rid,'a'*64,'completed','完成',DAY+'T17:00:00+08:00',DAY+'T17:00:00+08:00',DAY,'discovery','{}','{}'))
            c.execute('INSERT INTO research_candidates VALUES(%s,%s,%s)',(rid,0,symbol))
        return symbol

    def schedule(self, identifier, source, *, at='18:50', enabled=True, trading_only=True):
        value={'id':identifier,'name':identifier,'source':source,'at':at,'kind':'collection',
               'enabled':enabled,'trading_only':trading_only,'interval':0,'last_job':None,'last_slot':None}
        with store.db() as c:
            c.execute('INSERT INTO schedules(id,data) VALUES(%s,%s)',(identifier,store.dumps(value)))
            queue_state.sync_schedule(c,identifier,value)

    def test_empty_watchlist_uses_real_candidates_without_changing_watchlist(self):
        symbol=self.candidate()
        store.set_setting('watchlist', [])
        self.schedule('financials','bs_profit')
        plan=next(s for s in collection.schedules() if s['id']=='financials')
        self.assertFalse(plan['needs_symbols'])
        self.assertEqual(plan['symbols_count'],1)
        with patch.object(store,'today',return_value=DAY),patch.object(assets,'local_now',return_value=datetime.fromisoformat(DAY+'T19:00:00+08:00')):
            job=collection.run_schedule('financials',manual=False)
        self.assertEqual(job['symbols'],[symbol])
        self.assertEqual({(p['year'],p['quarter']) for p in job['items']},{(2026,3),(2025,3)})
        self.assertEqual(store.setting('watchlist'),[])

    def test_policy_upgrade_preserves_saved_switches_and_is_applied_once(self):
        self.schedule('cashflow','bs_cashflow',at='20:00',enabled=False)
        self.schedule('basics','bs_universe',at='08:00',trading_only=False)
        self.schedule('basic_details','bs_basics',at='16:30')
        self.schedule('daily','bs_daily_all',at='15:30')
        collection.ensure_foundation_schedules()
        plans={p['id']:p for p in collection.schedules()}
        self.assertFalse(plans['cashflow']['enabled'])
        self.assertEqual(plans['cashflow']['at'],'20:00')
        self.assertTrue(plans['basics']['trading_only'])
        self.assertEqual(plans['basic_details']['at'],'07:55')
        self.assertEqual(plans['daily']['at'],'17:30')
        collection.save_schedule('basic_details',{'at':'08:15','enabled':True,'trading_only':True})
        collection.ensure_foundation_schedules()
        plans={p['id']:p for p in collection.schedules()}
        self.assertEqual(plans['basic_details']['at'],'08:15')
        self.assertEqual(len([p for p in plans if p=='fund_flows']),1)

    def test_repeated_due_ticks_submit_one_job_and_weekend_roster_waits(self):
        self.candidate()
        self.schedule('financials','bs_profit')
        self.schedule('basics','bs_universe',at='08:00')
        assets.publish('calendar',[{'day':DAY,'symbol':'*','trading':True}],'bs_calendar')
        worker=collection.Engine();worker.last_maintenance=time.monotonic()
        with patch.object(store,'today',return_value=DAY),patch.object(assets,'local_now',return_value=datetime.fromisoformat(DAY+'T19:00:00+08:00')):
            # Keep only the financial plan in this tick so the test has one
            # real due job rather than unrelated source fixtures.
            plans=[p for p in collection.schedules() if p['id']=='financials']
            with patch.object(collection,'schedules',return_value=plans):
                worker.tick()
                plans=collection.schedules()
            with patch.object(collection,'schedules',return_value=[p for p in plans if p['id']=='financials']):
                worker.tick()
        with store.db(readonly=True) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM collection_jobs WHERE schedule_id='financials'").fetchone()[0],1)
        weekend='2026-11-21'
        with patch.object(store,'today',return_value=weekend),patch.object(assets,'local_now',return_value=datetime.fromisoformat(weekend+'T09:00:00+08:00')),\
             patch.object(assets,'calendar',return_value={weekend:False}),patch.object(collection,'run_schedule') as submit:
            worker.tick()
        submit.assert_not_called()

    def test_restart_resumes_original_automatic_job_at_saved_unit(self):
        assets.publish('calendar',[{'day':'2026-11-05','symbol':'*','trading':True}],'bs_calendar')
        job=collection.submit({'source':'bs_daily_all','start':'2026-11-04','end':'2026-11-05'},trigger='schedule')
        with store.db() as c:
            j=collection.get_job(job['id']);j.update(done=1,status='running',added=1)
            c.execute('UPDATE collection_jobs SET status=%s,data=%s WHERE id=%s',('running',store.dumps(j),j['id']))
            queue_state.sync_job(c,j['id'],j)
        worker=collection.Engine()
        with patch.object(collection.threading,'Thread'):
            worker.start()
        restored=collection.get_job(job['id'])
        self.assertEqual(restored['status'],'queued')
        self.assertEqual(restored['done'],1)
        self.assertEqual(restored['added'],1)
        self.assertEqual(worker.claim()['id'],job['id'])
        worker.stop()

    def test_restart_preserves_pause_and_cancel_and_does_not_recreate_old_snapshot(self):
        assets.save_source(fixtures.source('tx_quotes','quotes','quotes')|{'provider':'tencent'})
        with patch.object(store,'today',return_value='2026-11-18'):
            old=collection.submit({'source':'tx_quotes','start':'2026-11-18','end':'2026-11-18','symbols':['sh.600000']},trigger='schedule')
        paused=collection.submit({'source':'bs_daily_all','start':'2026-11-04','end':'2026-11-04'})
        collection.control(paused['id'],'pause')
        with store.db() as c:
            value=collection.get_job(old['id']);value['status']='running'
            c.execute('UPDATE collection_jobs SET status=%s,data=%s WHERE id=%s',('running',store.dumps(value),value['id']))
            queue_state.sync_job(c,value['id'],value)
        worker=collection.Engine()
        with patch.object(store,'today',return_value=DAY),patch.object(collection.threading,'Thread'):
            worker.start()
        self.assertEqual(collection.get_job(old['id'])['status'],'partial')
        self.assertEqual(collection.get_job(paused['id'])['status'],'paused')
        with patch.object(store,'today',return_value=DAY):
            with self.assertRaisesRegex(Exception,'历史截点'):
                collection.control(old['id'],'resume')
        worker.stop()

    def test_transient_failure_keeps_original_unit_and_can_save_on_retry(self):
        job=collection.submit({'source':'bs_daily_all','start':'2026-11-04','end':'2026-11-04'},trigger='schedule',schedule_id='daily')
        worker=collection.Engine();claimed=worker.claim()
        with patch.object(sources,'collect',side_effect=sources.SourceError('HTTP 504','network')):
            worker.execute_unit(claimed)
        waiting=collection.get_job(job['id'])
        self.assertEqual((waiting['status'],waiting['done'],waiting['added']),('waiting',0,0))
        waiting['retry_at']=0
        with store.db() as c:
            c.execute('UPDATE collection_jobs SET data=%s WHERE id=%s',(store.dumps(waiting),waiting['id']))
            queue_state.sync_job(c,waiting['id'],waiting)
        claimed=worker.claim()
        with patch.object(sources,'collect',return_value=[fixtures.bar(11)]):
            worker.execute_unit(claimed)
        saved=collection.get_job(job['id'])
        self.assertEqual((saved['id'],saved['status'],saved['done']),(job['id'],'completed',1))
        self.assertEqual(assets.rows('daily')['items'][0]['close'],11)

    def test_http_statuses_keep_transient_rate_limit_and_permission_semantics(self):
        for status,kind in ((502,'network'),(504,'network'),(429,'rate_limit'),(403,'restricted')):
            with self.subTest(status=status),patch.object(httpx,'request',return_value=httpx.Response(status,request=httpx.Request('GET','https://example.invalid'))):
                with self.assertRaises(sources.SourceError) as caught:
                    sources.http('GET','https://example.invalid')
                self.assertEqual(caught.exception.kind,kind)

    def test_scheduled_originals_are_focused_and_derived_financials_wait_for_them(self):
        source=fixtures.source('cninfo_text','announcements','announcement_text')|{'provider':'cninfo'}
        assets.save_source(source)
        for number in range(3):
            assets.publish('announcements',[{'day':'2026-11-04','symbol':'sh.600000',
                'key':str(number),'title':'2026年第三季度报告'+str(number),
                'published':'2026-11-04','url':'https://static.cninfo.com.cn/test'+str(number)+'.pdf'}],
                'cninfo_announcements')
        original=collection.submit({'source':'cninfo_text','start':'2026-11-04','end':'2026-11-04',
                                    'symbols':['sh.600000']},trigger='schedule',schedule_id='originals')
        self.assertTrue(original['items'][0]['plan_originals'])
        collection.Engine().execute_unit(original)
        self.assertEqual(collection.get_job(original['id'])['total'],2)
        derived=collection.submit({'source':'cninfo_financials','start':'2026-11-04','end':'2026-11-04',
                                   'symbols':['sh.600000']},trigger='schedule',schedule_id='filing_financials')
        worker=collection.Engine()
        with patch.object(sources,'collect') as network:
            worker.execute_unit(derived)
        network.assert_not_called()
        waiting=collection.get_job(derived['id'])
        self.assertEqual((waiting['status'],waiting['done']),('waiting',0))
        self.assertGreater(waiting['retry_at'],time.time())

    def test_late_index_is_read_before_original_plan_is_frozen(self):
        for sid,op in (('cninfo_announcements','announcements'),('cninfo_text','announcement_text')):
            assets.save_source(fixtures.source(sid,'announcements',op)|{'provider':'cninfo'})
        index=collection.submit({'source':'cninfo_announcements','start':'2026-11-04','end':'2026-11-04',
                                  'symbols':['sh.600000']},trigger='schedule')
        original=collection.submit({'source':'cninfo_text','start':'2026-11-04','end':'2026-11-04',
                                    'symbols':['sh.600000']},trigger='schedule',schedule_id='originals')
        worker=collection.Engine()
        worker.execute_unit(original)
        self.assertEqual(collection.get_job(original['id'])['status'],'waiting')
        record={'day':'2026-11-04','symbol':'sh.600000','key':'late',
                'title':'2026年第三季度报告','published':'2026-11-04',
                'url':'https://static.cninfo.com.cn/late.pdf'}
        assets.publish('announcements',[record],'cninfo_announcements')
        index.update(status='completed',done=index['total'])
        worker.persist(index)
        worker.execute_unit(collection.get_job(original['id']))
        planned=collection.get_job(original['id'])
        self.assertEqual((planned['status'],planned['total']),('queued',1))
        self.assertIn('record_id',planned['items'][0])
        with patch.object(sources,'collect',return_value=[record|{'text':'已读取的真实测试财报正文'*15}]):
            worker.execute_unit(planned)
        self.assertEqual(collection.get_job(original['id'])['status'],'completed')
        self.assertTrue(assets.rows('announcements')['items'][0]['text'])

    def test_failed_index_remains_a_real_gap_in_original_task(self):
        for sid,op in (('cninfo_announcements','announcements'),('cninfo_text','announcement_text')):
            assets.save_source(fixtures.source(sid,'announcements',op)|{'provider':'cninfo'})
        index=collection.submit({'source':'cninfo_announcements','start':DAY,'end':DAY,
                                  'symbols':['sh.600000']},trigger='schedule')
        index.update(status='failed',message='来源拒绝访问（HTTP 403）')
        collection.Engine().persist(index)
        original=collection.submit({'source':'cninfo_text','start':DAY,'end':DAY,
                                    'symbols':['sh.600000']},trigger='schedule',schedule_id='originals')
        collection.Engine().execute_unit(original)
        result=collection.get_job(original['id'])
        self.assertEqual((result['status'],result['no_data']),('partial',1))
        self.assertEqual(result['index_dependency_gaps'],[{'id':index['id'],'status':'failed'}])

    def test_daily_financial_schedule_refreshes_same_period_revisions(self):
        record={'day':'2026-10-30','published':'2026-10-30','report_date':'2026-09-30',
                'report_type':'profit','symbol':'sh.600000','key':'profit:2026-09-30','fields':{'netProfit':'1'}}
        assets.publish('financials',[record],'bs_profit')
        job=collection.submit({'source':'bs_profit','start':'2026-01-01','end':DAY,'symbols':['sh.600000']},
                              trigger='schedule',schedule_id='financials',financial_periods=['2026Q3'])
        worker=collection.Engine()
        with patch.object(sources,'collect',return_value=[record|{'fields':{'netProfit':'2'}}]) as network:
            worker.execute_unit(worker.claim())
        network.assert_called_once()
        self.assertEqual(collection.get_job(job['id'])['status'],'completed')
        self.assertEqual(assets.rows('financials')['items'][0]['fields']['netProfit'],'2')

    def test_comparative_windows_follow_the_original_fiscal_disclosure_period(self):
        cases={'2026-11-20':('2025-09-30','2025-10-01','2025-11-20'),
               '2026-10-20':('2025-09-30','2025-10-01','2025-10-31'),
               '2026-07-20':('2025-06-30','2025-07-01','2025-08-31'),
               '2026-02-20':('2024-12-31','2025-01-01','2025-04-30'),
               '2026-05-20':('2025-03-31','2025-04-01','2025-05-20')}
        for day,expected in cases.items():
            with self.subTest(day=day):self.assertEqual(collection.comparison_window(day),expected)

    def test_comparative_original_selection_and_completed_period_reuse(self):
        store.set_setting('watchlist',['sh.600000'])
        titles=('2025年第三季度报告','2025年第三季度报告摘要','H股2025年第三季度报告',
                '2025年第三季度业绩说明会公告','2025年第三季度经营数据简报',
                '关于2025年第三季度计提资产减值准备的公告',
                '关于披露2025年第三季度报告的提示性公告')
        for identifier,title in enumerate(titles):
            assets.publish('announcements',[{'day':'2025-10-30','symbol':'sh.600000','key':str(identifier),
                'title':title,'published':'2025-10-30','url':'https://static.cninfo.com.cn/old'+str(identifier)+'.pdf'}],
                'cninfo_announcements')
        retention.cleanup()
        with store.db(readonly=True) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM evidence_refs WHERE owner_type='financial_pending'").fetchone()[0],2)
        chosen=collection.comparison_original_ids(['sh.600000'],'2025-10-01','2025-10-31')
        self.assertEqual(len(chosen),1)
        with store.db(readonly=True) as c:
            self.assertEqual(assets.record_store.load(c,chosen[0])['title'],'2025年第三季度报告')
        collection.ensure_foundation_schedules()
        assets.save_source(fixtures.source('cninfo_announcements','announcements','announcements')|{'provider':'cninfo'})
        with patch.object(store,'today',return_value='2026-11-20'):
            index=collection.run_schedule('comparison_index',manual=False)
        self.assertEqual(index['items'][0]['start'],'2025-10-01')
        self.assertEqual(index['items'][0]['end'],'2025-11-20')
        assets.publish('financials',[{'day':'2025-10-30','published':'2025-10-30','report_date':'2025-09-30',
            'report_type':'filing','symbol':'sh.600000','key':'partial-old-filing',
            'fields':{'filing_fact_id':chosen[0],'parent_profit':'123','currency':'CNY','period_basis':'cumulative'}}],
            'cninfo_financials')
        with store.db(readonly=True) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM evidence_refs WHERE owner_type='financial_pending'").fetchone()[0],1)
        retention.cleanup()
        with store.db(readonly=True) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM facts WHERE dataset='announcements'").fetchone()[0],2)
        assets.publish('financials',[{'day':'2025-10-30','published':'2025-10-30','report_date':'2025-09-30',
            'report_type':'filing','symbol':'sh.600000','key':'old-filing',
            'fields':{'filing_fact_id':chosen[0],'operating_revenue':'12345.6','currency':'CNY','period_basis':'cumulative'}}],'cninfo_financials')
        with patch.object(store,'today',return_value='2026-11-20'):
            reused=collection.run_schedule('comparison_index',manual=False)
        self.assertTrue(reused['reused'])
        with store.db(readonly=True) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM evidence_refs WHERE owner_type='financial_pending'").fetchone()[0],0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM evidence_refs WHERE owner_type='financial_filing'").fetchone()[0],2)
        retention.cleanup()
        with store.db(readonly=True) as c:
            self.assertIsNotNone(c.execute('SELECT id FROM facts WHERE id=%s',(chosen[0],)).fetchone())
        late={'day':'2025-11-14','symbol':'sh.688981','key':'late-quarter',
              'title':'中芯国际2025年第三季度报告','published':'2025-11-14',
              'url':'https://static.cninfo.com.cn/late-quarter.pdf'}
        assets.publish('announcements',[late],'cninfo_announcements')
        chosen=collection.comparison_original_ids(['sh.688981'],'2025-11-01','2025-11-20')
        self.assertEqual(len(chosen),1)
        collection.Engine().pin_comparison_inputs({'schedule_id':'comparison_index'},
            {'start':'2025-11-01','end':'2025-11-20','symbols':['sh.688981']},[late])
        with store.db(readonly=True) as c:
            owner=c.execute("SELECT owner_id FROM evidence_refs WHERE owner_type='financial_pending' AND fact_id=%s",(chosen[0],)).fetchone()
        self.assertEqual(owner[0],'sh.688981:2025-09-30')

    def test_unusual_report_titles_remain_protected_until_amount_derivation(self):
        symbols=['sh.600498','sh.600936']
        titles=['600498_2025年_三季度报告','广西广电第三季度报告']
        for symbol,title in zip(symbols,titles):
            assets.publish('announcements',[{'day':'2025-10-30','symbol':symbol,'key':'filing',
                'title':title,'published':'2025-10-30','url':'https://static.cninfo.com.cn/'+symbol+'.pdf'}],
                'cninfo_announcements')
        retention.cleanup()
        chosen=collection.comparison_original_ids(symbols,'2025-10-01','2025-10-31')
        self.assertEqual(len(chosen),2)
        with store.db(readonly=True) as c:
            inputs=[assets.record_store.load(c,identifier) for identifier in chosen]
        for record in inputs:
            assets.publish('announcements',[record|{'text':'【PDF 第 1 页】\n公司2025年第三季度报告\n重要内容提示\n原文正文'}],
                           'cninfo_text')
        retention.cleanup()
        with store.db(readonly=True) as c:
            rows=c.execute("SELECT er.owner_id,a.title,a.text FROM evidence_refs er "
                           "JOIN announcements a ON a.fact_id=er.fact_id WHERE er.owner_type='financial_pending'").fetchall()
        bodies=[row for row in rows if row['text']]
        self.assertEqual(len(bodies),2)
        self.assertEqual({row['owner_id'] for row in bodies},{symbol+':2025-09-30' for symbol in symbols})
        self.assertEqual({row['title'] for row in bodies},set(titles))


if __name__=='__main__':
    unittest.main()
