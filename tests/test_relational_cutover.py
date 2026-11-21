"""Real cutover/rollback uses only three uniquely named, minimal test schemas."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import pymysql

from foundation import database as store
from tools import relational_cutover as cutover, relational_migration as migration


class RelationalCutoverTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='stock-cutover-test-')
        self.addCleanup(self.temp.cleanup)
        suffix=uuid.uuid4().hex[:10]
        self.production='stock_test_cutover_prod_'+suffix
        self.target='stock_test_cutover_new_'+suffix
        self.archive='stock_test_cutover_old_'+suffix
        self.owned=set()
        self.environment=patch.dict(os.environ,{
            'STOCK_DATA_ROOT':self.temp.name,'STOCK_MYSQL_DATABASE':self.production,
            'STOCK_DISABLE_WORKER':'1'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.clean_schemas)
        for name,value in (('PRODUCTION',self.production),('RELATIONAL_PREFIX','stock_test_cutover_new_'),
                           ('LEGACY_PREFIX','stock_test_cutover_old_')):
            replacement=patch.object(cutover,name,value,create=True)
            replacement.start();self.addCleanup(replacement.stop)
        offline=patch.object(cutover,'_offline')
        self.offline=offline.start();self.addCleanup(offline.stop)
        self.create_schema(self.production)
        self.create_schema(self.target)
        self.seed(self.production,typed=False)
        self.seed(self.target,typed=True)
        self.receipt=Path(self.temp.name)/'promotion.json'
        self.reverse=Path(self.temp.name)/'rollback.json'
        self.proof={'status':'validated','source':self.production,'target':self.target,
                    'validation':{'schema_version':store.SCHEMA_VERSION,'full_value_fidelity':True,
                                  'facts':1,'tables':{'heads':{'count':1}}}}

    def create_schema(self,name):
        self.assertTrue(name.startswith('stock_test_cutover_'))
        with store.db(admin=True,database='stock_foundation') as conn:
            self.assertIsNone(conn.execute('SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s',
                                           (name,)).fetchone())
            conn.execute('CREATE DATABASE '+cutover._q(name)+' CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
        self.owned.add(name)

    def exists(self,name):
        with store.db(admin=True,database='stock_foundation',readonly=True) as conn:
            return conn.execute('SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s',
                                (name,)).fetchone() is not None

    def clean_schemas(self):
        # The archive is created by the operation under test, after its absence
        # was checked. Only these exact names from this fixture may be dropped.
        if self.exists(self.archive):
            self.owned.add(self.archive)
        store.close_pool()
        with store.db(admin=True,database='stock_foundation') as conn:
            for name in sorted(self.owned):
                self.assertTrue(name.startswith('stock_test_cutover_'))
                self.assertIn(name,(self.production,self.target,self.archive))
                conn.execute('DROP DATABASE '+cutover._q(name))
        store.close_pool()

    def drop_archive(self):
        self.assertTrue(self.exists(self.archive))
        with store.db(admin=True,database='stock_foundation') as conn:
            conn.execute('DROP DATABASE '+cutover._q(self.archive))
        self.owned.discard(self.archive)

    def seed(self,schema,*,typed):
        with store.db(admin=True,database=schema) as conn:
            conn.execute('CREATE TABLE schema_versions(version INT PRIMARY KEY) ENGINE=InnoDB')
            conn.execute('CREATE TABLE facts(id INT PRIMARY KEY, marker VARCHAR(32) NOT NULL) ENGINE=InnoDB')
            conn.execute('CREATE TABLE heads(id INT PRIMARY KEY,fact_id INT NOT NULL,'
                         'CONSTRAINT cutover_heads_fact FOREIGN KEY(fact_id) REFERENCES facts(id)) ENGINE=InnoDB')
            conn.execute('CREATE TABLE collection_jobs(id INT PRIMARY KEY,status VARCHAR(32) NOT NULL) ENGINE=InnoDB')
            conn.execute('CREATE TABLE research_runs(id INT PRIMARY KEY,status VARCHAR(32) NOT NULL) ENGINE=InnoDB')
            conn.execute('INSERT INTO schema_versions VALUES(%s)',(store.SCHEMA_VERSION if typed else store.SCHEMA_VERSION-1,))
            conn.execute("INSERT INTO facts VALUES(1,'preserved value')")
            conn.execute('INSERT INTO heads VALUES(1,1)')
            conn.execute("INSERT INTO collection_jobs VALUES(1,'completed')")
            conn.execute("INSERT INTO research_runs VALUES(1,'completed')")
            if typed:
                conn.execute('CREATE TABLE typed_prices(fact_id INT PRIMARY KEY,close DOUBLE,'
                             'CONSTRAINT cutover_typed_fact FOREIGN KEY(fact_id) REFERENCES facts(id)) ENGINE=InnoDB')
                conn.execute('INSERT INTO typed_prices VALUES(1,13)')

    def tables(self,schema):
        with store.db(admin=True,database=schema,readonly=True) as conn:
            return cutover._tables(conn,schema)

    def status(self,schema,table,value):
        self.assertIn(table,('collection_jobs','research_runs'))
        with store.db(admin=True,database=schema) as conn:
            conn.execute('UPDATE '+cutover._q(table)+' SET status=%s WHERE id=1',(value,))

    def assert_local_references(self,schema):
        with store.db(admin=True,database=schema,readonly=True) as conn:
            references=cutover._references(conn,schema)
            self.assertTrue(references)
            self.assertTrue(all(row['referenced_schema']==schema for row in references))
            self.assertEqual(conn.execute('SELECT marker FROM facts WHERE id=1').fetchone()[0],'preserved value')
        with self.assertRaises(pymysql.IntegrityError):
            with store.db(admin=True,database=schema) as conn:
                conn.execute('INSERT INTO heads VALUES(2,999)')

    def promote(self):
        self.assertFalse(self.exists(self.archive))
        return cutover._promote(self.target,self.archive,self.proof,self.receipt)

    def test_real_three_schema_roundtrip_receipts_and_rejection_guards(self):
        self.status(self.production,'collection_jobs','running')
        with self.assertRaisesRegex(RuntimeError,'drained'):
            self.promote()
        self.assertFalse(self.exists(self.archive))
        self.status(self.production,'collection_jobs','completed')
        old=self.tables(self.production);new=self.tables(self.target)
        result=self.promote()
        self.assertEqual(json.loads(self.receipt.read_text(encoding='utf-8')),result)
        intent=json.loads(Path(str(self.receipt)+'.intent.json').read_text(encoding='utf-8'))
        self.assertTrue(intent['ddl_may_have_applied'])
        self.assertEqual(self.tables(self.production),new)
        self.assertEqual(self.tables(self.archive),old)
        self.assertEqual(self.tables(self.target),[])
        for name in (self.production,self.archive):
            self.assert_local_references(name)
        wrong=Path(self.temp.name)/'wrong-promotion.json'
        wrong.write_text(json.dumps({**result,'rollback':self.archive+'_wrong'}),encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError,'exact promotion'):
            cutover.rollback(self.target,self.archive,wrong,self.reverse)
        self.status(self.archive,'research_runs','running')
        with self.assertRaisesRegex(RuntimeError,'drained'):
            cutover.rollback(self.target,self.archive,self.receipt,self.reverse)
        self.status(self.archive,'research_runs','completed')
        with store.db(admin=True,database=self.archive) as conn:
            conn.execute('CREATE TABLE stray(id INT PRIMARY KEY) ENGINE=InnoDB')
        with self.assertRaisesRegex(RuntimeError,'manifest'):
            cutover.rollback(self.target,self.archive,self.receipt,self.reverse)
        with store.db(admin=True,database=self.archive) as conn:
            conn.execute('DROP TABLE stray')
        restored=cutover.rollback(self.target,self.archive,self.receipt,self.reverse)
        self.assertEqual(restored['status'],'restored')
        self.assertEqual(json.loads(self.reverse.read_text(encoding='utf-8')),restored)
        self.assertEqual(self.tables(self.production),old)
        self.assertEqual(self.tables(self.target),new)
        self.assertEqual(self.tables(self.archive),[])
        for name in (self.production,self.target):
            self.assert_local_references(name)

    def test_cli_always_uses_new_validation_or_same_invocation_synchronization(self):
        validation=Path(self.temp.name)/'validation.json'
        for operation,method in (('promote','validate'),('sync-promote','synchronize')):
            with self.subTest(operation=operation):
                validation.write_text('{"status":"stale","target":"wrong"}',encoding='utf-8')
                with patch.object(migration,method,return_value=self.proof) as fresh, \
                     patch.object(cutover,'_promote',wraps=cutover._promote) as promoting, \
                     patch('sys.argv',['relational_cutover',operation,'--target',self.target,
                                       '--archive',self.archive,'--receipt',str(self.receipt),
                                       '--validation-receipt',str(validation),'--batch-size','2']), \
                     redirect_stdout(io.StringIO()):
                    cutover.main()
                fresh.assert_called_once()
                self.assertEqual(fresh.call_args.args,(self.production,self.target))
                self.assertEqual(fresh.call_args.kwargs['batch_size'],2)
                self.assertIs(promoting.call_args.args[2],self.proof)
                self.assertEqual(json.loads(validation.read_text(encoding='utf-8')),self.proof)
                self.assertTrue(self.offline.called)
                self.assert_local_references(self.production)
                cutover.rollback(self.target,self.archive,self.receipt,self.reverse)
                self.drop_archive()

    def test_archive_and_offline_rejections_and_receipt_failure_boundaries(self):
        with patch.object(cutover,'_offline',side_effect=RuntimeError('service is running')):
            with self.assertRaisesRegex(RuntimeError,'service is running'):
                self.promote()
        self.assertFalse(self.exists(self.archive))
        self.create_schema(self.archive)
        with self.assertRaisesRegex(RuntimeError,'already exists'):
            cutover._promote(self.target,self.archive,self.proof,self.receipt)
        self.drop_archive()
        old=self.tables(self.production);new=self.tables(self.target)
        original_save=cutover._save
        def deny_receipt(path,value):
            if Path(path)==self.receipt:
                raise OSError('receipt destination denied')
            return original_save(path,value)
        with patch.object(cutover,'_save',side_effect=deny_receipt):
            with self.assertRaisesRegex(OSError,'destination denied'):
                self.promote()
        self.assertEqual(self.tables(self.production),old)
        self.assertEqual(self.tables(self.target),new)
        self.assertEqual(self.tables(self.archive),[])
        self.assertFalse(json.loads(Path(str(self.receipt)+'.intent.json').read_text(encoding='utf-8'))['ddl_may_have_applied'])
        self.drop_archive()
        def deny_final_receipt(path,value):
            if Path(path)==self.receipt and value.get('status')=='promoted':
                raise OSError('final receipt denied')
            return original_save(path,value)
        with patch.object(cutover,'_save',side_effect=deny_final_receipt):
            with self.assertRaisesRegex(RuntimeError,'may already have applied'):
                self.promote()
        # A final-receipt error is reported as uncertain; both physical copies
        # and the pre-DDL intent still exist for inspection.
        self.assertEqual(self.tables(self.production),new)
        self.assertEqual(self.tables(self.archive),old)
        self.assertEqual(self.tables(self.target),[])
        self.assertTrue(json.loads(Path(str(self.receipt)+'.intent.json').read_text(encoding='utf-8'))['ddl_may_have_applied'])
        self.assertEqual(json.loads(self.receipt.read_text(encoding='utf-8'))['status'],'prepared')
        for name in (self.production,self.archive):
            self.assert_local_references(name)

    def test_rollback_final_receipt_failure_reports_that_ddl_may_have_applied(self):
        old=self.tables(self.production);new=self.tables(self.target)
        self.promote()
        original_save=cutover._save
        def deny_final_receipt(path,value):
            if Path(path)==self.reverse and value.get('status')=='restored':
                raise OSError('rollback final receipt denied')
            return original_save(path,value)
        error=None
        with patch.object(cutover,'_save',side_effect=deny_final_receipt):
            try:
                cutover.rollback(self.target,self.archive,self.receipt,self.reverse)
            except Exception as caught:
                error=caught
        self.assertEqual(self.tables(self.production),old)
        self.assertEqual(self.tables(self.target),new)
        self.assertEqual(self.tables(self.archive),[])
        self.assertTrue(json.loads(Path(str(self.reverse)+'.intent.json').read_text(encoding='utf-8'))['ddl_may_have_applied'])
        self.assert_local_references(self.production)
        self.assertIsInstance(error,RuntimeError)
        self.assertIn('may already have applied',str(error))


if __name__=='__main__':
    unittest.main()
