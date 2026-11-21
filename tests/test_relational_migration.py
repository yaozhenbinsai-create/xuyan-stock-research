"""Legacy-to-typed migration tests use only explicitly isolated schemas."""
from __future__ import annotations

import base64
from collections import Counter
import json
import os
import struct
import tempfile
import unittest
import uuid
from unittest.mock import patch
import zlib

from foundation import database as store, record_store, storage_policy, queue_state
from research import state_store, coverage
from tools import relational_migration as migration
from tools.mysql_instance import create_isolated_database, drop_isolated_database


SOURCE='stock_test_migration_src_'+uuid.uuid4().hex[:8]
TARGETS=[]
DAY='2026-11-04'
SYMBOL='sh.600000'
RUN='0123456789abcdef'
JOB='1111111111111111'
CYCLE='3333333333333333'


def setUpModule():
    create_isolated_database(SOURCE)
    with store.db(admin=True,database=SOURCE) as conn:
        assert SOURCE.startswith('stock_test_')
        derived=set(record_store.TABLES)|set(state_store.TABLES)|set(coverage.TABLES)
        for table in reversed(migration._order(conn,derived)):
            conn.execute('DROP TABLE '+migration._ident(table))
        for table,definitions in (*state_store.COLUMNS.items(),
                                  ('collection_jobs',queue_state.JOB_COLUMNS),('schedules',queue_state.SCHEDULE_COLUMNS)):
            conn.execute('ALTER TABLE '+migration._ident(table)+' '+','.join('DROP COLUMN '+migration._ident(name) for name in definitions))
        conn.execute('ALTER TABLE facts MODIFY day CHAR(10) CHARACTER SET ascii NOT NULL, '
                     'MODIFY fingerprint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, '
                     'ADD COLUMN payload MEDIUMTEXT NOT NULL, DROP COLUMN payload_bytes')
        conn.execute('ALTER TABLE heads MODIFY day CHAR(10) CHARACTER SET ascii NOT NULL')
        conn.execute('DROP TABLE asset_stats')
        conn.execute('CREATE TABLE asset_stats(dataset VARCHAR(24) CHARACTER SET ascii PRIMARY KEY,data TEXT NOT NULL) ENGINE=InnoDB')
        conn.execute('DELETE FROM schema_versions')
        conn.execute('INSERT INTO schema_versions VALUES(1,%s)',(store.now(),))


def tearDownModule():
    for schema in TARGETS:
        with store.db(admin=True,database=SOURCE,readonly=True) as conn:
            exists=conn.execute('SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s',(schema,)).fetchone()
        if exists:
            drop_isolated_database(schema)
    drop_isolated_database(SOURCE)


class RelationalMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='stock-relational-migrate-')
        self.env=patch.dict(os.environ,{'STOCK_DATA_ROOT':self.temp.name,'STOCK_MYSQL_DATABASE':SOURCE,'STOCK_DISABLE_WORKER':'1'})
        self.env.start()
        self.target='stock_test_migration_dst_'+uuid.uuid4().hex[:8]
        TARGETS.append(self.target)
        self.payloads={}
        with store.db(admin=True,database=SOURCE) as conn:
            for table in reversed(migration._order(conn,migration._tables(conn)-{'schema_versions'})):
                conn.execute('DELETE FROM '+migration._ident(table))
        self.seed()

    def tearDown(self):
        self.env.stop();self.temp.cleanup()

    def fact(self,dataset,payload,source,*,historical_hash=False):
        value=storage_policy.normalize(dataset,payload)
        if historical_hash:
            # Explicitly unknown values in legacy history must not be dropped.
            value['pe']=None
        encoded=store.dumps(value)
        digest='0'*64 if historical_hash else storage_policy.content_hash(dataset,value)
        with store.db(admin=True,database=SOURCE) as conn:
            inserted=conn.execute('INSERT INTO facts(dataset,day,symbol,record_key,source,source_version,payload,fingerprint,'
                                  'first_seen,last_seen,acquisitions,original_id,last_original_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                                  (dataset,value['day'],value['symbol'],value.get('key',''),source,'legacy-test',encoded,digest,
                                   '2026-11-04T16:00:00+08:00','2026-11-04T17:00:00+08:00',3,42,43))
            fid=inserted.lastrowid
            conn.execute('INSERT INTO heads VALUES(%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE fact_id=VALUES(fact_id)',
                         (dataset,value['day'],value['symbol'],value.get('key',''),fid))
        self.payloads[fid]=value
        return fid

    def refresh_source_stats(self):
        with store.db(admin=True,database=SOURCE) as conn:
            conn.execute('DELETE FROM asset_stats');conn.execute('DELETE FROM dataset_usage')
            for dataset in sorted({v['dataset'] for v in conn.execute('SELECT DISTINCT dataset FROM facts')}):
                count,first,last,symbols=conn.execute('SELECT COUNT(*),MIN(day),MAX(day),COUNT(DISTINCT symbol) FROM heads WHERE dataset=%s',(dataset,)).fetchone()
                versions,amount=conn.execute('SELECT COUNT(*),SUM(OCTET_LENGTH(payload)) FROM facts WHERE dataset=%s',(dataset,)).fetchone()
                value={'dataset':dataset,'count':count,'first':first,'last':last,'symbols':symbols,'versions':versions,'generation':7}
                conn.execute('INSERT INTO asset_stats VALUES(%s,%s)',(dataset,store.dumps(value)))
                conn.execute('INSERT INTO dataset_usage VALUES(%s,%s,%s)',(dataset,amount,versions))

    def seed(self):
        base={'day':DAY,'symbol':SYMBOL,'open':10,'high':12,'low':9,'close':10,'preclose':10,
              'volume':100,'amount':1000,'adjust':'raw','suspended':False,'volume_unit':'shares'}
        self.old=self.fact('daily',base,'bs_daily_all',historical_hash=True)
        self.latest=self.fact('daily',{**base,'close':11},'bs_daily_all')
        self.fact('calendar',{'day':DAY,'symbol':'*','trading':True,'markets':['sh','sz']},'bs_calendar')
        self.fact('basics',{'day':DAY,'symbol':SYMBOL,'key':'universe','name':'隔离证券','listed':None,'status':'1','board':'主板'},'bs_universe')
        self.fact('industry',{'day':DAY,'symbol':SYMBOL,'key':'financial','group':'金融业','taxonomy':'测试分类',
                             'source_effective_date':None,'historical_effective_unknown':True,'unclassified':False},'bs_industry')
        self.fact('financials',{'day':DAY,'symbol':SYMBOL,'key':'profit:2026-09-30','report_type':'profit',
                               'report_date':'2026-09-30','published':DAY,'fields':{'netProfit':'123.400','MBRevenue':'','other':None}},'bs_profit')
        self.notice=self.fact('announcements',{'day':DAY,'symbol':SYMBOL,'key':'notice','title':'隔离公告',
                                             'published':DAY+'T17:00:00+08:00','text':'保留这份正文及其原始时点。','url':'https://example.invalid/a.pdf'},'cninfo_text')
        raw=b'H600000'
        packed={'covered':base64.b64encode(struct.pack('<I',len(raw))+zlib.compress(raw)).decode('ascii')}
        data={'session':'close','intent':'隔离研究','bundle':{'market':{'coverage':.8},'scope_pack':packed,'candidates':[{'symbol':SYMBOL}]}}
        report={'title':'隔离原稿','mode':'close_research','published_at':DAY+'T18:00:00+08:00','market_analysis':'只包含隔离样本',
                'forecast_target':{'version':'session-targets-v1'},'decisions':[{'symbol':SYMBOL,'action':'watch','rating':{'stars':2}}]}
        self.contract={'action':'watch','session_policy':{'session':'close','reference_from':DAY+'T09:30:00+08:00',
                                                         'action_valid_until':'2026-11-05T14:55:00+08:00'},
                       'entry':{'low':10,'high':None,'invalidate_below':9.5,'condition':'等待核验'},
                       'endpoints':{'D1':'2026-11-05','D3':None},
                       'entry_reference':{'fact_id':self.old,'at':DAY+'T15:00:00+08:00','price':10,
                                          'confirmed_at':DAY+'T18:00:00+08:00','kind':'close','symbol':SYMBOL,
                                          'conditions_confirmed':False,'adjustment_verified':None},
                       'entry_peer_refs':[{'fact_id':self.latest,'symbol':SYMBOL,'at':DAY+'T15:00:00+08:00','price':11}],
                       'removal_reference':None,'removal_peer_refs':[],
                       'structure_guard':{'below':9,'basis':'原冻结结构'},
                       'rise':{'type':'breakout','trigger_above':11,'invalidation_price':None},
                       'published_at':DAY+'T18:00:00+08:00','adjustment_verified':False,
                       'last_review':{'at':DAY+'T18:00:00+08:00','evidence_through':DAY+'T17:00:00+08:00','state':'observe'},
                       'invalidation':'原失效条件','first_removed_at':None,'thesis_key':'isolated','research_horizon':'D3'}
        with store.db(admin=True,database=SOURCE) as conn:
            conn.execute('INSERT INTO fact_origins VALUES(%s,%s,%s,%s,%s)',
                         (self.old,'bs_daily','legacy-test',DAY+'T16:30:00+08:00',DAY+'T17:00:00+08:00'))
            conn.execute('INSERT INTO evidence_refs VALUES(%s,%s,%s,%s)',('run',RUN,self.old,DAY+'T18:00:00+08:00'))
            conn.execute('INSERT INTO models VALUES(1,%s,%s,%s)',('隔离模型',store.dumps({'enabled':True}),b'migration-private-sentinel'))
            conn.execute('INSERT INTO research_runs(id,request_key,status,stage,created,updated,market_day,kind,data,report) '
                         'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                         (RUN,'a'*64,'completed_limited','隔离签发',DAY+'T17:30:00+08:00',DAY+'T18:00:00+08:00',DAY,'discovery',store.dumps(data),store.dumps(report)))
            conn.execute('INSERT INTO research_calls(id,run_id,model_id,started,finished,status,data) VALUES(%s,%s,1,%s,%s,%s,%s)',
                         ('2222222222222222',RUN,DAY+'T17:30:00+08:00',DAY+'T17:31:00+08:00','complete',
                          store.dumps({'attempt':2,'recoverable':False,'usage_state':'complete','usage_normalized':{'input':20,'output':5,'total':25,'cached_input':4,'reasoning_output':1}})))
            conn.execute('INSERT INTO research_cycles(id,identity_key,run_id,symbol,market_day,created,updated,state,formal,data) '
                         'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                         (CYCLE,'c'*64,RUN,SYMBOL,DAY,DAY+'T18:00:00+08:00',DAY+'T18:00:00+08:00','observe',False,store.dumps(self.contract)))
            job={'source':'cninfo_announcements','dataset':'announcements','status':'completed','trigger':'daily',
                 'done':2,'total':2,'received':2,'added':1,'unchanged':1,'retry_at':0,'created':DAY+'T19:00:00+08:00',
                 'start':DAY,'end':DAY,'expires_at':DAY+'T21:00:00+08:00'}
            conn.execute('INSERT INTO collection_jobs(id,identity,status,created,data) VALUES(%s,%s,%s,%s,%s)',
                         (JOB,'b'*64,'completed',job['created'],store.dumps(job)))
            schedule={'source':'cninfo_announcements','kind':'daily','enabled':True,'trading_only':True,'at':'19:00',
                      'interval':0,'last_slot':DAY,'last_job':JOB}
            conn.execute('INSERT INTO schedules VALUES(%s,%s)',('announcements',store.dumps(schedule)))
        self.refresh_source_stats()

    def test_stage_preserves_values_ids_metadata_and_projects_query_columns(self):
        receipt=migration.stage(SOURCE,self.target,batch_size=2)
        self.assertEqual(receipt['status'],'validated')
        self.assertEqual(receipt['facts']['count'],len(self.payloads))
        self.assertEqual(receipt['facts']['current_fingerprint_algorithm_mismatches'],1)
        with store.db(readonly=True,admin=True,database=self.target) as conn:
            self.assertNotIn('payload',migration._columns(conn,'facts'))
            self.assertEqual(record_store.load_many(conn,list(self.payloads)),self.payloads)
            row=conn.execute('SELECT fingerprint,acquisitions,original_id,last_original_id FROM facts WHERE id=%s',(self.old,)).fetchone()
            self.assertEqual(bytes(row[0]),bytes(32));self.assertEqual(tuple(row)[1:],(3,42,43))
            self.assertEqual(conn.execute('SELECT fact_id FROM heads WHERE dataset=%s',('daily',)).fetchone()[0],self.latest)
            self.assertEqual(conn.execute('SELECT secret FROM models WHERE id=1').fetchone()[0],b'migration-private-sentinel')
            self.assertEqual(tuple(conn.execute('SELECT session,market_coverage,decision_count FROM research_runs WHERE id=%s',(RUN,)).fetchone()),('close',.8,1))
            self.assertEqual(tuple(conn.execute('SELECT input_tokens,total_tokens FROM research_calls').fetchone()),(20,25))
            self.assertEqual(tuple(conn.execute('SELECT done,total,source FROM collection_jobs WHERE id=%s',(JOB,)).fetchone()),(2,2,'cninfo_announcements'))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM research_scope_members').fetchone()[0],1)
            self.assertEqual(migration._contract_value(state_store.load_cycle_data(conn,CYCLE)),migration._contract_value(self.contract))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM research_cycle_endpoints WHERE cycle_id=%s',(CYCLE,)).fetchone()[0],2)
        self.assertNotIn('migration-private-sentinel',json.dumps(receipt))

    def test_sync_copies_new_versions_metadata_and_source_deletions(self):
        migration.stage(SOURCE,self.target,batch_size=3)
        self.new=self.fact('daily',{'day':'2026-11-05','symbol':SYMBOL,'open':11,'high':12,'low':10,'close':11,
                                    'preclose':11,'volume':200,'amount':2200,'adjust':'raw'},'bs_daily_all')
        with store.db(admin=True,database=SOURCE) as conn:
            conn.execute('UPDATE facts SET acquisitions=8,last_seen=%s WHERE id=%s',('2026-11-06T18:00:00+08:00',self.old))
            conn.execute('DELETE FROM heads WHERE fact_id=%s',(self.notice,))
            conn.execute('DELETE FROM facts WHERE id=%s',(self.notice,))
        self.payloads.pop(self.notice)
        self.refresh_source_stats()
        receipt=migration.synchronize(SOURCE,self.target,batch_size=2)
        self.assertEqual(receipt['source_removed_facts_pruned_from_target'],1)
        with store.db(readonly=True,admin=True,database=self.target) as conn:
            self.assertEqual(record_store.load_many(conn,list(self.payloads)),self.payloads)
            self.assertEqual(conn.execute('SELECT acquisitions FROM facts WHERE id=%s',(self.old,)).fetchone()[0],8)
            self.assertIsNone(conn.execute('SELECT fact_id FROM announcements WHERE fact_id=%s',(self.notice,)).fetchone())
        self.assertEqual(migration.validate(SOURCE,self.target,batch_size=2)['status'],'validated')

    def test_validation_reports_typed_corruption_and_projection_corruption(self):
        migration.stage(SOURCE,self.target,batch_size=2)
        with store.db(admin=True,database=self.target) as conn:
            conn.execute('UPDATE announcements SET text=%s WHERE fact_id=%s',('不一致的正文',self.notice))
        with self.assertRaisesRegex(migration.MigrationError,'Typed value differs'):
            migration.validate(SOURCE,self.target,batch_size=2)
        with store.db(admin=True,database=self.target) as conn:
            conn.execute('UPDATE announcements SET text=%s WHERE fact_id=%s',(self.payloads[self.notice]['text'],self.notice))
            conn.execute('UPDATE research_runs SET market_coverage=.1 WHERE id=%s',(RUN,))
        with self.assertRaisesRegex(migration.MigrationError,'Research projection differs'):
            migration.validate(SOURCE,self.target,batch_size=2)
        with store.db(admin=True,database=self.target) as conn:
            conn.execute('UPDATE research_runs SET market_coverage=.8 WHERE id=%s',(RUN,))
            conn.execute('UPDATE research_cycle_contracts SET entry_low=123 WHERE cycle_id=%s',(CYCLE,))
        with self.assertRaisesRegex(migration.MigrationError,'Cycle contract differs'):
            migration.validate(SOURCE,self.target,batch_size=2)

    def test_production_target_is_rejected_before_any_write(self):
        with self.assertRaises(migration.MigrationError):
            migration.stage(SOURCE,'stock_foundation')


class AutoIncrementHighWaterTests(unittest.TestCase):
    """Exercise engine counters with two tiny schemas, without the full migration."""

    def test_deleted_high_ids_and_existing_target_high_water_are_preserved(self):
        source='stock_test_migration_water_src_'+uuid.uuid4().hex[:8]
        target='stock_test_migration_water_dst_'+uuid.uuid4().hex[:8]
        created=[]
        with tempfile.TemporaryDirectory(prefix='stock-migration-water-') as temporary:
            with patch.dict(os.environ,{'STOCK_DATA_ROOT':temporary,'STOCK_MYSQL_DATABASE':source,'STOCK_DISABLE_WORKER':'1'}):
                try:
                    for schema in (source,target):
                        with store.db(admin=True,database='stock_foundation') as admin:
                            admin.execute('CREATE DATABASE '+migration._ident(schema))
                            created.append(schema)
                        with store.db(admin=True,database=schema) as conn:
                            conn.execute('CREATE TABLE models(id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,name VARCHAR(30) UNIQUE) ENGINE=InnoDB')
                            conn.execute('INSERT INTO models(id,name) VALUES(1,%s)',('retained',))
                    for schema,high in ((source,600),(target,900)):
                        with store.db(admin=True,database=schema) as conn:
                            conn.execute('INSERT INTO models(id,name) VALUES(%s,%s)',(high,'deleted'))
                            conn.execute('DELETE FROM models WHERE id=%s',(high,))
                    with store.db(admin=True,database=source) as src:
                        with store.db(admin=True,database=target) as dst:
                            self.assertEqual(src.execute('SELECT MAX(id) FROM models').fetchone()[0],1)
                            receipt=migration._sync_auto_increment(src,dst)
                            self.assertEqual(receipt['models']['source_next'],601)
                            self.assertEqual(receipt['models']['target_before'],901)
                            self.assertEqual(receipt['models']['target_next'],901)
                            src.execute('ALTER TABLE models AUTO_INCREMENT=1500')
                            receipt=migration._sync_auto_increment(src,dst)
                            self.assertEqual(receipt['models']['target_next'],1500)
                            self.assertEqual(migration._validate_auto_increment(src,dst)['models']['source_next'],1500)
                            dst.execute('ALTER TABLE models AUTO_INCREMENT=2')
                            with self.assertRaisesRegex(migration.MigrationError,'AUTO_INCREMENT high-water'):
                                migration._validate_auto_increment(src,dst)
                            migration._sync_auto_increment(src,dst)
                            self.assertEqual(dst.execute('INSERT INTO models(name) VALUES(%s)',('next',)).lastrowid,1500)
                            src.execute('ALTER TABLE models AUTO_INCREMENT=2000')
                            with self.assertRaises(store.IntegrityError):
                                src.execute('INSERT INTO models(name) VALUES(%s)',('retained',))
                            # InnoDB reserves the generated id even when a unique
                            # key rejects the row; MAX(id) still remains one.
                            self.assertEqual(src.execute('SELECT MAX(id) FROM models').fetchone()[0],1)
                            self.assertEqual(migration._auto_increment_values(src)['models'],2001)
                            migration._sync_auto_increment(src,dst)
                            self.assertEqual(dst.execute('INSERT INTO models(name) VALUES(%s)',('after-failure',)).lastrowid,2001)
                finally:
                    store.close_pool()
                    for schema in reversed(created):
                        with store.db(admin=True,database='stock_foundation') as admin:
                            admin.execute('DROP DATABASE '+migration._ident(schema))

    def test_restore_prefix_is_accepted_but_formal_and_same_schema_are_rejected(self):
        migration._validate_names('stock_foundation','stock_restore_backup_import_test')
        with self.assertRaises(migration.MigrationError):
            migration._validate_names('stock_foundation','stock_foundation')
        with self.assertRaises(migration.MigrationError):
            migration._validate_names('stock_restore_backup_import_test','stock_restore_backup_import_test')


class CompositePaginationTests(unittest.TestCase):
    def test_all_boundaries_and_content_verified_head_skip(self):
        source='stock_test_migration_page_src_'+uuid.uuid4().hex[:8]
        target='stock_test_migration_page_dst_'+uuid.uuid4().hex[:8]
        created=[]
        columns=('dataset','day','symbol','record_key','fact_id')
        keys=columns[:-1]
        with tempfile.TemporaryDirectory(prefix='stock-migration-pages-') as temporary:
            with patch.dict(os.environ,{'STOCK_DATA_ROOT':temporary,'STOCK_MYSQL_DATABASE':source,'STOCK_DISABLE_WORKER':'1'}):
                try:
                    for schema in (source,target):
                        with store.db(admin=True,database='stock_foundation') as admin:
                            admin.execute('CREATE DATABASE '+migration._ident(schema)+' CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
                            created.append(schema)
                        with store.db(admin=True,database=schema) as conn:
                            conn.execute('CREATE TABLE heads(dataset VARCHAR(24) NOT NULL,day DATE NOT NULL,'
                                         'symbol VARCHAR(24) NOT NULL,record_key VARCHAR(40) NOT NULL,fact_id BIGINT NOT NULL,'
                                         'PRIMARY KEY(dataset,day,symbol,record_key)) ENGINE=InnoDB')
                    values=[]
                    for dataset in ('calendar','daily','financials'):
                        for day in ('2026-09-01','2026-09-02','2026-10-01'):
                            for symbol in ('sh.600000','sz.000001'):
                                for key in ('','report','中文'):
                                    values.append(dict(zip(columns,(dataset,day,symbol,key,len(values)+1))))
                    with store.db(admin=True,database=source) as src:
                        migration._insert(src,'heads',columns,values);src.raw.commit()
                        expected=[dict(row) for row in src.execute('SELECT * FROM heads ORDER BY dataset,day,symbol,record_key')]
                        for batch_size in (1,2,7,54,55):
                            actual=[row for page in migration._pages(src,'heads',columns,keys,batch_size) for row in page]
                            self.assertEqual(actual,expected)
                        with store.db(admin=True,database=target) as dst:
                            copied,skipped=migration._copy_ordinary(src,dst,{'heads'},7)
                            self.assertEqual(skipped,[]);self.assertEqual(copied['heads']['count'],54)
                            with patch.object(dst,'execute',wraps=dst.execute) as query:
                                with patch.object(dst,'executemany',wraps=dst.executemany) as write:
                                    copied,skipped=migration._copy_ordinary(src,dst,{'heads'},7)
                                    self.assertEqual(skipped,['heads'])
                                    self.assertFalse(any(call.args[0].startswith('DELETE FROM `heads`') for call in query.call_args_list))
                                    self.assertEqual(write.call_count,0)
                            # Preserve count while changing a referenced fact: a
                            # count-only shortcut would incorrectly skip this.
                            src.execute('UPDATE heads SET fact_id=fact_id+100 WHERE dataset=%s AND day=%s AND symbol=%s AND record_key=%s',
                                        ('daily','2026-09-02','sh.600000',''))
                            src.raw.commit()
                            with patch.object(dst,'execute',wraps=dst.execute) as query:
                                copied,skipped=migration._copy_ordinary(src,dst,{'heads'},7)
                                self.assertEqual(skipped,[])
                                self.assertTrue(any(call.args[0].startswith('DELETE FROM `heads`') for call in query.call_args_list))
                            self.assertEqual(migration._table_digest(src,'heads',columns,keys,7),
                                             migration._table_digest(dst,'heads',columns,keys,7))
                finally:
                    store.close_pool()
                    for schema in reversed(created):
                        with store.db(admin=True,database='stock_foundation') as admin:
                            admin.execute('DROP DATABASE '+migration._ident(schema))

