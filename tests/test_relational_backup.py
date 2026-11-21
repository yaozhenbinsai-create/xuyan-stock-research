"""Restore typed TIME/DATETIME/DATE/DECIMAL/BINARY facts in disposable schemas."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from foundation import assets, backups, database as store, queue_state, record_store
from tools.mysql_instance import create_isolated_database, drop_isolated_database


class RelationalBackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        suffix = uuid.uuid4().hex[:10]
        cls.source = 'stock_test_relational_backup_'+suffix
        cls.restored = 'stock_restore_relational_backup_'+suffix
        cls.temp = tempfile.TemporaryDirectory(prefix='stock-relational-backup-')
        cls.env = patch.dict(os.environ, {'STOCK_DATA_ROOT':cls.temp.name,
            'STOCK_MYSQL_DATABASE':cls.source,'STOCK_DISABLE_WORKER':'1'})
        cls.env.start()
        cls.restore_created = False
        try:create_isolated_database(cls.source)
        except BaseException:
            try:drop_isolated_database(cls.source)
            finally:cls.env.stop();cls.temp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            if cls.restore_created:drop_isolated_database(cls.restored)
        finally:
            try:drop_isolated_database(cls.source)
            finally:cls.env.stop();cls.temp.cleanup()

    def test_backup_restore_keeps_scheduled_time_typed_quote_and_fact_identity(self):
        self.assertTrue(os.environ['STOCK_MYSQL_DATABASE'].startswith('stock_test_'))
        self.assertEqual(os.environ['STOCK_DISABLE_WORKER'],'1')
        quote = {'day':'2026-11-13','symbol':'sh.600000','key':'20261113143500',
                 'name':'隔离备份股票','open':10.,'high':10.5,'low':9.5,'close':10.25,
                 'preclose':10.,'volume':1000.,'amount':10250.,
                 'source_time':'2026-11-13T14:35:00.123456+08:00','volume_unit':'shares'}
        finance = {'day':'2026-10-28','symbol':'sh.600000','key':'profit:2026-09-30',
                   'report_type':'profit','report_date':'2026-09-30','published':'2026-10-28',
                   'fields':{'netProfit':'000123.4500','MBRevenue':'','unknown':None}}
        calendar = {'day':'2026-11-13','symbol':'*','key':'','trading':True,'markets':['sh','sz']}
        schedule = {'id':'isolated_backup_clock','source':'bs_calendar','kind':'collection',
                    'at':'19:07','enabled':True,'trading_only':True,'interval':0,
                    'last_job':None,'last_slot':'2026-11-13'}
        with store.db() as conn:
            quote_id,_ = assets.insert_fact(conn,'quotes','tx_quotes',quote)
            financial_id,_ = assets.insert_fact(conn,'financials','bs_profit',finance)
            calendar_id,_ = assets.insert_fact(conn,'calendar','bs_calendar',calendar)
            conn.execute('INSERT INTO schedules(id,data) VALUES(%s,%s)',(schedule['id'],store.dumps(schedule)))
            queue_state.sync_schedule(conn,schedule['id'],schedule)
            before = record_store.load_many(conn,[quote_id,financial_id,calendar_id])
            fingerprint = bytes(conn.execute('SELECT fingerprint FROM facts WHERE id=%s',(quote_id,)).fetchone()[0])
            self.assertEqual(len(fingerprint),32)
            self.assertEqual(conn.execute('SELECT run_at FROM schedules WHERE id=%s',(schedule['id'],)).fetchone()[0],timedelta(hours=19,minutes=7))
        receipt = backups.create_backup()
        path = Path(self.temp.name)/'backups'/receipt['file']
        self.assertTrue(path.is_file())
        footer = backups.verify_file(path)
        self.assertEqual(footer['counts']['facts'],3)
        self.__class__.restore_created = True
        restored = backups.restore_isolated(path,self.restored)
        self.assertEqual(restored['integrity'],'restored_hashes_and_counts_verified')
        self.assertEqual(set(restored['table_hashes']),set(backups.TABLES))
        with store.db(readonly=True,database=self.restored) as conn:
            after = record_store.load_many(conn,[quote_id,financial_id,calendar_id])
            self.assertEqual(store.dumps(before),store.dumps(after))
            self.assertEqual(bytes(conn.execute('SELECT fingerprint FROM facts WHERE id=%s',(quote_id,)).fetchone()[0]),fingerprint)
            self.assertEqual(conn.execute('SELECT source_time FROM quote_snapshots WHERE fact_id=%s',(quote_id,)).fetchone()[0],datetime(2026,11,13,14,35,0,123456))
            self.assertEqual(conn.execute('SELECT run_at FROM schedules WHERE id=%s',(schedule['id'],)).fetchone()[0],timedelta(hours=19,minutes=7))
            amount = conn.execute('SELECT numeric_value FROM financial_values WHERE fact_id=%s AND field_name=%s',(financial_id,'netProfit')).fetchone()[0]
            self.assertEqual(amount,Decimal('123.45'))
            self.assertEqual(conn.execute('SELECT day FROM facts WHERE id=%s',(calendar_id,)).fetchone()[0],'2026-11-13')
            self.assertEqual(conn.execute('SELECT count(*) FROM heads WHERE fact_id IN (%s,%s,%s)',(quote_id,financial_id,calendar_id)).fetchone()[0],3)


if __name__=='__main__':unittest.main()
