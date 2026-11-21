"""Data repair review regressions. All network and database calls are replaced."""
from contextlib import contextmanager
import copy
import json
import unittest
from unittest.mock import patch
import pymysql

from foundation import collection, database as store, sources
from research import preparation


class Result:
    rowcount=1
    def __init__(self,row=None):self.row=row
    def fetchone(self):return self.row


class MemoryDB:
    def __init__(self,row=None):self.row=row;self.executed=[]
    @contextmanager
    def db(self,**kwargs):yield self
    def execute(self,sql,params=()):
        self.executed.append((sql,params))
        return Result(self.row)


def src(operation='profit'):
    return {'id':'bs_'+operation,'operation':operation,'dataset':'financials','provider':'baostock',
            'enabled':True,'automatic':True,'min_interval':0}


def job(operation='profit'):
    return {'id':'original','source':'bs_'+operation,'trigger':'research_evidence','mode':'missing',
            'start':'2025-11-13','end':'2026-11-13','retries':0,'done':0,'total':2,'failed':0,
            'no_data':0,'items':[
                {'start':'2025-11-13','end':'2026-11-13','symbol':'sh.600000','symbols':['sh.600000'],
                 'year':2026,'quarter':3,'financial_fallback_depth':0},
                {'start':'2025-11-13','end':'2026-11-13','symbol':'sh.600000','symbols':['sh.600000'],
                 'year':2025,'quarter':3}]}


class DataReviewTests(unittest.TestCase):
    def test_worker_and_clock_survive_audit_failure_without_logging_error_bodies(self):
        for method,operation,delay in (('loop','claim',3),('clock_loop','tick',20)):
            with self.subTest(method=method):
                worker=collection.Engine()
                with patch.object(worker.stop_event,'is_set',side_effect=[False,True]),\
                     patch.object(worker.stop_event,'wait') as wait,\
                     patch.object(worker,operation,side_effect=pymysql.OperationalError(2013,'secret-upstream-key')) ,\
                     patch.object(store,'audit',side_effect=pymysql.OperationalError(2013,'secret-audit-key')) as audit,\
                     self.assertLogs(collection.log,level='ERROR') as captured:
                    getattr(worker,method)()
                wait.assert_called_once_with(delay)
                audit.assert_called_once_with('采集线程异常' if method=='loop' else '采集时钟异常',{'type':'OperationalError'})
                self.assertIn('OperationalError',' '.join(captured.output))
                self.assertNotIn('secret',' '.join(captured.output))

    def test_undisclosed_current_quarter_enqueues_older_quarter_and_yoy_in_original_job(self):
        worker=collection.Engine();j=job();memory=MemoryDB()
        with patch.object(store,'db',memory.db),patch.object(collection.assets,'source',return_value=src()),\
             patch.object(collection.assets,'patch_source'),patch.object(worker,'persist'),\
             patch.object(sources,'collect',side_effect=sources.SourceError('no disclosed report','empty')):
            worker.execute_unit(j)
        self.assertEqual(j['id'],'original');self.assertEqual(j['done'],1)
        self.assertEqual(j['no_data'],1);self.assertEqual(j['total'],4)
        self.assertEqual([(p['year'],p['quarter']) for p in j['items']],[(2026,3),(2026,2),(2025,2),(2025,3)])
        self.assertEqual(j['items'][1]['financial_fallback_depth'],1)
        self.assertNotIn('financial_fallback_depth',j['items'][2])

    def test_fallback_stops_after_four_current_quarters(self):
        worker=collection.Engine();j=job();j['items'][0]['financial_fallback_depth']=3;memory=MemoryDB()
        with patch.object(store,'db',memory.db),patch.object(collection.assets,'source',return_value=src()),\
             patch.object(collection.assets,'patch_source'),patch.object(worker,'persist'),\
             patch.object(sources,'collect',side_effect=sources.SourceError('no report','empty')):
            worker.execute_unit(j)
        self.assertEqual(j['total'],2);self.assertEqual(j['no_data'],1)

    def test_existing_disclosed_financial_period_is_reused_without_network(self):
        worker=collection.Engine();j=job();memory=MemoryDB((99,))
        with patch.object(store,'db',memory.db),patch.object(collection.assets,'source',return_value=src()),\
             patch.object(worker,'persist') as persist,patch.object(sources,'collect') as collect:
            worker.execute_unit(j)
        collect.assert_not_called();self.assertEqual(j['done'],1);self.assertEqual(j['reused'],1)
        self.assertEqual(persist.call_args.args[1]['fact_id'],99)
        self.assertIn('h.day<=%s',memory.executed[0][0])
        self.assertEqual(memory.executed[0][1],('sh.600000','bs_profit','2026-09-30','2026-11-13'))

    def test_expired_nonquote_evidence_keeps_original_progress_and_loses_priority(self):
        worker=collection.Engine();j=job();j.update(expires_at='2026-11-13T14:39:00+08:00',continue_after_expiry=True)
        original=copy.deepcopy(j['items'])
        with patch.object(store,'now',return_value='2026-11-13T14:40:00+08:00'),patch.object(worker,'persist'),\
             patch.object(sources,'collect') as collect:
            worker.execute_unit(j)
        collect.assert_not_called();self.assertEqual(j['items'],original);self.assertEqual(j['done'],0)
        self.assertEqual(j['status'],'queued');self.assertNotIn('expires_at',j)
        self.assertEqual(j['evidence_deadline_at'],'2026-11-13T14:39:00+08:00')

    def test_expired_quote_stays_partial(self):
        worker=collection.Engine();j=job();j.update(source='tx_quotes',expires_at='2026-11-13T11:30:00+08:00')
        with patch.object(store,'now',return_value='2026-11-13T11:31:00+08:00'),patch.object(worker,'persist'),\
             patch.object(sources,'collect') as collect:
            worker.execute_unit(j)
        collect.assert_not_called();self.assertEqual(j['status'],'partial');self.assertIn('expires_at',j)

    def test_partial_job_retains_unfinished_units_on_persist(self):
        worker=collection.Engine();j=job();j['status']='partial';memory=MemoryDB(('running',))
        with patch.object(store,'db',memory.db):worker.persist(j)
        update=next(params for sql,params in memory.executed if sql.startswith('UPDATE'))
        self.assertIn('items',json.loads(update[1]))

    def test_manual_resume_expired_evidence_uses_original_remaining_units(self):
        j=job();j.update(status='partial',done=1,expires_at='2026-11-13T11:30:00+08:00')
        memory=MemoryDB({'id':j['id'],'status':j['status'],'data':json.dumps(j),
                         'expires_at':collection.queue_state.local_datetime(j['expires_at'])})
        with patch.object(store,'db',memory.db),patch.object(store,'audit'),\
             patch.object(store,'now',return_value='2026-11-13T12:00:00+08:00'),\
             patch.object(collection.assets,'source',return_value=src()):
            result=collection.control(j['id'],'resume')
        self.assertEqual(result['done'],1);self.assertEqual(result['status'],'queued');self.assertNotIn('expires_at',result)
        saved=json.loads(next(params[1] for sql,params in memory.executed if sql.startswith('UPDATE')))
        self.assertEqual(saved['items'],j['items'])

    def test_network_retry_applies_to_targeted_cashflow(self):
        worker=collection.Engine();j=job('cashflow');j['retries']=3
        with patch.object(store,'db',MemoryDB().db),patch.object(collection.assets,'source',return_value=src('cashflow')),\
             patch.object(collection.assets,'local_now',return_value=__import__('datetime').datetime.fromisoformat('2026-11-13T09:00:00+08:00')),\
             patch.object(collection.assets,'patch_source'),patch.object(worker,'persist'),\
             patch.object(sources,'collect',side_effect=sources.SourceError('network','network')):
            worker.execute_unit(j)
        self.assertEqual(j['status'],'waiting');self.assertEqual(j['done'],0);self.assertEqual(j['deferred_retries'],1)

    def test_exhausted_deferred_retries_reach_failed_terminal(self):
        worker=collection.Engine();j=job('cashflow');j.update(retries=3,deferred_retries=3)
        with patch.object(store,'db',MemoryDB().db),patch.object(collection.assets,'source',return_value=src('cashflow')),\
             patch.object(collection.assets,'local_now',return_value=__import__('datetime').datetime.fromisoformat('2026-11-13T09:00:00+08:00')),\
             patch.object(collection.assets,'patch_source'),patch.object(worker,'persist'),\
             patch.object(sources,'collect',side_effect=sources.SourceError('network','network')):
            worker.execute_unit(j)
        self.assertEqual(j['status'],'failed');self.assertEqual(j['failed'],1);self.assertEqual(j['done'],0)

    def test_prewarm_stops_retrying_after_three_resumes_and_respects_pause(self):
        state={'retry_counts':{'original':3}}
        with patch.object(collection,'get_job',return_value={'status':'failed'}),patch.object(collection,'control') as control:
            self.assertFalse(preparation._warm_pending('original',state,'2026-11-13T11:10:00+08:00','2026-11-13T09:00:00+08:00'))
            control.assert_not_called()
        with patch.object(collection,'get_job',return_value={'status':'paused'}),patch.object(collection,'control') as control:
            self.assertFalse(preparation._warm_pending('original',{},'2026-11-13T11:10:00+08:00','2026-11-13T09:00:00+08:00'))
            control.assert_not_called()

    def test_prewarm_failure_waits_fifteen_minutes_then_resumes_same_job(self):
        state={};expiry='2026-11-13T11:10:00+08:00';now='2026-11-13T09:00:00+08:00'
        with patch.object(collection,'get_job',return_value={'status':'failed'}),\
             patch.object(collection,'control') as control,patch.object(preparation.time,'time',return_value=1000):
            self.assertTrue(preparation._warm_pending('original',state,expiry,now));control.assert_not_called()
        with patch.object(collection,'get_job',return_value={'status':'failed'}),\
             patch.object(collection,'control') as control,patch.object(preparation.time,'time',return_value=1900):
            self.assertTrue(preparation._warm_pending('original',state,expiry,now))
            control.assert_called_once_with('original','resume')
        self.assertEqual(state['retry_counts']['original'],1)

    def test_originals_start_before_new_index_and_late_ids_are_not_dropped(self):
        submitted=[]
        def submit(raw,**kwargs):submitted.append((raw,kwargs));return {'id':str(len(submitted))}
        completed={'status':'completed','done':1,'total':1,'received':1}
        with patch.object(store,'now',return_value='2026-11-13T14:36:00+08:00'),\
             patch.object(store,'setting',return_value=False),patch.object(preparation.sessions,'remaining_seconds',return_value=540),\
             patch.object(preparation,'_original_ids',side_effect=[[1],[1,2]]),\
             patch.object(collection,'submit',side_effect=submit),patch.object(collection,'get_job',return_value=completed):
            preparation.acquire(['sh.600000'],'2026-11-13','tail','2026-11-13T14:45:00+08:00')
        self.assertEqual(submitted[0][0]['source'],'cninfo_text')
        self.assertEqual(submitted[0][1]['records'],[1]);self.assertEqual(submitted[-1][1]['records'],[2])
        self.assertEqual(submitted[0][1]['expires_at'],'2026-11-13T14:39:00+08:00')
        self.assertTrue(all(kwargs['continue_after_expiry'] for _,kwargs in submitted))
        action=next(raw for raw,_ in submitted if raw['source']=='bs_actions')
        self.assertEqual(action['start'],'2025-11-13')


if __name__=='__main__':unittest.main()
