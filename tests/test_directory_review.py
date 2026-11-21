"""Synthetic in-memory SQL tests; never connects to the project MySQL."""
import importlib.util
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


directory = load('review_directory', ROOT/'foundation/directory.py')
sys.modules['foundation.directory'] = directory
sources = load('review_sources', ROOT/'foundation/sources.py')
sessions = load('review_sessions', ROOT/'research/sessions.py')
recaps = load('review_recaps', ROOT/'research/recaps.py')


class MemorySQL:
    def __init__(self):
        self.raw = sqlite3.connect(':memory:')
        self.raw.row_factory = sqlite3.Row
        self.raw.executescript('''
            CREATE TABLE facts(id INTEGER PRIMARY KEY,dataset TEXT,day TEXT,symbol TEXT,record_key TEXT,
              source TEXT,first_seen TEXT,last_seen TEXT);
            CREATE TABLE fact_origins(fact_id INTEGER,source TEXT,first_seen TEXT,last_seen TEXT);
            CREATE TABLE heads(dataset TEXT,day TEXT,symbol TEXT,record_key TEXT,fact_id INTEGER);
            CREATE TABLE security_records(fact_id INTEGER PRIMARY KEY,security_id INTEGER);
            CREATE TABLE security_details(id INTEGER PRIMARY KEY,name TEXT,listed TEXT,delisted TEXT,
              status TEXT,board TEXT,present_fields INTEGER,integer_fields INTEGER,
              empty_fields INTEGER,negative_zero_fields INTEGER);
        ''')

    def execute(self, sql, params=()):
        sql = re.sub(r' FORCE INDEX \(fact_identity\)', '', sql)
        return self.raw.execute(sql.replace('%s', '?'), params)

    def fact(self, fid, symbol, *, day='2026-11-12', source='bs_universe', key='',
             known='2026-11-12T17:30:00+08:00', status='1', head=True):
        self.execute('INSERT INTO facts VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                     (fid,'basics',day,symbol,key,source,known,known))
        from foundation.record_store import BITS
        present=BITS['security_details']['name']|BITS['security_details']['status']
        self.execute('INSERT INTO security_details(id,name,status,present_fields,integer_fields,empty_fields,negative_zero_fields) '
                     'VALUES(%s,%s,%s,%s,0,0,0)',(fid,str(fid),status,present))
        self.execute('INSERT INTO security_records VALUES(%s,%s)',(fid,fid))
        if head:
            self.execute('DELETE FROM heads WHERE day=%s AND symbol=%s AND record_key=%s',(day,symbol,key))
            self.execute('INSERT INTO heads VALUES(%s,%s,%s,%s,%s)',('basics',day,symbol,key,fid))


class DirectoryReviewTests(unittest.TestCase):
    def setUp(self):
        self.c = MemorySQL()
        self.c.fact(1,'sh.600001')
        self.c.fact(2,'sz.000001')

    def tearDown(self):
        self.c.raw.close()

    def test_two_details_today_cannot_shrink_prior_market_roster(self):
        for i,s in enumerate(('sh.600001','sz.000002'),10):
            self.c.fact(i,s,day='2026-11-13',source='bs_basics',key='detail',known='2026-11-13T12:00:00+08:00')
        # The old MAX(basics day) chooses the two-detail date.
        self.assertEqual(self.c.execute("SELECT MAX(day) FROM heads WHERE dataset='basics'").fetchone()[0],'2026-11-13')
        day,rows=directory.market_roster(self.c,'2026-11-13',known_at='2026-11-13T12:01:00+08:00')
        self.assertEqual(day,'2026-11-12')
        self.assertEqual(set(rows),{'sh.600001','sz.000001'})

    def test_legacy_detail_head_does_not_hide_retained_universe(self):
        self.c.fact(3,'sh.600001',source='bs_basics',known='2026-11-12T18:00:00+08:00',status='0')
        self.assertEqual(self.c.execute("SELECT fact_id FROM heads WHERE symbol='sh.600001'").fetchone()[0],3)
        _,rows=directory.market_roster(self.c,'2026-11-12',known_at='2026-11-12T19:00:00+08:00')
        self.assertEqual(rows['sh.600001']['fact_id'],1)
        self.assertEqual(rows['sh.600001']['status'],'1')

    def test_cross_source_origin_is_not_visible_before_its_own_first_seen(self):
        self.c.fact(3,'sh.600003',day='2026-11-13',source='bs_basics',known='2026-11-13T09:00:00+08:00')
        self.c.execute('INSERT INTO fact_origins VALUES(%s,%s,%s,%s)',(3,'bs_universe','2026-11-13T17:30:00+08:00','2026-11-13T17:30:00+08:00'))
        before,rows=directory.market_roster(self.c,'2026-11-13',known_at='2026-11-13T12:00:00+08:00')
        self.assertEqual(before,'2026-11-12')
        self.assertNotIn('sh.600003',rows)
        after,rows=directory.market_roster(self.c,'2026-11-13',known_at='2026-11-13T18:00:00+08:00')
        self.assertEqual(after,'2026-11-13')
        self.assertEqual(rows['sh.600003']['fact_id'],3)

    def test_same_day_universe_revision_uses_knowledge_time(self):
        self.c.fact(3,'sh.600001',known='2026-11-12T18:00:00+08:00',status='0')
        _,before=directory.market_roster(self.c,'2026-11-12',known_at='2026-11-12T17:45:00+08:00')
        _,after=directory.market_roster(self.c,'2026-11-12',known_at='2026-11-12T18:01:00+08:00')
        self.assertEqual(before['sh.600001']['fact_id'],1)
        self.assertEqual(after['sh.600001']['fact_id'],3)
        self.assertEqual(after['sh.600001']['status'],'0')

    def test_pointed_universe_is_not_a_full_market_census(self):
        self.c.fact(3,'sh.600001',day='2026-11-13',key='universe_partial',known='2026-11-13T12:00:00+08:00')
        day,rows=directory.market_roster(self.c,'2026-11-13',known_at='2026-11-13T12:01:00+08:00')
        self.assertEqual(day,'2026-11-12')
        self.assertEqual(len(rows),2)

    def test_current_universe_head_wins_when_canonical_value_returns(self):
        self.c.fact(3,'sh.600001',known='2026-11-12T18:00:00+08:00',status='0')
        self.c.execute('UPDATE heads SET fact_id=1 WHERE symbol=%s',('sh.600001',))
        self.c.execute('UPDATE facts SET last_seen=%s WHERE id=1',('2026-11-12T19:00:00+08:00',))
        _,before=directory.market_roster(self.c,'2026-11-12',known_at='2026-11-12T18:30:00+08:00')
        _,after=directory.market_roster(self.c,'2026-11-12',known_at='2026-11-12T19:01:00+08:00')
        self.assertEqual(before['sh.600001']['fact_id'],3)
        self.assertEqual(after['sh.600001']['fact_id'],1)

    def test_current_cross_source_head_checks_universe_last_seen(self):
        self.c.fact(3,'sh.600001',source='bs_basics',known='2026-11-12T09:00:00+08:00')
        self.c.execute('INSERT INTO fact_origins VALUES(%s,%s,%s,%s)',(3,'bs_universe','2026-11-12T10:00:00+08:00','2026-11-12T19:00:00+08:00'))
        _,before=directory.market_roster(self.c,'2026-11-12',known_at='2026-11-12T18:30:00+08:00')
        _,after=directory.market_roster(self.c,'2026-11-12',known_at='2026-11-12T19:01:00+08:00')
        self.assertEqual(before['sh.600001']['fact_id'],1)
        self.assertEqual(after['sh.600001']['fact_id'],3)

    def test_stale_origin_last_seen_cannot_hide_a_future_canonical_revisit(self):
        self.c.fact(3,'sh.600001',known='2026-11-12T18:00:00+08:00',status='0')
        self.c.execute('INSERT INTO fact_origins VALUES(%s,%s,%s,%s)',(1,'bs_universe','2026-11-12T17:30:00+08:00','2026-11-12T17:30:00+08:00'))
        self.c.execute('UPDATE heads SET fact_id=1 WHERE symbol=%s',('sh.600001',))
        self.c.execute('UPDATE facts SET last_seen=%s WHERE id=1',('2026-11-12T19:00:00+08:00',))
        _,rows=directory.market_roster(self.c,'2026-11-12',known_at='2026-11-12T18:30:00+08:00')
        self.assertEqual(rows['sh.600001']['fact_id'],3)

    def test_empty_or_over_boundary_roster_is_explicit(self):
        self.assertEqual(directory.market_roster(self.c,'2026-11-01',known_at='2026-11-13'),(None,{}))
        with self.assertRaisesRegex(Exception,'超出'):
            directory.market_roster(self.c,'2026-11-12',known_at='2026-11-13',limit=1)

    def test_source_keys_separate_details_full_roster_and_pointed_universe(self):
        rows=[{'code':'sh.600001','type':'1','code_name':'A','status':'1','tradeStatus':'1'},
              {'code':'sz.000001','type':'1','code_name':'B','status':'1','tradeStatus':'1'}]
        p={'end':'2026-11-12','symbols':[]}
        with patch.object(sources.ms,'today',return_value='2026-11-13'):
            details=sources.normalize_bs('basics',rows,{**p,'symbols':['sh.600001']})
            full=sources.normalize_bs('universe',rows,p)
            pointed=sources.normalize_bs('universe',rows,{**p,'symbols':['sh.600001']})
        self.assertEqual({x['key'] for x in details},{'detail'})
        self.assertEqual({x['key'] for x in full},{'universe'})
        self.assertEqual([(x['symbol'],x['key']) for x in pointed],[('sh.600001','universe_partial')])

    def test_full_market_capture_uses_same_roster_and_ignores_new_detail_day(self):
        self.c.fact(3,'sh.600099',day='2026-11-13',source='bs_basics',key='detail',known='2026-11-13T12:00:00+08:00')
        @contextmanager
        def db(**kwargs):
            yield self.c
        settings={'enabled':['noon'],'symbols':[],'quote_age_seconds':300}
        def setting(key,default=None,**kwargs):
            return False if key=='automation_paused' else {}
        with patch.object(sessions.store,'db',db),patch.object(sessions,'config',return_value=settings), \
             patch.object(sessions,'trading_day',return_value=True),patch.object(sessions.store,'setting',side_effect=setting):
            value=sessions.captures('2026-11-13T11:26:00+08:00')
        self.assertEqual(value,[('2026-11-13','noon',['sh.600001','sz.000001'])])

    def test_recap_joins_one_retained_universe_fact_per_symbol(self):
        class CaptureSQL:
            def execute(inner,sql,params):
                inner.sql,inner.params=sql,params
                return inner
            def fetchall(inner):return []
        c=CaptureSQL()
        with patch.object(recaps.security_directory,'market_roster',return_value=('2026-11-12',{
                'sh.600001':{'fact_id':1},'sz.000001':{'fact_id':2}})):
            self.assertEqual(recaps._groups(c,'2026-11-13',0.01,'basics'),[])
        self.assertEqual(c.params,('2026-11-13','2026-11-13',1,2))
        self.assertIn('g.symbol=p.symbol AND g.id IN (%s,%s)',c.sql)
        self.assertNotIn('JOIN heads hg',c.sql)


if __name__=='__main__':
    unittest.main()
