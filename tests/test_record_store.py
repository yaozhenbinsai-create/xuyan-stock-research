"""Relational fact fidelity and reference integrity in a disposable MySQL schema."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from foundation import database as store, record_store, storage_policy
from tools.mysql_instance import create_isolated_database, drop_isolated_database


class RecordStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = 'stock_test_record_store_'+uuid.uuid4().hex[:10]
        cls.temp = tempfile.TemporaryDirectory(prefix='stock-record-store-')
        cls.env = patch.dict(os.environ, {'STOCK_DATA_ROOT':cls.temp.name,
            'STOCK_MYSQL_DATABASE':cls.schema, 'STOCK_DISABLE_WORKER':'1'})
        cls.env.start()
        try:
            create_isolated_database(cls.schema)
            with store.db(admin=True) as conn:
                for statement in record_store.schema_sql().split(';'):
                    if statement.strip():conn.execute(statement)
            with store.db(readonly=True) as conn:
                cls.catalogue = {row[0]:row[1] for row in conn.execute('SHOW COLUMNS FROM facts')}
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

    def insert(self, conn, dataset, values):
        payload = storage_policy.normalize(dataset, {'day':'2026-11-13','symbol':'sh.600000',**values})
        digest = hashlib.sha256(uuid.uuid4().bytes).digest()
        fingerprint = digest if 'binary' in self.catalogue['fingerprint'].lower() else digest.hex()
        row = {'dataset':dataset, 'day':payload['day'], 'symbol':payload['symbol'],
               'record_key':payload['key'], 'source':'isolated_record_store',
               'source_version':'', 'fingerprint':fingerprint,
               'first_seen':'2026-11-13T19:00:00+08:00','last_seen':'2026-11-13T19:00:00+08:00'}
        if 'payload_bytes' in self.catalogue:
            row['payload_bytes'] = len(store.dumps(payload).encode())
        if 'payload' in self.catalogue:
            row['payload'] = store.dumps(payload)
        keys = tuple(row)
        result = conn.execute('INSERT INTO facts ('+','.join('`'+key+'`' for key in keys)+') VALUES ('+','.join(['%s']*len(keys))+')',tuple(row[key] for key in keys))
        return result.lastrowid,payload

    def assert_payload(self, actual, expected):
        # Canonical JSON also distinguishes int/float, negative zero and null.
        self.assertEqual(store.dumps(actual),store.dumps(expected))

    def test_all_business_datasets_roundtrip_without_a_serialized_store(self):
        examples = {
            'calendar':{'symbol':'*','trading':True,'markets':['sh','sz','bj']},
            'basics':{'name':'隔离证券','listed':'2000-01-01','delisted':'','status':'1','board':'主板'},
            'industry':{'group':'C39计算机','taxonomy':'证监会','source_effective_date':None,
                        'historical_effective_unknown':True,'unclassified':False},
            'concept':{'group':'概念样本','taxonomy':'已核来源','source_effective_date':'2026-11-13'},
            'area':{'group':'上海','taxonomy':None},
            'daily':{'open':10,'high':10.5,'low':9.7,'close':10.1,'preclose':10.0,'amount':12345.67,
                     'volume':10000,'turnover':None,'suspended':False,'is_st':False,'pe':None},
            'quotes':{'name':'隔离证券','close':10.1,'source_time':'2026-11-13T14:35:00+08:00','volume_unit':'shares'},
            'minutes':{'close':10.1,'period':5,'source_time':'20261113143500123','closed':True},
            'financials':{'key':'profit:2026-09-30','report_type':'profit','report_date':'2026-09-30',
                          'published':'2026-10-28','fields':{'netProfit':'000123.4500','MBRevenue':'',
                          'unknown':None,'ratio':0.12345678901234567,'large_integer':10**60,'flag':False}},
            'announcements':{'title':'隔离公告','published':'2026-11-13T18:30:01.123456+08:00',
                             'url':'https://example.invalid/document','text':'正文节选',
                             'text_extracted_at':'2026-11-13T18:40:00+08:00','text_pages':2,'total_pages':8,
                             'text_scope':'前两页','text_truncated':False},
            'policies':{'title':'隔离政策','published':'2026-11-13','effective_day':'2026-12-01',
                        'url':'https://example.invalid/policy','text':'正文','document_sha256':'a'*64},
            'actions':{'record_kind':'event','fields':{'foreAdjustFactor':0.12345678901234567,
                       'backAdjustFactor':None,'adjustFactor':1}},
            'fund_flows':{'net_main':-12345.6789,'net_large':None,'ratio_main':-0.0,
                          'unit':'CNY','basis':'isolated','time_precision':'day'},
        }
        with store.db() as conn:
            items = [(*self.insert(conn,dataset,values),dataset) for dataset,values in examples.items()]
            record_store.save_many(conn,[(fid,dataset,payload) for fid,payload,dataset in items])
            loaded = record_store.load_many(conn,[fid for fid,_,_ in reversed(items)])
            for fid,payload,dataset in items:
                with self.subTest(dataset=dataset):self.assert_payload(loaded[fid],payload)
            hydrated = record_store.hydrate(conn,[{'id':fid,'source':'kept-metadata'} for fid,_,_ in reversed(items)])
            self.assertEqual([row['id'] for row in hydrated],[fid for fid,_,_ in reversed(items)])
            self.assertTrue(all(isinstance(row['payload'],dict) and row['source']=='kept-metadata' for row in hydrated))
            columns = conn.execute('SELECT TABLE_NAME,COLUMN_NAME,DATA_TYPE FROM information_schema.columns '
                                   'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ('+','.join(['%s']*len(record_store.TABLES))+')',record_store.TABLES).fetchall()
            self.assertFalse(any(row['DATA_TYPE']=='json' or row['COLUMN_NAME']=='payload' for row in columns))

    def test_missing_null_empty_and_defaulted_fields_stay_distinct(self):
        examples = [('calendar',{'symbol':'*','trading':False}),
                    ('calendar',{'symbol':'*','trading':False,'markets':None}),
                    ('calendar',{'symbol':'*','trading':False,'markets':[]}),
                    ('financials',{}),('financials',{'fields':None}),('financials',{'fields':{}}),
                    ('basics',{'name':'缺省上市日'}),('basics',{'name':'未知上市日','listed':None}),
                    ('basics',{'name':'空上市日','listed':''}),('daily',{'close':0.0,'preclose':None})]
        with store.db() as conn:
            for dataset,value in examples:
                fid,payload = self.insert(conn,dataset,value)
                record_store.save(conn,fid,dataset,payload)
                self.assert_payload(record_store.load(conn,fid),payload)

    def test_dimension_content_deduplicates_across_days_without_erasing_presence(self):
        unique = uuid.uuid4().hex
        with store.db() as conn:
            one,p1 = self.insert(conn,'basics',{'name':unique,'board':'主板'})
            two,p2 = self.insert(conn,'basics',{'day':'2026-11-16','name':unique,'board':'主板'})
            three,p3 = self.insert(conn,'basics',{'day':'2026-11-17','name':unique,'board':'主板','listed':None})
            record_store.save_many(conn,[(one,'basics',p1),(two,'basics',p2),(three,'basics',p3)])
            refs = {row['fact_id']:row['security_id'] for row in conn.execute('SELECT fact_id,security_id FROM security_records WHERE fact_id IN (%s,%s,%s)',(one,two,three))}
            self.assertEqual(refs[one],refs[two]);self.assertNotEqual(refs[one],refs[three])
            for fid,payload in ((one,p1),(two,p2),(three,p3)):
                self.assert_payload(record_store.load(conn,fid),payload)
            a,pa = self.insert(conn,'industry',{'group':unique,'taxonomy':'same'})
            b,pb = self.insert(conn,'industry',{'day':'2026-11-16','group':unique,'taxonomy':'same'})
            record_store.save_many(conn,[(a,'industry',pa),(b,'industry',pb)])
            refs = conn.execute('SELECT DISTINCT classification_id FROM classification_records WHERE fact_id IN (%s,%s)',(a,b)).fetchall()
            self.assertEqual(len(refs),1)

    def test_numeric_and_timestamp_precision_survive_typed_sql(self):
        cases = [('daily',{'close':0.12345678901234567,'volume':2**60+1,'amount':-0.0}),
                 ('quotes',{'source_time':'2026-11-13T06:30:01.120+00:00','close':1.0000000000000002}),
                 ('quotes',{'source_time':'2026-11-13 14:30:00Z'}),
                 ('minutes',{'source_time':'20261113113000123','period':5}),
                 ('fund_flows',{'source_time':'2026-11-13T14:35','net_main':1.0})]
        with store.db() as conn:
            ids = []
            for dataset,value in cases:
                fid,payload = self.insert(conn,dataset,value);ids.append(fid)
                record_store.save(conn,fid,dataset,payload)
                self.assert_payload(record_store.load(conn,fid),payload)
            stamp = conn.execute('SELECT HOUR(source_time),MICROSECOND(source_time) FROM quote_snapshots WHERE fact_id=%s',(ids[1],)).fetchone()
            self.assertEqual(tuple(stamp),(14,120000))

    def test_fact_delete_cascades_business_and_child_rows_but_preserves_shared_dimensions(self):
        with store.db() as conn:
            fid,payload = self.insert(conn,'financials',{'fields':{'netProfit':'120'}})
            record_store.save(conn,fid,'financials',payload)
            conn.execute('DELETE FROM facts WHERE id=%s',(fid,))
            for table in ('financial_reports','financial_values'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM `'+table+'` WHERE fact_id=%s',(fid,)).fetchone()[0],0)
            self.assertEqual(record_store.load_many(conn,[fid]),{})
            with self.assertRaises(KeyError):record_store.load(conn,fid)

    def test_missing_business_row_is_reported_as_corruption_and_nested_fields_are_rejected(self):
        with store.db() as conn:
            fid,payload = self.insert(conn,'daily',{'close':10.0})
            with self.assertRaisesRegex(RuntimeError,'missing'):
                record_store.load(conn,fid)
            fid,payload = self.insert(conn,'financials',{'fields':{'unexpected':{'nested':1}}})
            with self.assertRaisesRegex(ValueError,'scalar'):
                record_store.save(conn,fid,'financials',payload)

    def test_schema_file_matches_repository_contract(self):
        self.assertEqual((Path(__file__).resolve().parents[1]/'foundation/records_schema.sql').read_text(encoding='utf-8'),record_store.schema_sql())

    def test_saved_legacy_nulls_survive_without_running_new_acquisition_normalization(self):
        with store.db() as conn:
            fid,payload = self.insert(conn,'daily',{'close':10.0})
            historical = {**payload,'pe':None,'pb':None}
            record_store.save(conn,fid,'daily',historical)
            self.assert_payload(record_store.load(conn,fid),historical)
            with self.assertRaisesRegex(ValueError,'Unmapped'):
                record_store.save(conn,fid,'daily',{**historical,'unexpected_legacy_field':'must not disappear'})
            with patch.dict(storage_policy.FIELDS,{'daily':storage_policy.FIELDS['daily']|{'future_unmapped'}}):
                with self.assertRaisesRegex(ValueError,'Unmapped'):
                    record_store.save(conn,fid,'daily',{**historical,'future_unmapped':1})


if __name__=='__main__':unittest.main()
