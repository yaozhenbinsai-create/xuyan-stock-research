"""The policy clock -> durable collector -> typed storage path, isolated only."""
from datetime import datetime
from hashlib import sha256
import time
import unittest
import uuid
from unittest.mock import patch

from foundation import assets, collection, database as store, sources
from foundation.policy_feed import PolicyBatch
from tools.mysql_instance import create_isolated_database, drop_isolated_database
import test_foundation as fixtures

SCHEMA='stock_test_policy_clock_'+uuid.uuid4().hex[:8]
DAY='2026-11-20'


def setUpModule():create_isolated_database(SCHEMA)
def tearDownModule():drop_isolated_database(SCHEMA)


class PolicyScheduleTests(fixtures.IsolatedCase):
    @classmethod
    def setUpClass(cls):cls.database=SCHEMA

    def test_automatic_clock_queues_once_and_resumes_frozen_policy_pages_into_storage(self):
        store.set_setting('automation_paused',False)
        collection.ensure_foundation_schedules()
        worker=collection.Engine();worker.last_maintenance=time.monotonic()
        now=datetime.fromisoformat(DAY+'T08:21:00+08:00')
        with patch.object(store,'today',return_value=DAY),patch.object(assets,'local_now',return_value=now):
            plans=[p for p in collection.schedules() if p['id']=='policy_morning']
            with patch.object(collection,'schedules',return_value=plans):worker.tick()
            plans=[p for p in collection.schedules() if p['id']=='policy_morning']
            with patch.object(collection,'schedules',return_value=plans):worker.tick()
        with store.db(readonly=True) as c:
            rows=c.execute("SELECT id,`trigger` FROM collection_jobs WHERE schedule_id='policy_morning'").fetchall()
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['trigger'],'schedule')
        urls=[f'https://www.gov.cn/zhengce/content/202611/content_{number}.htm' for number in range(1001,1005)]
        def record(url):
            return {'day':DAY,'published':DAY,'title':'制造业政策'+url[-8:],'symbol':'*',
                    'key':sha256(url.encode()).hexdigest(),'url':url,
                    'text':'【原文第 1 段】\n制造业支持政策。'*20,'document_sha256':sha256(url.encode()).hexdigest(),
                    '_terms':['制造业','没有出现的术语']}
        requests=[]
        def fetch(src,params):
            requests.append(params)
            if len(requests)==1:
                return PolicyBatch([record(url) for url in urls[:3]],checked=True,
                    diagnostics=[{'status':'checked','url':'https://www.gov.cn/zhengce/'}],
                    page_info={'next':2,'next_params':{'policy_snapshot':urls}})
            self.assertEqual(params['policy_snapshot'],urls)
            return PolicyBatch([record(urls[3])],checked=True,page_info={'next':None})
        with patch.object(sources,'collect',side_effect=fetch):
            worker.execute_unit(worker.claim())
            first=collection.get_job(rows[0]['id'])
            self.assertEqual(first['policy_snapshot'],urls)
            self.assertTrue(first['items'][1]['policy_snapshot_ref'])
            self.assertNotIn('policy_snapshot',first['items'][1])
            worker.last_provider.clear()
            worker.execute_unit(worker.claim())
        final=collection.get_job(rows[0]['id'])
        self.assertEqual((final['status'],final['done'],final['total'],final['added'],final['no_data']),('completed',2,2,4,0))
        with store.db(readonly=True) as c:
            facts=c.execute("SELECT fact_id FROM heads WHERE dataset='policies'").fetchall()
            self.assertEqual(len(facts),4)
            self.assertEqual(store.setting('policy_terms:'+str(facts[0][0]),[],conn=c),['制造业'])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM research_calls').fetchone()[0],0)

    def test_checked_no_new_policies_is_distinct_from_upstream_failure(self):
        checked=PolicyBatch(checked=True,diagnostics=[{'status':'checked_no_new','message':'已核对栏目，无新增原文'}])
        with patch('foundation.policy_feed.collect',return_value=checked):
            self.assertIs(sources.collect(assets.source('official_policy'),{'start':DAY,'end':DAY}),checked)
        with patch('foundation.policy_feed.collect',side_effect=sources.SourceError('官方原件读取失败','network')):
            with self.assertRaises(sources.SourceError):
                sources.collect(assets.source('official_policy'),{'start':DAY,'end':DAY})


if __name__=='__main__':unittest.main()
