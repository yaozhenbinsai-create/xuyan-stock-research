"""Announcement-index refreshes must not discard a matching extracted original."""
import copy
import json
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from foundation import assets, database as store, record_store
from tools.mysql_instance import create_isolated_database, drop_isolated_database


SCHEMA='stock_test_announcement_'+uuid.uuid4().hex[:10]


def setUpModule():create_isolated_database(SCHEMA)
def tearDownModule():drop_isolated_database(SCHEMA)


class AnnouncementReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='stock-announcement-review-')
        self.addCleanup(self.temp.cleanup)
        env=patch.dict(os.environ,{'STOCK_DATA_ROOT':self.temp.name,'STOCK_MYSQL_DATABASE':SCHEMA,'STOCK_DISABLE_WORKER':'1'})
        env.start();self.addCleanup(env.stop)
        self.index={'day':'2026-11-12','symbol':'sh.600000','key':self._testMethodName,
                    'title':'2026年第三季度报告','published':'2026-11-12T08:00:00+08:00',
                    'url':'https://static.cninfo.com.cn/test-review.pdf','text':None}
        self.original={**self.index,'text':'【PDF 第 1 页】真实隔离正文，包含营业收入及经营活动现金流。',
                       'text_extracted_at':'2026-11-12T09:00:00+08:00'}

    def insert(self,value,source,stamp):
        with store.db() as conn:
            return assets.insert_fact(conn,'announcements',source,value,observed=stamp)

    def head(self,value=None):
        value=value or self.index
        with store.db(readonly=True) as conn:
            row=conn.execute("SELECT f.id,f.first_seen FROM heads h JOIN facts f ON f.id=h.fact_id "
                             "WHERE h.dataset='announcements' AND h.day=%s AND h.symbol=%s AND h.record_key=%s",
                             (value['day'],value['symbol'],value['key'])).fetchone()
            payload=record_store.load(conn,row['id'])
        return {'id':row['id'],'payload':payload,'first_seen':row['first_seen']}

    def test_index_original_same_index_keeps_original_head_and_index_acquisition(self):
        iid,_=self.insert(self.index,'cninfo_announcements','2026-11-12T08:10:00+08:00')
        tid,_=self.insert(self.original,'cninfo_text','2026-11-12T09:00:00+08:00')
        again,inserted=self.insert(self.index,'cninfo_announcements','2026-11-12T10:00:00+08:00')
        self.assertEqual(again,iid);self.assertFalse(inserted)
        head=self.head();self.assertEqual(head['id'],tid);self.assertTrue(head['payload']['text'])
        self.assertEqual(head['first_seen'],'2026-11-12T09:00:00+08:00')
        with store.db(readonly=True) as conn:
            index=conn.execute('SELECT acquisitions,first_seen,last_seen FROM facts WHERE id=%s',(iid,)).fetchone()
        self.assertEqual(index['acquisitions'],2)
        self.assertEqual(index['first_seen'],'2026-11-12T08:10:00+08:00')
        self.assertEqual(index['last_seen'],'2026-11-12T10:00:00+08:00')

    def test_real_metadata_revision_points_to_new_index_without_old_text(self):
        for field,value in [('title','更正后的第三季度报告'),('published','2026-11-12T08:05:00+08:00'),
                            ('url','https://static.cninfo.com.cn/revised-test-review.pdf')]:
            with self.subTest(field=field):
                index={**self.index,'key':self.index['key']+field}
                original={**self.original,'key':index['key']}
                self.insert(index,'cninfo_announcements','2026-11-12T08:10:00+08:00')
                old_id,_=self.insert(original,'cninfo_text','2026-11-12T09:00:00+08:00')
                revised={**index,field:value}
                new_id,inserted=self.insert(revised,'cninfo_announcements','2026-11-12T10:00:00+08:00')
                self.assertTrue(inserted);self.assertNotEqual(new_id,old_id)
                head=self.head(index);self.assertEqual(head['id'],new_id);self.assertFalse(head['payload'].get('text'))
                self.assertEqual(head['payload'][field],value)

    def test_frozen_old_fact_payload_fingerprint_and_knowledge_time_are_unchanged(self):
        iid,_=self.insert(self.index,'cninfo_announcements','2026-11-12T08:10:00+08:00')
        tid,_=self.insert(self.original,'cninfo_text','2026-11-12T09:00:00+08:00')
        with store.db(readonly=True) as conn:
            frozen=record_store.hydrate(conn,conn.execute('SELECT id,fingerprint,first_seen FROM facts WHERE id IN (%s,%s) ORDER BY id',(iid,tid)).fetchall())
        self.insert(copy.deepcopy(self.index),'cninfo_announcements','2026-11-12T10:00:00+08:00')
        with store.db(readonly=True) as conn:
            after=record_store.hydrate(conn,conn.execute('SELECT id,fingerprint,first_seen FROM facts WHERE id IN (%s,%s) ORDER BY id',(iid,tid)).fetchall())
        self.assertEqual(after,frozen)
        self.assertEqual(self.head()['id'],tid)


if __name__=='__main__':unittest.main()
