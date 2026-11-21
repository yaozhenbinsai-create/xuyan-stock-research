"""Research reads exercise typed storage with an isolated MySQL database."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from foundation import assets, database as store
from research import discovery, evidence, history_fill, market, originals, outlook, preparation
from tools.mysql_instance import create_isolated_database, drop_isolated_database


SCHEMA = 'stock_test_relread_' + uuid.uuid4().hex[:10]
DAY = '2026-11-04'
SYMBOL = 'sh.600000'


def setUpModule():
    create_isolated_database(SCHEMA)


def tearDownModule():
    store.close_pool()
    drop_isolated_database(SCHEMA)


def bar(day=DAY, close=10, **extra):
    return {'day':day,'symbol':SYMBOL,'open':10,'high':12,'low':9,'close':close,
            'preclose':10,'volume':100,'amount':1000,'adjust':'raw',
            'suspended':False,'volume_unit':'shares',**extra}


class RelationalResearchReads(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='stock-relational-read-')
        self.env = patch.dict(os.environ, {'STOCK_DATA_ROOT':self.temp.name,
                                          'STOCK_MYSQL_DATABASE':SCHEMA,
                                          'STOCK_DISABLE_WORKER':'1'})
        self.env.start()
        with store.db(admin=True) as conn:
            self.assertTrue(SCHEMA.startswith('stock_test_'))
            conn.execute('DELETE FROM evidence_refs')
            conn.execute('DELETE FROM fact_origins')
            conn.execute('DELETE FROM heads')
            conn.execute('DELETE FROM facts')
            conn.execute('DELETE FROM settings')
        discovery._cache.clear()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def insert(self, dataset, value, source, known='2026-11-04T11:00:00+08:00'):
        with store.db() as conn:
            return assets.insert_fact(conn,dataset,source,value,observed=known)[0]

    def test_quote_cutoff_and_knowledge_retain_the_original_revision(self):
        quote = bar(key='112900',source_time=DAY+'T11:29:00+08:00')
        first = self.insert('quotes',quote,'tx_quotes',DAY+'T11:29:20+08:00')
        second = self.insert('quotes',{**quote,'close':11},'tx_quotes',DAY+'T11:31:00+08:00')
        with store.db(readonly=True) as conn:
            frozen = market.latest_quotes(conn,DAY,DAY+'T11:30:00+08:00',DAY+'T11:30:00+08:00',300)
            later = market.latest_quotes(conn,DAY,DAY+'T03:30:00+00:00',DAY+'T11:32:00+08:00',300)
        self.assertEqual((frozen[SYMBOL]['_id'],frozen[SYMBOL]['close']),(first,10))
        self.assertEqual((later[SYMBOL]['_id'],later[SYMBOL]['close']),(second,11))
        self.assertEqual(frozen[SYMBOL]['source_time'],quote['source_time'])

    def test_frozen_price_reference_does_not_follow_a_revised_head(self):
        first = self.insert('daily',bar(),'bs_daily_all')
        second = self.insert('daily',bar(close=11),'bs_daily_all')
        with store.db(readonly=True) as conn:
            frozen = market.frozen_bars(SYMBOL,[first],1,conn)
            current = market.closing_bar(conn,SYMBOL,DAY)
            mapped = discovery._map(conn,'daily',DAY)
        self.assertEqual(frozen[0]['close'],10)
        self.assertEqual((current['fact_id'],current['close']),(second,11))
        self.assertEqual(mapped[SYMBOL]['fact_id'],second)
        self.assertEqual(mapped[SYMBOL]['close'],11)

    def test_calendar_and_comparability_use_typed_trading_and_price_fields(self):
        days=['2026-11-02','2026-11-03','2026-11-04']
        for day in days:
            self.insert('calendar',{'day':day,'symbol':'*','trading':True},'bs_calendar')
            self.insert('daily',bar(day=day),'bs_daily_all')
        self.insert('calendar',{'day':'2026-11-01','symbol':'*','trading':False},'bs_calendar')
        with store.db(readonly=True) as conn:
            self.assertEqual(outlook.trading_dates(conn,'2026-11-01',DAY),days)
            self.assertTrue(market.comparable_window(conn,SYMBOL,{'at':days[0]+'T15:00:00+08:00'},DAY))
        self.insert('daily',bar(day=days[1],preclose=8),'bs_daily_all')
        with store.db(readonly=True) as conn:
            self.assertFalse(market.comparable_window(conn,SYMBOL,{'at':days[0]+'T15:00:00+08:00'},DAY))

    def test_evidence_uses_bounded_records_and_preserves_financial_source_values(self):
        financial={'day':'2026-10-30','symbol':SYMBOL,'key':'profit:2026-09-30',
                   'published':'2026-10-30','report_date':'2026-09-30','report_type':'profit',
                   'fields':{'netProfit':'123.400','MBRevenue':'','CFOToOR':None}}
        self.insert('financials',financial,'bs_profit')
        notice={'day':DAY,'symbol':SYMBOL,'key':'risk','title':'关于收到监管措施的公告',
                'published':DAY+'T10:00:00+08:00','url':'https://example.invalid/risk.pdf',
                'text':'原始正文。'*400}
        self.insert('announcements',notice,'cninfo_text')
        with store.db(readonly=True) as conn:
            documents, limits=evidence.collect(conn,DAY,DAY+'T11:30:00+08:00',True,[SYMBOL])
        self.assertEqual(limits,[])
        self.assertEqual(documents[SYMBOL]['financials'][0]['fields'],financial['fields'])
        self.assertEqual(len(documents[SYMBOL]['announcements'][0]['text']),1200)

    def test_original_title_filter_and_preparation_keep_the_same_source_documents(self):
        notice={'day':DAY,'symbol':SYMBOL,'key':'filing','title':'2026年第三季度报告',
                'published':DAY+'T09:00:00+08:00',
                'url':'https://static.cninfo.com.cn/finalpage/test.pdf'}
        identifier=self.insert('announcements',notice,'cninfo_announcements')
        self.assertEqual(preparation._original_ids([SYMBOL],DAY,DAY),[identifier])
        extracted={**notice,'text':'【PDF 第 1 页】营业收入 100万元。'}
        full=self.insert('announcements',extracted,'cninfo_text')
        with store.db(readonly=True) as conn:
            rows=originals._original_filings(conn,SYMBOL,DAY+'T11:30:00+08:00',DAY+'T11:30:00+08:00')
        self.assertEqual([row['id'] for row in rows],[full])
        self.assertEqual(rows[0]['text'],extracted['text'])
        self.assertEqual(preparation._original_ids([SYMBOL],DAY,DAY),[])

    def test_history_fill_filters_suspended_and_keeps_valid_trading_bars(self):
        first=datetime.fromisoformat('2026-10-01')
        for offset in range(10):
            day=(first+timedelta(days=offset)).date().isoformat()
            self.insert('daily',bar(day=day,suspended=offset==5),'bs_daily_all')
        history={SYMBOL:deque(maxlen=65)}
        with store.db(readonly=True) as conn:
            result=history_fill.expand(conn,history,'2026-10-10',DAY+'T11:30:00+08:00',minimum=8,maximum=9)
        self.assertEqual(result['recovered'],1)
        self.assertEqual(len(history[SYMBOL]),9)
        self.assertNotIn('2026-10-06',[row['day'] for row in history[SYMBOL]])

    def test_new_risk_after_a_check_blocks_the_old_verified_result(self):
        notice={'day':DAY,'symbol':SYMBOL,'key':'old','title':'日常公告',
                'published':DAY+'T09:00:00+08:00','url':'https://example.invalid/old.pdf','text':'已核正文'}
        identifier=self.insert('announcements',notice,'cninfo_text')
        with store.db() as conn:
            store.set_setting(f'research_check:{SYMBOL}:risk',
                              {'state':'verified','fact_ids':[identifier],
                               'confirmed_at':DAY+'T11:05:00+08:00','valid_until':DAY},conn=conn)
        self.insert('announcements',{**notice,'key':'new','title':'关于被立案调查的公告'},
                    'cninfo_text',DAY+'T11:10:00+08:00')
        with store.db(readonly=True) as conn:
            checks=evidence.read_checks(conn,SYMBOL,DAY+'T11:30:00+08:00')
        self.assertEqual(checks['risk']['state'],'blocked')

    def test_discovery_crosses_the_history_batch_boundary_without_losing_symbols(self):
        days=[]
        day=datetime.fromisoformat('2026-08-03')
        while len(days)<65:
            if day.weekday()<5:
                days.append(day.date().isoformat())
            day+=timedelta(days=1)
        symbols=[f'sh.{600000+index:06d}' for index in range(18)]
        assets.publish('calendar',[{'day':day,'symbol':'*','trading':True} for day in days],'bs_calendar')
        assets.publish('basics',[{'day':days[-1],'symbol':symbol,'key':'universe',
                                  'name':'隔离样本'+symbol,'status':'1','board':'主板'}
                                 for symbol in symbols],'bs_universe')
        bars=[]
        for index,day in enumerate(days):
            previous=10+max(0,index-1)*.02
            close=10+index*.02
            for symbol in symbols:
                bars.append({'day':day,'symbol':symbol,'open':previous,'high':close+.1,
                             'low':previous-.1,'close':close,'preclose':previous,
                             'volume':100,'amount':1000,'is_st':False,'suspended':False,
                             'adjust':'raw','volume_unit':'shares'})
        assets.publish('daily',bars,'bs_daily_all')
        # 18 * 65 = 1170 records exceeds the reader's 1000-row SQL batch.
        broad=discovery.snapshot(days[-1])
        pointed=discovery.snapshot(days[-1],symbols=symbols)
        self.assertEqual(broad['market']['with_60_bars'],len(symbols))
        self.assertEqual(pointed['market']['with_60_bars'],len(symbols))
        self.assertEqual(broad['market']['advancing'],len(symbols))
        self.assertEqual(pointed['market']['advancing'],len(symbols))

