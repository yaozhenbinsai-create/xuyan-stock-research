"""One tiny v1 backup is verified exactly, then recovered into typed tables."""
from __future__ import annotations

from contextlib import contextmanager
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from foundation import backup_compat, backups, database as store, record_store
from tools.mysql_instance import drop_isolated_database


def legacy_columns():
    """Fixture headers come from the verified archived DDL, not today's schema."""
    text=backup_compat.LEGACY_SCHEMA.read_text(encoding='utf-8')
    tables={}
    for table,body in re.findall(r'CREATE TABLE IF NOT EXISTS\s+`?(\w+)`?\s*\((.*?)\) ENGINE=InnoDB',text,re.S):
        segments=[];current=[];depth=0;quote=None;escape=False
        for character in body:
            if quote:
                current.append(character)
                if escape:escape=False
                elif character=='\\':escape=True
                elif character==quote:quote=None
            elif character in ("'",'"','`'):
                quote=character;current.append(character)
            elif character=='(':
                depth+=1;current.append(character)
            elif character==')':
                depth-=1;current.append(character)
            elif character==',' and depth==0:
                segments.append(''.join(current).strip());current=[]
            else:current.append(character)
        segments.append(''.join(current).strip())
        columns=[]
        for segment in segments:
            match=re.match(r'`?(\w+)`?\s+',segment)
            if (match and (segment.startswith('`') or match[1].upper() not in ('PRIMARY','UNIQUE','KEY','CONSTRAINT','FOREIGN','INDEX'))
                    and 'GENERATED' not in segment.upper()):
                columns.append(match[1])
        tables[table]=columns
    return tables


def write_v1_fixture(path):
    headers=legacy_columns()
    stamp='2026-11-13T19:00:00+08:00'
    payload={'day':'2026-11-13','symbol':'sh.600000','key':'','open':10.,'high':11.,'low':9.,
             'close':10.,'preclose':10.,'volume':100.,'amount':1000.,'pe':None,'pb':None,
             'adjust':'raw','volume_unit':'shares'}
    fingerprint=hashlib.sha256(store.dumps(['daily',payload]).encode()).hexdigest()
    run='legacy_backup_01';call='legacy_call_0001';job='legacy_job_00001'
    model_secret=b'\x00isolated-cipher-fixture\xff'
    measured={'usage':{'input_tokens':10,'output_tokens':2,'total_tokens':12},
              'usage_normalized':{'input':10,'output':2,'total':12,'cached_input':None,'reasoning_output':None},
              'usage_state':'complete'}
    job_data={'id':job,'source':'bs_daily_all','dataset':'daily','name':'旧备份采集',
              'status':'waiting','created':stamp,'updated':stamp,'trigger':'manual','mode':'missing',
              'start':payload['day'],'end':payload['day'],'done':1,'total':2,'retry_at':123.,
              'items':[{'start':payload['day'],'end':payload['day']}],'message':'原进度'}
    rows={
        'schema_versions':[{'version':1,'applied_at':stamp}],
        'facts':[{'id':42,'dataset':'daily','day':payload['day'],'symbol':payload['symbol'],'record_key':'',
                  'source':'bs_daily_all','source_version':'','payload':store.dumps(payload),'fingerprint':fingerprint,
                  'first_seen':stamp,'last_seen':stamp,'acquisitions':3,'original_id':None,'last_original_id':None}],
        'heads':[{'dataset':'daily','day':payload['day'],'symbol':payload['symbol'],'record_key':'','fact_id':42}],
        'evidence_refs':[{'owner_type':'research','owner_id':run,'fact_id':42,'created':stamp}],
        'models':[{'id':5,'name':'旧备份模型','data':store.dumps({'enabled':False,'protocol':'openai',
                    'model_identifier':'isolated-disabled','base_url':'https://example.invalid/v1'}),'secret':model_secret}],
        'research_runs':[{'id':run,'request_key':'a'*64,'status':'completed','stage':'历史原稿','created':stamp,
                          'updated':stamp,'market_day':payload['day'],'kind':'discovery',
                          'data':store.dumps({'session':'close','intent':'历史备份原稿'}),
                          'report':store.dumps({'id':run,'title':'不可改写的原稿','market_analysis':'原市场分析',
                                               'published_at':stamp,'mode':'retrospective','decisions':[]})}],
        'research_calls':[{'id':call,'run_id':run,'model_id':5,'started':stamp,'finished':stamp,
                           'status':'completed','data':store.dumps(measured)}],
        'collection_jobs':[{'id':job,'identity':'b'*64,'status':'waiting','created':stamp,'data':store.dumps(job_data)}],
        'schedules':[{'id':'daily','data':store.dumps({'id':'daily','source':'bs_daily_all','kind':'collection',
                        'enabled':False,'trading_only':True,'at':'17:30','last_job':job,'last_slot':payload['day']})}],
        'asset_stats':[{'dataset':'daily','data':store.dumps({'dataset':'daily','name':'日线行情','count':1,
                         'first':payload['day'],'last':payload['day'],'symbols':1,'versions':1,'generation':0})}],
        'dataset_usage':[{'dataset':'daily','payload_bytes':len(store.dumps(payload).encode()),'records':1}],
    }
    digest=hashlib.sha256();counts={}
    with gzip.open(path,'wb') as stream:
        def emit(value):
            data=(store.dumps(value)+'\n').encode();stream.write(data);digest.update(data)
        emit({'format':'stock-mysql-jsonl-v1','schema_version':1,'created':stamp})
        for table,columns in headers.items():
            # Historical files intentionally have no auto_increment header.
            emit({'table':table,'columns':columns})
            for row in rows.get(table,[]):
                if set(row)!=set(columns):
                    raise AssertionError((table,sorted(set(columns)-row.keys()),sorted(row.keys()-set(columns))))
                emit({'row':[backups.pack(row[column]) for column in columns]})
            counts[table]=len(rows.get(table,[]))
        footer={'counts':counts,'sha256':digest.hexdigest()}
        stream.write((store.dumps({'footer':footer})+'\n').encode())
    return payload,fingerprint,model_secret,footer,run,call,job


class LegacyBackupRecoveryTests(unittest.TestCase):
    def test_exact_v1_restore_then_typed_migration_preserves_values_ids_and_history(self):
        target='stock_restore_compat_fixture_'+uuid.uuid4().hex[:10]
        known={target}
        with tempfile.TemporaryDirectory(prefix='stock-v1-backup-') as directory,patch.dict(os.environ,{
                'STOCK_DATA_ROOT':directory,'STOCK_MYSQL_DATABASE':target,'STOCK_DISABLE_WORKER':'1'}):
            path=Path(directory)/'historical-v1.jsonl.gz'
            payload,fingerprint,secret,footer,run,call,job=write_v1_fixture(path)
            self.assertEqual(backups.verify_file(path),footer)
            try:
                result=backups.restore_isolated(path,target)
                known.add(result['temporary_legacy_database'])
                self.assertEqual(result['integrity'],'legacy_backup_verified_and_typed_migration_validated')
                self.assertEqual(result['source_schema_version'],1)
                self.assertTrue(result['temporary_legacy_removed'])
                self.assertFalse(result['auto_increment']['recorded_high_water_complete'])
                self.assertIn('facts',result['auto_increment']['missing_headers'])
                self.assertEqual(set(result['legacy_restore']['table_hashes']),set(backup_compat.LEGACY_TABLES))
                with store.db(readonly=True,database=target) as conn:
                    self.assertEqual(store.dumps(record_store.load(conn,42)),store.dumps(payload))
                    self.assertEqual(bytes(conn.execute('SELECT fingerprint FROM facts WHERE id=42').fetchone()[0]).hex(),fingerprint)
                    self.assertEqual(conn.execute('SELECT fact_id FROM heads').fetchone()[0],42)
                    self.assertEqual(conn.execute('SELECT fact_id FROM evidence_refs').fetchone()[0],42)
                    self.assertEqual(bytes(conn.execute('SELECT secret FROM models WHERE id=5').fetchone()[0]),secret)
                    self.assertEqual(conn.execute('SELECT total_tokens FROM research_calls WHERE id=%s',(call,)).fetchone()[0],12)
                    self.assertEqual(conn.execute('SELECT done,total FROM collection_jobs WHERE id=%s',(job,)).fetchone()[0],1)
                    self.assertEqual(json.loads(conn.execute('SELECT report FROM research_runs WHERE id=%s',(run,)).fetchone()[0])['title'],'不可改写的原稿')
                    self.assertIsNone(conn.execute('SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s',
                                                  (result['temporary_legacy_database'],)).fetchone())
            except backup_compat.LegacyBackupRestoreError as failure:
                known.add(failure.legacy_database)
                raise
            finally:
                for name in known:
                    with store.db(admin=True,database='stock_foundation',readonly=True) as conn:
                        exists=conn.execute('SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s',(name,)).fetchone()
                    if exists:drop_isolated_database(name)


class LegacyBackupUnitTests(unittest.TestCase):
    def test_failure_keeps_the_temporary_legacy_identity_and_never_drops_evidence(self):
        from tools import relational_migration, mysql_instance
        @contextmanager
        def fake_db(**options):
            yield SimpleNamespace(execute=lambda *args:SimpleNamespace(fetchone=lambda:None))
        with patch.object(store,'db',fake_db),patch.object(backup_compat,'create_legacy_schema') as create, \
             patch.object(backups,'restore_raw',return_value={'counts':{},'sha256':'verified','auto_increment':{}}), \
             patch.object(relational_migration,'stage',side_effect=RuntimeError('isolated migration failure')), \
             patch.object(mysql_instance,'drop_isolated_database') as drop:
            with self.assertRaises(backup_compat.LegacyBackupRestoreError) as failed:
                backup_compat.restore_legacy(Path('unused.gz'),'stock_restore_requested',{})
            self.assertEqual(failed.exception.phase,'migrate_to_typed_schema')
            self.assertEqual(failed.exception.target_database,'stock_restore_requested')
            self.assertRegex(failed.exception.legacy_database,r'^stock_restore_legacy_backup_[a-f0-9]{16}$')
            create.assert_called_once_with(failed.exception.legacy_database)
            drop.assert_not_called()

    def test_auto_increment_headers_are_applied_and_old_missing_high_water_is_explicit(self):
        counters={'facts':43,'models':6};queries=[]
        class Rows(list):
            def fetchall(self):return self
        class Connection:
            def execute(self,sql,params=()):
                queries.append(sql)
                if sql.startswith('SELECT TABLE_NAME,AUTO_INCREMENT'):
                    return Rows([name,value] for name,value in counters.items())
                match=re.fullmatch(r'ALTER TABLE `(\w+)` AUTO_INCREMENT=(\d+)',sql)
                if match:counters[match[1]]=int(match[2])
                return []
        @contextmanager
        def fake_db(**options):yield Connection()
        with patch.object(store,'db',fake_db):
            result=backups._restore_increments('stock_restore_fixture',{'tables':{'facts':{'auto_increment':1000}}})
        self.assertEqual(counters['facts'],1000)
        self.assertEqual(result['missing_headers'],['models'])
        self.assertFalse(result['recorded_high_water_complete'])
        self.assertIn('不能还原',result['limitation'])
        self.assertEqual(queries[0],'SET SESSION information_schema_stats_expiry=0')


if __name__=='__main__':unittest.main()
