"""Small real-MySQL risk-availability regression, never using production tables."""
import hashlib, os, tempfile, unittest, uuid
from unittest.mock import patch
from foundation import database as store,record_store
from research import evidence
from tools.mysql_instance import create_isolated_database, drop_isolated_database

class RiskInvalidationMySQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema='stock_test_risk_availability_'+uuid.uuid4().hex[:12]
        cls.temp=tempfile.TemporaryDirectory(prefix='stock-risk-availability-')
        cls.env=patch.dict(os.environ,{'STOCK_DATA_ROOT':cls.temp.name,'STOCK_MYSQL_DATABASE':cls.schema,'STOCK_DISABLE_WORKER':'1'})
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
        self.assertTrue(self.schema.startswith('stock_test_'))
        self.assertEqual(store.description()['name'],self.schema)
        with store.db() as conn:
            for table in ('heads','facts','settings'):conn.execute('DELETE FROM `'+table+'`')
            base=self.insert(conn,'base','2026-11-12','2026-11-13T09:00:00+08:00','已读公司公告')
            store.set_setting('research_check:sh.600000:risk',{'state':'verified','fact_ids':[base],
                'confirmed_at':'2026-11-13T10:00:00+08:00','valid_until':'2026-11-15','note':'隔离已读核验'},conn=conn)
    def insert(self,conn,key,day,seen,title):
        payload={'day':day,'symbol':'sh.600000','key':key,'title':title,'text':'隔离原文'}
        fid=conn.execute('INSERT INTO facts(dataset,day,symbol,record_key,source,payload_bytes,fingerprint,first_seen,last_seen) VALUES(%s,%s,%s,%s,%s,%s,UNHEX(%s),%s,%s)',
            ('announcements',day,'sh.600000',key,'cninfo_text',len(store.dumps(payload).encode()),hashlib.sha256(key.encode()).hexdigest(),seen,seen)).lastrowid
        record_store.save(conn,fid,'announcements',payload)
        conn.execute('INSERT INTO heads VALUES(%s,%s,%s,%s,%s)',('announcements',day,'sh.600000',key,fid))
        return fid
    def state(self):
        with store.db(readonly=True) as conn:return evidence.read_checks(conn,'sh.600000','2026-11-13T12:00:00+08:00')['risk']['state']
    def test_old_publication_discovered_after_check_blocks(self):
        with store.db() as conn:self.insert(conn,'late-old','2026-11-12','2026-11-13T11:00:00+08:00','重大诉讼公告')
        self.assertEqual(self.state(),'blocked')
    def test_risk_is_not_hidden_behind_thirty_five_routine_notices(self):
        with store.db() as conn:
            self.insert(conn,'risk-old','2026-11-12','2026-11-13T10:30:00+08:00','行政处罚公告')
            for i in range(35):self.insert(conn,'ordinary-'+str(i),'2026-11-13','2026-11-13T11:00:00+08:00','董事会会议决议')
        self.assertEqual(self.state(),'blocked')
    def test_future_availability_does_not_invalidate_earlier_freeze(self):
        with store.db() as conn:self.insert(conn,'future','2026-11-12','2026-11-13T12:01:00+08:00','行政处罚公告')
        self.assertEqual(self.state(),'verified')
    def test_routine_new_notice_does_not_invalidate(self):
        with store.db() as conn:self.insert(conn,'ordinary','2026-11-13','2026-11-13T11:00:00+08:00','董事会会议决议')
        self.assertEqual(self.state(),'verified')

if __name__=='__main__':unittest.main()
