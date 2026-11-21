"""Typed discovery history retains its old result with fewer bounded SQL reads."""
from __future__ import annotations

from datetime import date, timedelta
import math
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from foundation import assets, database as store, record_store
from research import discovery
from tools.mysql_instance import create_isolated_database, drop_isolated_database

SCHEMA = 'stock_test_discovery_history_' + uuid.uuid4().hex[:8]
KNOWN = '2026-11-04T19:00:00+08:00'
_temp = None
_env = None


def setUpModule():
    global _temp, _env
    _temp = tempfile.TemporaryDirectory(prefix='stock-discovery-history-')
    _env = patch.dict(os.environ, {'STOCK_DATA_ROOT':_temp.name,
                                  'STOCK_MYSQL_DATABASE':SCHEMA,'STOCK_DISABLE_WORKER':'1'})
    _env.start()
    create_isolated_database(SCHEMA)


def tearDownModule():
    try:
        store.close_pool()
        drop_isolated_database(SCHEMA)
    finally:
        _env.stop()
        _temp.cleanup()


def legacy_history_rows(conn, symbols, first_day, last_day, *, pointed):
    """The pre-optimization metadata + hydration reader, for parity checks."""
    scope = ','.join(['%s']*len(symbols))
    positions = ('symbol','day','record_key') if pointed else ('day','symbol','record_key')
    index = 'head_symbol' if pointed else 'PRIMARY'
    ordering = ','.join('h.'+key for key in positions)
    after = None
    while True:
        terms = [];values = []
        if after is not None:
            for offset, position in enumerate(positions):
                terms.append('('+' AND '.join([f'h.{key}=%s' for key in positions[:offset]]+
                                             [f'h.{position}>%s'])+')')
                values.extend(after[:offset+1])
        continuation = ' AND ('+' OR '.join(terms)+')' if terms else ''
        rows = conn.execute(
            f'SELECT h.symbol,h.day,h.record_key,f.id,f.first_seen FROM heads h FORCE INDEX ({index}) '
            'JOIN facts f ON f.id=h.fact_id '
            f"WHERE h.dataset='daily' AND h.day BETWEEN %s AND %s AND h.symbol IN ({scope})"
            +continuation+f' ORDER BY {ordering} LIMIT 1000',
            (first_day,last_day,*symbols,*values)).fetchall()
        yield from record_store.hydrate(conn, rows)
        if len(rows)<1000:
            break
        after = tuple(rows[-1][key] for key in positions)


class DiscoveryHistoryMySQL(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.days=[]
        day=date(2026,8,3)
        while len(cls.days)<65:
            if day.weekday()<5:
                cls.days.append(day.isoformat())
            day+=timedelta(days=1)
        cls.symbols=[f'sh.{600000+i:06d}' for i in range(80)]
        momentum=[10]*60+[10.10,10.20,10.30,10.38,10.60]
        contraction=[10]*59+[10.20,10.21,10.20,10.23,10.25,10.28]
        recovery=[10]*60+[9.80,9.85,10.05,10.12,10.16]
        samples=[momentum,contraction,recovery,momentum]
        bars=[]
        for stock,symbol in enumerate(cls.symbols):
            kind=stock%4
            closes=samples[kind]
            for i,day in enumerate(cls.days):
                close=closes[i]
                previous=closes[i-1] if i else close
                volume=50 if kind==1 and i>=59 else 200 if kind==3 and i==64 else 100
                bars.append({'day':day,'symbol':symbol,'open':previous,
                             'high':10.4 if kind==1 and i==49 else max(close,previous)+.02,
                             'low':min(close,previous)-.02,'close':close,'preclose':previous,
                             'volume':volume,'amount':10000+stock,'adjust':'raw',
                             'suspended':False,'is_st':False,'volume_unit':'shares'})
        # Exercise integer precision, absent vs unknown fields, and signed zero.
        bars[0].update(amount=2**53+17,pe=None,turnover=-0.0)
        with patch.object(store,'now',return_value=KNOWN):
            assets.publish('calendar',[{'day':day,'symbol':'*','trading':True} for day in cls.days],'bs_calendar')
            assets.publish('basics',[{'day':cls.days[-1],'symbol':symbol,'key':'universe',
                                      'name':'隔离样本'+symbol,'status':'1','board':'主板'}
                                     for symbol in cls.symbols],'bs_universe')
            assets.publish('daily',bars,'bs_daily_all')
        # Current ingestion omits None fields; retained historical typed rows may
        # explicitly mark them present. Exercise that supported storage shape.
        with store.db() as conn:
            fact=conn.execute("SELECT fact_id FROM heads WHERE dataset='daily' AND day=%s AND symbol=%s LIMIT 1",
                              (cls.days[0],cls.symbols[0])).fetchone()[0]
            value=record_store.load(conn,fact)
            value['pe']=None
            conn.execute('DELETE FROM daily_bars WHERE fact_id=%s',(fact,))
            record_store.save(conn,fact,'daily',value)
            conn.execute('UPDATE facts SET payload_bytes=%s WHERE id=%s',(len(store.dumps(value).encode()),fact))

    def tearDown(self):
        discovery._cache.clear()

    def test_full_snapshot_fingerprints_classifications_and_coverage_match(self):
        # 80 * 65 = 5200 rows crosses the optimized reader's bounded batch.
        for pointed in (False,True):
            with self.subTest(pointed=pointed), patch.object(store,'now',return_value=KNOWN):
                args={'symbols':self.symbols} if pointed else {}
                discovery._cache.clear()
                with patch.object(discovery,'_history_rows',side_effect=legacy_history_rows):
                    before=discovery.snapshot(self.days[-1],**args)
                discovery._cache.clear()
                after=discovery.snapshot(self.days[-1],**args)
                self.assertEqual(after['input_hash'],before['input_hash'])
                self.assertEqual(after,before)
                self.assertEqual(after['market']['with_60_bars'],80)
                self.assertEqual(after['market']['rise_counts'],dict.fromkeys(('strong','setup','early','breakout'),20))

    def test_typed_history_preserves_values_and_uses_one_read_per_batch(self):
        class CountedConnection:
            def __init__(self,conn):self.conn=conn;self.calls=0
            def execute(self,*args,**kwargs):
                self.calls+=1
                return self.conn.execute(*args,**kwargs)
        with store.db(readonly=True) as conn:
            legacy=CountedConnection(conn)
            expected=list(legacy_history_rows(legacy,self.symbols,self.days[0],self.days[-1],pointed=False))
            joined=CountedConnection(conn)
            with patch.object(record_store,'hydrate',side_effect=AssertionError('history must not reread generic payloads')):
                actual=list(discovery._history_rows(joined,self.symbols,self.days[0],self.days[-1],pointed=False))
        self.assertEqual([(row['id'],row['payload']) for row in actual],[(row['id'],row['payload']) for row in expected])
        self.assertEqual(len(actual),5200)
        self.assertLess(joined.calls,legacy.calls)
        first=actual[0]['payload']
        self.assertEqual(first['amount'],2**53+17)
        self.assertIs(type(first['amount']),int)
        self.assertIn('pe',first)
        self.assertIsNone(first['pe'])
        self.assertNotIn('pb',first)
        self.assertEqual(math.copysign(1,first['turnover']),-1)
        self.assertEqual((legacy.calls,joined.calls),(28,2))

    def test_missing_typed_bar_still_fails_instead_of_disappearing(self):
        with store.db(admin=True) as conn:
            fact=conn.execute("SELECT fact_id FROM heads WHERE dataset='daily' AND day=%s AND symbol=%s LIMIT 1",
                              (self.days[0],self.symbols[0])).fetchone()[0]
            try:
                conn.execute('DELETE FROM daily_bars WHERE fact_id=%s',(fact,))
                with self.assertRaisesRegex(RuntimeError,'Typed daily record is missing'):
                    list(discovery._history_rows(conn,[self.symbols[0]],self.days[0],self.days[0],pointed=True))
            finally:
                conn.raw.rollback()