"""Real session/preparation/rotation arithmetic with a virtual clock and no I/O."""
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

from foundation import database as store
from research import model_transport, preparation, rotation, sessions
from test_rotation_stage_review import StrictStageDB, Result


class Clock:
    def __init__(self,at='2026-11-13T14:36:00+08:00'):self.time=datetime.fromisoformat(at)
    def now(self):return self.time.isoformat(timespec='seconds')
    def advance(self,seconds):self.time+=timedelta(seconds=seconds)


class Database(StrictStageDB):
    def execute(self,sql,params=()):
        if sql.startswith('SELECT id,data FROM models'):
            return Result(rows=[{'id':i,'data':'{"enabled":true}'} for i in (1,2,3)])
        return super().execute(sql,params)


class WindowBudgetTests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock();self.database=Database();self.calls=[];self.jobs=[]
        self.deadline='2026-11-13T14:45:00+08:00'
        self.chain=[{'model':{'id':i,'name':name,'timeout_seconds':180,'max_output_tokens':None},'config_hash':'frozen'}
                    for i,name in ((1,'primary'),(2,'Gemini'),(3,'third'))]
        self.plan={'chain':self.chain,'model':self.chain[0]['model'],'request_timeout_seconds':600,
                   'publish_until':self.deadline,'budget':{'max_requests':None}}

    def prepare(self):
        def submit(raw,**kwargs):
            self.jobs.append({'source':raw['source'],'expires_at':kwargs['expires_at']})
            return {'id':str(len(self.jobs))}
        with patch.object(store,'now',side_effect=self.clock.now),patch.object(store,'setting',return_value=False),\
             patch.object(preparation,'_original_ids',return_value=[]),\
             patch.object(preparation.collection,'submit',side_effect=submit),\
             patch.object(preparation.collection,'get_job',return_value={'status':'running','done':0,'total':1,'received':0}),\
             patch.object(preparation.time,'sleep',side_effect=self.clock.advance):
            preparation.acquire(['sh.600000'],'2026-11-13','tail',self.deadline)

    def execute(self,actions):
        def complete(run_id,selected,system,payload):
            model_id=selected['model']['id'];action,duration=actions[model_id]
            timeout=selected['budget']['timeout_seconds']
            self.calls.append({'model_id':model_id,'timeout':timeout,
                               'configured':selected['budget'].get('configured_timeout_seconds'),
                               'start':self.clock.now()})
            if duration>timeout:
                self.clock.advance(timeout)
                raise model_transport.ModelFailure('stream_deadline','synthetic timeout before remote DONE',unknown=True)
            self.clock.advance(duration)
            if action=='known_fail':raise model_transport.ModelFailure('http_503','synthetic explicit failure',retryable=True)
            call_id='call'+str(model_id)
            self.database.calls[call_id]={'status':'completed','data':'{}'}
            return store.dumps({'valid':action!='invalid'}),call_id
        def validate(text):
            value=json.loads(text)
            if not value['valid']:raise ValueError('synthetic report violates unchanged evidence contract')
            return value
        with patch.object(store,'db',self.database.db),patch.object(store,'now',side_effect=self.clock.now),\
             patch.object(rotation.preferences,'context',return_value={}),\
             patch.object(rotation.state_store,'sync_run'),patch.object(rotation.state_store,'sync_call'),\
             patch.object(model_transport,'complete',side_effect=complete):
            return rotation.execute('run',self.plan,'system',{'all_evidence':'unchanged'},validate)

    def test_tail_preparation_180_then_actual_211_second_primary_can_finish(self):
        self.assertEqual(sessions.SESSIONS['tail']['start'],'14:36')
        self.assertEqual(sessions.SESSIONS['tail']['publish_until'],'14:45')
        self.prepare()
        self.assertEqual(self.clock.now(),'2026-11-13T14:39:00+08:00')
        self.assertEqual({row['source'] for row in self.jobs},{'cninfo_announcements','bs_actions','bs_profit','bs_cashflow'})
        report,call_id=self.execute({1:('ok',211)})
        self.assertTrue(report['valid']);self.assertEqual(call_id,'call1')
        self.assertEqual(self.calls[0]['timeout'],345)
        self.assertEqual(self.clock.now(),'2026-11-13T14:42:31+08:00')

    def test_invalid_primary_after_211_seconds_then_gemini_100_still_meets_hard_deadline(self):
        self.prepare()
        report,call_id=self.execute({1:('invalid',211),2:('ok',100)})
        self.assertTrue(report['valid']);self.assertEqual(call_id,'call2')
        self.assertEqual([row['model_id'] for row in self.calls],[1,2])
        self.assertEqual([row['timeout'] for row in self.calls],[345,134])
        self.assertEqual(self.database.calls['call1']['status'],'invalid')
        self.assertEqual(self.clock.now(),'2026-11-13T14:44:11+08:00')
        self.assertGreaterEqual((datetime.fromisoformat(self.deadline)-self.clock.time).total_seconds(),15)

    def test_unknown_at_hard_deadline_minus_signing_reserve_still_dispatches_only_once(self):
        self.prepare()
        with self.assertRaises(model_transport.ModelFailure) as failure:self.execute({1:('ok',500)})
        self.assertTrue(failure.exception.unknown)
        self.assertEqual([row['model_id'] for row in self.calls],[1])
        self.assertEqual(self.clock.now(),'2026-11-13T14:44:45+08:00')
        self.assertEqual(self.database.status,'unknown')

    def test_no_dispatch_when_only_signing_reserve_remains(self):
        self.clock=Clock('2026-11-13T14:44:46+08:00')
        with self.assertRaises(rotation.Deadline):self.execute({1:('ok',1)})
        self.assertEqual(self.calls,[])

    def test_configured_180_second_model_limit_is_not_silently_extended(self):
        self.prepare();self.plan.pop('request_timeout_seconds')
        with self.assertRaises(model_transport.ModelFailure) as failure:self.execute({1:('ok',211)})
        self.assertTrue(failure.exception.unknown)
        self.assertEqual(self.calls[0]['timeout'],180)
        self.assertEqual(len(self.calls),1)

    def test_known_failures_rotate_using_actual_remaining_time_and_preserve_other_budgets(self):
        self.clock=Clock('2026-11-13T14:39:00+08:00')
        self.plan.pop('request_timeout_seconds')
        for value in self.chain:value['model']['timeout_seconds']=500
        with patch.object(rotation.sessions,'remaining_seconds',side_effect=[501,195,105]):
            self.execute({1:('known_fail',0),2:('known_fail',0),3:('ok',0)})
        self.assertEqual([row['timeout'] for row in self.calls],[486,180,90])
        self.assertEqual([row['model_id'] for row in self.calls],[1,2,3])
        self.assertTrue(all(row['configured']==500 for row in self.calls))
        self.assertIsNone(self.plan['budget']['max_requests'])


if __name__=='__main__':unittest.main()
