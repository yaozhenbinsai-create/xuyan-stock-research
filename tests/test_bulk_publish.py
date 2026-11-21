"""Real MySQL regression checks for atomic, batched market-data publication."""
import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

import pymysql.cursors

from foundation import assets, database as store, record_store, retention
from tools.mysql_instance import create_isolated_database, drop_isolated_database


def quotes(count=200):
    return [{'day':'2026-11-16', 'symbol':f'sh.{600000+i}', 'key':'15:00',
             'source_time':'2026-11-16T15:00:00+08:00', 'close':10.1,
             'open':10., 'high':10.2, 'low':9.9, 'preclose':10.,
             'volume':100000, 'amount':1000000.} for i in range(count)]


class BulkPublishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = 'stock_test_bulk_publish_'+uuid.uuid4().hex[:10]
        cls.temp = tempfile.TemporaryDirectory(prefix='stock-bulk-publish-')
        cls.env = patch.dict(os.environ, {'STOCK_DATA_ROOT':cls.temp.name,
            'STOCK_MYSQL_DATABASE':cls.schema, 'STOCK_DISABLE_WORKER':'1'})
        cls.env.start()
        try:
            create_isolated_database(cls.schema)
        except BaseException:
            cls.env.stop(); cls.temp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            drop_isolated_database(cls.schema)
        finally:
            cls.env.stop(); cls.temp.cleanup()

    def setUp(self):
        self.assertTrue(os.environ['STOCK_MYSQL_DATABASE'].startswith('stock_test_'))
        self.assertEqual(os.environ['STOCK_DISABLE_WORKER'], '1')
        with store.db(admin=True) as conn:
            for table in ('heads','facts','asset_stats','dataset_usage','settings','collection_holds'):
                conn.execute('DELETE FROM `'+table+'`')
        retention._cache.clear()

    def test_200_quotes_use_one_actual_insert_per_table(self):
        queries=[]
        original=pymysql.cursors.Cursor._query
        def query(cursor, sql):
            queries.append(sql.decode(errors='replace') if isinstance(sql, (bytes,bytearray)) else sql)
            return original(cursor, sql)
        started=time.perf_counter()
        with patch.object(pymysql.cursors.Cursor, '_query', query):
            result=assets.publish('quotes', quotes(), 'tx_quotes')
        elapsed=time.perf_counter()-started
        self.assertEqual(result, {'received':200,'added':200,'unchanged':0,'held':0})
        for prefix in ('INSERT INTO facts(', 'INSERT INTO `quote_snapshots`', 'INSERT INTO heads '):
            self.assertEqual(sum(sql.startswith(prefix) for sql in queries), 1, prefix)
        with store.db(readonly=True) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM quote_snapshots').fetchone()[0],200)
            self.assertEqual(conn.execute("SELECT records FROM dataset_usage WHERE dataset='quotes'").fetchone()[0],200)
        output=store.ROOT/'output/运行核查-20261116'
        if output.is_dir():
            (output/'save-profile-after.json').write_text(json.dumps(
                {'isolated_schema':self.schema,'rows':200,'seconds':elapsed,
                 'actual_sql_statements':len(queries),'result':result},indent=2),encoding='utf-8')

    def test_duplicate_and_revision_order_keep_acquisition_counts(self):
        a=quotes(1)[0]; b={**a,'close':10.15}
        self.assertEqual(assets.publish('quotes',[a,b,a],'tx_quotes'),
                         {'received':3,'added':2,'unchanged':1,'held':0})
        self.assertEqual(assets.publish('quotes',[a,a],'tx_quotes')['unchanged'],2)
        with store.db(readonly=True) as conn:
            rows=conn.execute('SELECT f.id,f.acquisitions,q.close FROM facts f '
                              'JOIN quote_snapshots q ON q.fact_id=f.id ORDER BY q.close').fetchall()
            self.assertEqual([(r['acquisitions'],r['close']) for r in rows],[(4,10.1),(1,10.15)])
            self.assertEqual(conn.execute('SELECT fact_id FROM heads').fetchone()[0],rows[0]['id'])
            self.assertEqual(conn.execute("SELECT version_count,record_count FROM asset_stats WHERE dataset='quotes'").fetchone()._values,(2,1))

    def test_duplicate_other_source_keeps_historical_typed_shape(self):
        value={k:v for k,v in quotes(1)[0].items() if k!='source_time'}
        assets.publish('daily',[value],'bs_daily_all')
        with store.db() as conn:
            fid=conn.execute('SELECT id FROM facts').fetchone()[0]
            original=record_store.load(conn,fid)
            record_store.save(conn,fid,'daily',{**original,'pe':None,'pb':None})
        result=assets.publish('daily',[value,value],'tx_daily')
        self.assertEqual(result['added'],0)
        self.assertEqual(result['unchanged'],2)
        with store.db(readonly=True) as conn:
            self.assertEqual(record_store.load(conn,fid),{**original,'pe':None,'pb':None})
            self.assertEqual(conn.execute('SELECT acquisitions FROM facts').fetchone()[0],3)
            self.assertEqual({r[0] for r in conn.execute('SELECT source FROM fact_origins')},{'tx_daily','bs_daily_all'})

    def test_automatic_holds_and_manual_publication(self):
        values=quotes(3)
        values[2]['day']='2026-11-17'; values[2]['source_time']='2026-11-17T15:00:00+08:00'
        with store.db() as conn:
            conn.execute("INSERT INTO collection_holds VALUES('quotes','2026-11-16','sh.600000')")
            conn.execute("INSERT INTO collection_holds VALUES('quotes','2026-11-17','*')")
        self.assertEqual(assets.publish('quotes',values,'tx_quotes'),
                         {'received':3,'added':1,'unchanged':0,'held':2})
        self.assertEqual(assets.publish('quotes',values,'tx_quotes',automatic=False),
                         {'received':3,'added':2,'unchanged':1,'held':0})

    def test_held_invalid_record_does_not_reject_the_remaining_valid_scope(self):
        values=quotes(2)
        values[0]['close']=None
        with store.db() as conn:
            conn.execute("INSERT INTO collection_holds VALUES('quotes','2026-11-16','sh.600000')")
        self.assertEqual(assets.publish('quotes',values,'tx_quotes'),
                         {'received':2,'added':1,'unchanged':0,'held':1})
        with self.assertRaises(ValueError):
            assets.publish('quotes',values,'tx_quotes',automatic=False)

    def test_invalid_batch_and_missing_typed_row_do_not_partially_commit(self):
        values=quotes(2)
        with self.assertRaises(ValueError):
            assets.publish('quotes',[values[0],{**values[1],'close':-1}],'tx_quotes')
        with store.db(readonly=True) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM facts').fetchone()[0],0)
        assets.publish('quotes',[values[0]],'tx_quotes')
        with store.db() as conn:
            conn.execute('DELETE FROM quote_snapshots')
        with self.assertRaisesRegex(RuntimeError,'业务表不一致'):
            assets.publish('quotes',values,'tx_quotes')
        with store.db(readonly=True) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*),SUM(acquisitions) FROM facts').fetchone()._values,(1,1))

    def test_concurrent_duplicate_publishers_count_only_their_own_new_rows(self):
        values=quotes()
        barrier=threading.Barrier(2)
        def publish():
            barrier.wait(timeout=10)
            return assets.publish('quotes',values,'tx_quotes')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(publish) for _ in range(2)]
            results=[future.result(timeout=90) for future in futures]
        self.assertEqual(sorted(result['added'] for result in results),[0,200])
        with store.db(readonly=True) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*),MIN(acquisitions),MAX(acquisitions) FROM facts').fetchone()._values,(200,2,2))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM quote_snapshots').fetchone()[0],200)
            self.assertEqual(conn.execute("SELECT records FROM dataset_usage WHERE dataset='quotes'").fetchone()[0],200)

    def test_whole_market_batch_survives_driver_and_lookup_chunk_boundaries(self):
        values=quotes(5216)
        result=assets.publish('quotes',values,'tx_quotes')
        self.assertEqual(result['added'],5216)
        again=assets.publish('quotes',values,'tx_quotes')
        self.assertEqual((again['added'],again['unchanged']),(0,5216))
        with store.db(readonly=True) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM quote_snapshots').fetchone()[0],5216)
            self.assertEqual(conn.execute('SELECT MIN(acquisitions),MAX(acquisitions) FROM facts').fetchone()._values,(2,2))

    def test_whole_market_facts_and_heads_limit_application_batches_to_200(self):
        values=[{key:value for key,value in item.items() if key not in ('key','source_time')}
                for item in quotes(5216)]
        batches={'facts':[],'heads':[]}
        original=store.Connection.executemany
        def write(conn,sql,parameters):
            rows=list(parameters)
            for table,prefix in (('facts','INSERT INTO facts('),('heads','INSERT INTO heads ')):
                if sql.startswith(prefix):batches[table].append(len(rows))
            return original(conn,sql,rows)
        with patch.object(store.Connection,'executemany',write):
            result=assets.publish('daily',values,'bs_daily_all')
        self.assertEqual(result,{'received':5216,'added':5216,'unchanged':0,'held':0})
        for table,sizes in batches.items():
            self.assertTrue(sizes,table)
            self.assertTrue(all(0<size<=200 for size in sizes),(table,sizes))
            self.assertEqual(sum(sizes),5216,table)
        with store.db(readonly=True) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM facts').fetchone()[0],5216)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM daily_bars').fetchone()[0],5216)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM heads h JOIN facts f ON f.id=h.fact_id "
                "JOIN daily_bars d ON d.fact_id=f.id WHERE h.dataset='daily' AND f.dataset=h.dataset "
                "AND f.day=h.day AND f.symbol=h.symbol AND f.record_key=h.record_key").fetchone()[0],5216)
            self.assertEqual(conn.execute("SELECT records FROM dataset_usage WHERE dataset='daily'").fetchone()[0],5216)

    def test_second_heads_chunk_error_rolls_back_all_new_records_and_usage(self):
        values=[{key:value for key,value in item.items() if key not in ('key','source_time')}
                for item in quotes(401)]
        baseline={**values[0],'symbol':'sh.688888'}
        assets.publish('daily',[baseline],'bs_daily_all')
        def state(conn):
            return {'facts':conn.execute('SELECT COUNT(*) FROM facts').fetchone()[0],
                    'typed':conn.execute('SELECT COUNT(*) FROM daily_bars').fetchone()[0],
                    'heads':conn.execute('SELECT COUNT(*) FROM heads').fetchone()[0],
                    'usage':dict(conn.execute("SELECT * FROM dataset_usage WHERE dataset='daily'").fetchone()),
                    'stats':dict(conn.execute("SELECT * FROM asset_stats WHERE dataset='daily'").fetchone()),
                    'generation':store.setting('generation_daily',0,conn=conn)}
        with store.db(readonly=True) as conn:before=state(conn)
        self.assertEqual((before['facts'],before['typed'],before['heads']),(1,1,1))
        original=store.Connection.executemany
        head_calls=[];uncommitted={}
        def write(conn,sql,parameters):
            rows=list(parameters)
            if sql.startswith('INSERT INTO heads '):
                head_calls.append(len(rows))
                if len(head_calls)==2:
                    uncommitted.update(state(conn))
                    raise pymysql.OperationalError(2013,'synthetic second-heads-chunk failure')
            return original(conn,sql,rows)
        with patch.object(store.Connection,'executemany',write):
            with self.assertRaisesRegex(pymysql.OperationalError,'synthetic second-heads-chunk failure'):
                assets.publish('daily',values,'bs_daily_all')
        self.assertEqual(head_calls,[200,200])
        self.assertEqual((uncommitted['facts'],uncommitted['typed'],uncommitted['heads']),(402,402,201))
        with store.db(readonly=True) as conn:
            self.assertEqual(state(conn),before)
            self.assertEqual(conn.execute('SELECT symbol FROM heads').fetchone()[0],baseline['symbol'])

    def test_cross_chunk_duplicates_and_revisions_keep_counts_heads_and_generation(self):
        a=quotes(1)[0];b={**a,'close':10.15}
        first=assets.publish('quotes',[a]*201,'tx_quotes')
        self.assertEqual(first,{'received':201,'added':1,'unchanged':200,'held':0})
        with store.db(readonly=True) as conn:
            original=conn.execute('SELECT id,acquisitions FROM facts').fetchone()
            self.assertEqual(original['acquisitions'],201)
            self.assertEqual(conn.execute('SELECT fact_id FROM heads').fetchone()[0],original['id'])
            self.assertEqual(store.setting('generation_quotes',0,conn=conn),1)
            stats=conn.execute("SELECT record_count,version_count,generation FROM asset_stats WHERE dataset='quotes'").fetchone()
            self.assertIsNotNone(stats)
            self.assertEqual(stats._values,(1,1,1))
        second=assets.publish('quotes',[a]*200+[b,a],'tx_quotes')
        self.assertEqual(second,{'received':202,'added':1,'unchanged':201,'held':0})
        with store.db(readonly=True) as conn:
            rows=conn.execute('SELECT f.id,f.acquisitions,q.close FROM facts f '
                              'JOIN quote_snapshots q ON q.fact_id=f.id ORDER BY q.close').fetchall()
            self.assertEqual([(row['acquisitions'],row['close']) for row in rows],[(402,10.1),(1,10.15)])
            self.assertEqual(conn.execute('SELECT fact_id FROM heads').fetchone()[0],original['id'])
            self.assertEqual(store.setting('generation_quotes',0,conn=conn),2)
            self.assertEqual(conn.execute("SELECT record_count,version_count,generation FROM asset_stats WHERE dataset='quotes'").fetchone()._values,(1,2,2))
            self.assertEqual(conn.execute("SELECT records FROM dataset_usage WHERE dataset='quotes'").fetchone()[0],2)


if __name__=='__main__':
    unittest.main()
