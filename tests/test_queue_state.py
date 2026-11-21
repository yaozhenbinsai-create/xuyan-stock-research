"""Collection scheduling uses typed columns; all mutations stay in a test schema."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from foundation import assets, collection, database as store, queue_state
from tools.mysql_instance import create_isolated_database, drop_isolated_database


class QueueStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = 'stock_test_queue_state_'+uuid.uuid4().hex[:10]
        cls.temp = tempfile.TemporaryDirectory(prefix='stock-queue-state-')
        cls.env = patch.dict(os.environ, {'STOCK_DATA_ROOT':cls.temp.name,
            'STOCK_MYSQL_DATABASE':cls.schema,'STOCK_DISABLE_WORKER':'1'})
        cls.env.start()
        try:create_isolated_database(cls.schema)
        except BaseException:
            try:drop_isolated_database(cls.schema)
            finally:cls.env.stop();cls.temp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        try:drop_isolated_database(cls.schema)
        finally:cls.env.stop();cls.temp.cleanup()

    def setUp(self):
        self.assertTrue(os.environ['STOCK_MYSQL_DATABASE'].startswith('stock_test_'))
        self.assertEqual(os.environ['STOCK_DISABLE_WORKER'],'1')
        with store.db() as conn:
            conn.execute('DELETE FROM collection_jobs')
            conn.execute('DELETE FROM schedules')
        store.set_setting('automation_paused',False)
        assets.save_source({'id':'bs_calendar','name':'隔离日历','dataset':'calendar','operation':'calendar',
                            'provider':'baostock','enabled':True,'automatic':True,'implemented':True,
                            'validation':'sample_passed','min_interval':0,'timeout':10})

    def job(self, conn, **changes):
        identifier = uuid.uuid4().hex[:16]
        doc = {'id':identifier,'identity':hashlib.sha256(identifier.encode()).hexdigest(),
               'status':'queued','created':'2026-11-13T13:00:00+08:00','updated':'2026-11-13T13:00:00+08:00',
               'source':'bs_calendar','dataset':'calendar','name':'隔离日历','start':'2026-11-13','end':'2026-11-13',
               'trigger':'schedule','schedule_id':'isolated_schedule','mode':'missing','message':'等待',
               'done':0,'total':2,'added':0,'unchanged':0,'received':0,'failed':0,'no_data':0,
               'reused':0,'retries':0,'retry_at':0,'last_yield':0,
               'items':[{'start':'2026-11-13','end':'2026-11-13'}],**changes}
        conn.execute('INSERT INTO collection_jobs(id,identity,status,created,data) VALUES(%s,%s,%s,%s,%s)',
                     (identifier,doc['identity'],doc['status'],doc['created'],store.dumps(doc)))
        queue_state.sync_job(conn,identifier,doc)
        return doc

    def test_claim_prefers_expiring_work_and_never_claims_a_future_retry(self):
        with store.db() as conn:
            urgent = self.job(conn,retry_at=5000,expires_at='2026-11-13T06:34:00+00:00')
            due = self.job(conn,retry_at=1000,last_yield=3)
            self.job(conn,retry_at=5000,last_yield=0)
        engine = collection.Engine()
        with patch.object(collection.time,'time',return_value=1000),patch.object(store,'now',return_value='2026-11-13T14:35:00+08:00'):
            self.assertEqual(engine.claim()['id'],urgent['id'])
            self.assertEqual(engine.claim()['id'],due['id'])
            self.assertIsNone(engine.claim())

    def test_pause_allows_only_manual_work_even_when_an_automatic_deadline_expires(self):
        with store.db() as conn:
            automatic = self.job(conn,expires_at='2026-11-13T14:34:00+08:00')
            manual = self.job(conn,trigger='manual')
        store.set_setting('automation_paused',True)
        with patch.object(store,'now',return_value='2026-11-13T14:35:00+08:00'):
            claimed = collection.Engine().claim()
            self.assertEqual(claimed['id'],manual['id'])
            self.assertIsNone(collection.Engine().claim())
        self.assertEqual(collection.get_job(automatic['id'])['status'],'queued')

    def test_claim_and_progress_ignore_stale_document_fields(self):
        with store.db() as conn:
            job = self.job(conn,done=1,retry_at=0)
            stale = {**job,'retry_at':10**12,'done':999}
            conn.execute('UPDATE collection_jobs SET data=%s WHERE id=%s',(store.dumps(stale),job['id']))
        with patch.object(collection.time,'time',return_value=1000):
            claimed = collection.Engine().claim()
        self.assertEqual(claimed['id'],job['id'])
        self.assertEqual(claimed['retry_at'],0)
        self.assertEqual(claimed['done'],1)
        with store.db(readonly=True) as conn:
            raw = json.loads(conn.execute('SELECT data FROM collection_jobs WHERE id=%s',(job['id'],)).fetchone()[0])
        self.assertEqual(raw['done'],1)

    def test_submit_control_persist_and_interrupted_recovery_keep_columns_in_sync(self):
        first = collection.submit({'source':'bs_calendar','start':'2026-11-13','end':'2026-11-13'})
        duplicate = collection.submit({'source':'bs_calendar','start':'2026-11-13','end':'2026-11-13'})
        self.assertEqual(first['id'],duplicate['id'])
        worker = collection.Engine()
        claimed = worker.claim()
        self.assertEqual(claimed['status'],'running')
        collection.control(first['id'],'pause')
        claimed.update(status='queued',done=1,added=7,received=7)
        worker.persist(claimed)
        current = collection.get_job(first['id'])
        self.assertEqual((current['status'],current['done'],current['added']),('paused',1,7))
        collection.control(first['id'],'resume')
        running = worker.claim()
        with patch.object(collection.threading,'Thread'):
            collection.Engine().start()
        current = collection.get_job(first['id'])
        self.assertEqual(current['status'],'interrupted')
        self.assertEqual(current['done'],1)
        with store.db(readonly=True) as conn:
            row = dict(conn.execute('SELECT status,done,added,data FROM collection_jobs WHERE id=%s',(first['id'],)).fetchone())
        document = json.loads(row['data'])
        for key in ('status','done','added'):
            self.assertEqual(row[key],document[key])
        self.assertEqual(running['id'],first['id'])

    def test_list_limits_before_reading_requests_and_omits_work_items(self):
        with store.db() as conn:
            self.job(conn,created='2026-11-13T13:00:00+08:00')
            newest = self.job(conn,created='2026-11-13T14:00:00+08:00',done=1,total=2)
        result = collection.list_jobs(1)
        self.assertEqual([item['id'] for item in result],[newest['id']])
        self.assertEqual((result[0]['done'],result[0]['total']),(1,2))
        self.assertNotIn('items',result[0])
        self.assertNotIn('`data`',queue_state.JOB_SUMMARY_COLUMNS)

    def test_schedule_save_and_tick_sync_receipt_without_json_queries(self):
        plan = {'id':'isolated_schedule','kind':'collection','source':'bs_calendar','name':'隔离计划',
                'enabled':True,'trading_only':False,'at':'00:00','interval':0}
        with store.db() as conn:
            conn.execute('INSERT INTO schedules(id,data) VALUES(%s,%s)',(plan['id'],store.dumps(plan)))
            queue_state.sync_schedule(conn,plan['id'],plan)
        collection.save_schedule(plan['id'],{'at':'01:00','enabled':True,'trading_only':False})
        engine = collection.Engine();engine.last_maintenance=1
        clock = datetime(2026,11,13,19,0,tzinfo=store.SHANGHAI)
        with patch.object(collection.time,'monotonic',return_value=1),patch.object(assets,'local_now',return_value=clock), \
             patch.object(assets,'calendar',return_value={}),patch.object(store,'today',return_value='2026-11-13'), \
             patch.object(collection,'run_schedule',return_value={'id':'isolated_job'}):
            engine.tick()
        with store.db(readonly=True) as conn:
            row = dict(conn.execute('SELECT * FROM schedules WHERE id=%s',(plan['id'],)).fetchone())
        self.assertEqual((row['last_job'],row['last_slot']),('isolated_job','2026-11-13'))
        self.assertEqual(json.loads(row['data'])['last_job'],row['last_job'])
        self.assertEqual(queue_state.schedule_view(row)['at'],'01:00')

    def test_timestamp_conversion_and_expiry_are_explicit(self):
        self.assertEqual(queue_state.local_datetime('2026-11-13T06:30:00+00:00'),datetime(2026,11,13,14,30))
        self.assertTrue(queue_state.expired('2026-11-13T06:30:00Z','2026-11-13T14:30:00+08:00'))
        self.assertFalse(queue_state.expired(None,'2026-11-13T14:30:00+08:00'))
        with self.assertRaises(ValueError):queue_state.local_datetime('not-a-deadline')

    def test_upgrade_adds_legacy_columns_once_and_syncs_saved_requests(self):
        # This test is last alphabetically.  It exercises the real upgrade DDL,
        # not an emulated database or the live project's tables.
        with store.db(admin=True) as conn:
            conn.execute('DROP TABLE collection_jobs')
            conn.execute('DROP TABLE schedules')
            conn.execute('CREATE TABLE schedules(id VARCHAR(48) CHARACTER SET ascii PRIMARY KEY,data MEDIUMTEXT NOT NULL) ENGINE=InnoDB')
            conn.execute('CREATE TABLE collection_jobs(id CHAR(16) CHARACTER SET ascii PRIMARY KEY,'
                         'identity CHAR(64) CHARACTER SET ascii NOT NULL,status VARCHAR(20) NOT NULL,'
                         'created VARCHAR(32) NOT NULL,data MEDIUMTEXT NOT NULL,'
                         "active_identity CHAR(64) GENERATED ALWAYS AS (CASE WHEN status IN ('queued','running','waiting','paused') THEN identity ELSE NULL END) STORED,"
                         'UNIQUE KEY active_job_identity(active_identity)) ENGINE=InnoDB')
            added = queue_state.apply_schema(conn)
            self.assertIn('collection_jobs.expires_at',added)
            self.assertIn('schedules.last_job',added)
            self.assertEqual(queue_state.apply_schema(conn),[])
        with store.db() as conn:
            doc = self.job(conn,done=1,total=4,expires_at='2026-11-13T19:00:00+08:00')
            queue_state.sync_job(conn,doc['id'])
            plan = {'id':'announcements','enabled':True,'trading_only':True,'at':'18:00',
                    'last_job':doc['id'],'last_slot':'2026-11-13'}
            conn.execute('INSERT INTO schedules(id,data) VALUES(%s,%s)',(plan['id'],store.dumps(plan)))
            queue_state.sync_schedule(conn,plan['id'])
            receipt = conn.execute('SELECT j.done,j.total FROM schedules s JOIN collection_jobs j ON j.id=s.last_job '
                                   'WHERE s.id=%s AND s.last_slot=%s',(plan['id'],plan['last_slot'])).fetchone()
            self.assertEqual(tuple(receipt),(1,4))


if __name__=='__main__':unittest.main()
