from datetime import timedelta
import json
import unittest
import uuid
from unittest.mock import patch

from fastapi import HTTPException

from foundation import assets, database as store
from research import contracts, discovery, feedback, learning, originals, portfolio, preferences, review
from tools.mysql_instance import create_isolated_database, drop_isolated_database
import test_research as fixtures


SCHEMA='stock_test_evidence_'+uuid.uuid4().hex[:8]
SYMBOL='sh.600000'


def setUpModule():
    create_isolated_database(SCHEMA)


def tearDownModule():
    drop_isolated_database(SCHEMA)


class EvidenceReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ResearchTests('test_publish_once_idempotency_and_evidence_pin')
        with patch.object(fixtures,'SCHEMA',SCHEMA):
            self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.day=self.fixture.day
        self.next_day=self.fixture.days[65]

    def policy(self):
        html=f'''<title>隔离政策原件</title><meta name="PubDate" content="{self.day}">
        <div id="UCAP-CONTENT"><p>{'推进储能设备和产业设备更新。' * 12}</p><p>自2026年10月1日起施行。</p></div>'''
        with patch.object(originals,'_download',return_value=html):
            first=originals.import_policy({'url':'https://www.gov.cn/zhengce/content/fixture.htm','terms':['储能设备']})
            duplicate=originals.import_policy({'url':'https://www.gov.cn/zhengce/content/fixture.htm?ref=x','terms':['储能设备']})
        self.assertEqual(first['fact_id'],duplicate['fact_id'])
        self.assertEqual(duplicate['added'],0)
        return first

    def test_filing_values_policy_terms_and_original_citations(self):
        original=self.policy()
        with self.assertRaises(HTTPException):
            originals.import_policy({'url':'https://www.gov.cn.other.invalid/fixture.htm','terms':['储能设备']})
        for key,amount in (('first',1300),('revision',1280)):
            assets.publish('announcements',[{'day':self.day,'symbol':SYMBOL,'key':key,'title':'2026年第一季度报告',
                          'published':self.day+'T11:00:00+08:00','url':'https://static.cninfo.com.cn/fixture.pdf',
                          'text':f'【PDF 第 5 页】\n单位：万元\n营业收入 {amount}\n主营业务为储能设备生产销售。'}], 'cninfo_text')
        bundle=discovery.snapshot(self.day,[SYMBOL]);p=bundle['candidates'][0]['originals']
        self.assertEqual(len(p['financial_fields']),2)
        self.assertEqual(p['comparisons'][0]['state'],'differs')
        self.assertEqual(p['industry_links'][0]['policy_id'],original['fact_id'])
        self.assertIn(original['fact_id'],bundle['candidates'][0]['evidence_ids'])
        with store.db(readonly=True) as conn:
            hydrated=originals.attach_quotes(conn,p)
        self.assertTrue(all(anchor['quote'] for anchor in hydrated['anchors']))
        report=fixtures.model_report(bundle)
        anchor=hydrated['anchors'][0]
        report['decisions'][0]['supports'][0]['fact_ids']=[anchor['fact_id']]
        with self.assertRaisesRegex(ValueError,'页段'):
            contracts.validate_report(store.dumps(report),bundle)
        report['decisions'][0]['supports'][0]['anchor_ids']=[anchor['id']]
        validated=contracts.validate_report(store.dumps(report),bundle)
        self.assertTrue(any('数字差异' in x for x in validated['decisions'][0]['formal_blockers']))

    def test_fees_t_plus_one_import_dedup_and_void(self):
        buy={'external_id':'buy','symbol':SYMBOL,'side':'buy','filled_at':self.day+'T10:00:00','quantity':100,'price':'10','fees':'5'}
        sell={**buy,'external_id':'sell','side':'sell','filled_at':self.next_day+'T09:45:00','quantity':40,'price':'12','fees':'2'}
        portfolio.record('actual',[buy])
        self.assertEqual(len(portfolio.record('actual',[buy])['repeated']),1)
        with self.assertRaises(HTTPException):
            portfolio.record('actual',[{**sell,'filled_at':self.day+'T14:00:00'}])
        self.assertEqual(len(portfolio.fills('actual')['items']),1)
        csv='编号,代码,方向,成交时间,股数,成交价,费用\n'+f"sell,{SYMBOL},卖出,{sell['filled_at']},40,12,2"
        inserted=portfolio.import_csv('actual',csv)['inserted'][0]
        actual=portfolio.positions('actual')['items'][0]
        self.assertEqual((actual['quantity'],actual['net_basis']),(60,'603.00'))
        self.assertEqual(portfolio.symbol_summary('actual',SYMBOL)['realized_net'],'76.00')
        self.assertEqual(portfolio.positions('paper')['items'],[])
        portfolio.record('paper',[{**buy,'fees':None},sell])
        self.assertIsNone(portfolio.symbol_summary('paper',SYMBOL)['realized_net'])
        portfolio.void('actual',inserted,'隔离账本更正')
        self.assertEqual(portfolio.positions('actual')['items'][0]['quantity'],100)

    def test_frozen_same_window_controls_and_benchmark(self):
        peers=['sh.600001','sh.600002','sh.600003']
        at=self.next_day+'T09:35:00+08:00'
        draft={'kind':'discovery','mode':'close_research','market_day':self.day,'method_version':learning.BASE['version'],
               'session':'close','published_at':self.day+'T18:30:00+08:00','horizons':{'D1':self.next_day,'D3':None},
               'decisions':[{'symbol':SYMBOL,'action':'reject'}]}
        with store.db() as conn:
            review.freeze_controls(conn,'a'*16,draft,{'candidates':[],'comparison_samples':[{'symbol':peers[0],'cohort':'outside'}]}, {})
        with patch.object(store,'now',return_value=self.next_day+'T16:00:00+08:00'):
            for symbol, close in [(SYMBOL,11),*[(s,10.2) for s in peers]]:
                prices=[('09:34:50',10),('09:35:20',5)]
                assets.publish('quotes',[{'day':self.next_day,'symbol':symbol,'key':clock,'source_time':self.next_day+'T'+clock+'+08:00',
                                'close':value,'open':value,'low':value,'high':value,'volume':100} for clock,value in prices],'tx_quotes')
                assets.publish('daily',[{'day':self.next_day,'symbol':symbol,'open':10,'close':close,'low':10,'high':close}],'bs_daily_all')
            with store.db() as conn:
                review.refresh(conn,store.now())
                result={'status':'complete','endpoint':self.next_day,'reference':{'at':at},'return':.1}
                review.benchmark(conn,result,peers,[],'fixture/D1')
        samples=review.page()['items']
        self.assertEqual({row['cohort'] for row in samples},{'rejected','outside'})
        self.assertTrue(all(row['status']=='complete' and row['data']['reference']['price']==10 for row in samples))
        self.assertAlmostEqual(result['benchmark_return'],.02)
        self.assertAlmostEqual(result['excess_return'],.08)

    def test_feedback_applies_only_to_current_confirmed_version(self):
        p=feedback.propose({'topic':'risk','note':'请把反证列在前面'})
        self.assertIsNone(preferences.current()['confirmed_at'])
        feedback.decide(p['id'],'accept',True)
        self.assertEqual(preferences.current()['risk'],'conservative')
        pending=feedback.propose({'topic':'shorter'})
        preferences.save({'confirm':True,'revoke':True})
        with self.assertRaises(HTTPException):
            feedback.decide(pending['id'],'accept',True)
        self.assertIsNone(preferences.current()['confirmed_at'])
        self.assertEqual(preferences.interpret('只看主板，最多3只，期限D1。'),{'boards':['主板'],'candidate_limit':3,'horizon':'D1'})

    def test_non_volume_experiment_release_uses_two_evidence_windows(self):
        entry=learning.create({'title':'提高收盘位置门槛','factor':'close_location_min','threshold':.75,
                               'train_start':self.fixture.days[0],'train_end':self.fixture.days[10],
                               'validation_end':self.fixture.days[20],'test_end':self.fixture.days[30]})
        self.assertFalse(learning.detail(entry['id'])['evaluation']['qualified'])
        rows=[{'day':d,'baseline':True,'candidate':i%2==0,'return':.08 if i%2==0 else -.05}
              for i,d in enumerate(self.fixture.days[:24])]
        evidence=learning._evaluation(entry,{'train':[],'validation':[],'test':rows,'shadow':rows},False)
        self.assertTrue(evidence['qualified'])
        self.assertFalse(learning._evaluation(entry,{'train':[],'validation':[],'test':rows,'shadow':[]},False)['qualified'])
        with patch.object(learning,'evaluate',return_value=evidence):
            learning.change(entry['id'],'publish',True)
        self.assertEqual(learning.active()['close_location_min'],.75)
        self.assertEqual(learning.rollback(True)['active'],learning.BASE)
