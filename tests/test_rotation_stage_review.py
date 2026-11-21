"""Exercise real rotation/save with a strict VARCHAR(40) in-memory database."""
from contextlib import contextmanager
import json
import unittest
from unittest.mock import patch

from foundation import database as store
from research import model_transport, rotation


class Result:
    def __init__(self,row=None,rows=()):self.row=row;self.rows=rows
    def fetchone(self):return self.row
    def __iter__(self):return iter(self.rows)


class StrictStageDB:
    def __init__(self):
        self.data={};self.status='running';self.calls={};self.saved_stages=[]
    @contextmanager
    def db(self,**kwargs):yield self
    def execute(self,sql,params=()):
        if sql.startswith('SELECT data FROM research_runs'):
            return Result(store.Row(['data'],[store.dumps(self.data)]))
        if sql.startswith('SELECT status,data FROM research_runs'):
            return Result(store.Row(['status','data'],[self.status,store.dumps(self.data)]))
        if sql.startswith('SELECT id,data FROM models'):
            return Result(rows=[{'id':i,'data':'{"enabled":true}'} for i in (1,2)])
        if sql.startswith('UPDATE research_runs SET data='):
            data,status,stage,updated,run_id=params
            if len(stage)>40:raise ValueError('1406 Data too long for column stage')
            self.data=json.loads(data);self.status=status;self.saved_stages.append(stage)
        elif sql.startswith('SELECT data FROM research_calls'):
            return Result(store.Row(['data'],[self.calls[params[0]]['data']]))
        elif sql.startswith("UPDATE research_calls SET status='invalid'"):
            self.calls[params[1]].update(status='invalid',data=params[0])
        else:raise AssertionError('Unexpected SQL: '+sql)
        return Result()


class RotationStageReviewTests(unittest.TestCase):
    def setUp(self):
        # These tests exercise stage length and rotation state. Real relational
        # projection is covered by the isolated repository integration tests.
        for name in ('sync_run','sync_call'):
            projection=patch.object(rotation.state_store,name)
            projection.start();self.addCleanup(projection.stop)

    def test_long_primary_invalid_report_rotates_to_long_backup_without_db_error(self):
        database=StrictStageDB();attempts=[]
        primary='Primary-'+('甲'*112);backup='Backup-'+('乙'*113)
        self.assertEqual(len(primary),120);self.assertEqual(len(backup),120)
        chain=[{'model':{'id':i,'name':name,'timeout_seconds':180,'max_output_tokens':None},'config_hash':'frozen'}
               for i,name in ((1,primary),(2,backup))]
        plan={'chain':chain,'model':chain[0]['model'],'budget':{'max_requests':None}}
        def complete(run_id,selected,system,payload):
            mid=selected['model']['id'];attempts.append(mid);call_id='call'+str(mid)
            database.calls[call_id]={'status':'completed','data':'{}'}
            return store.dumps({'valid':mid==2}),call_id
        def validate(text):
            value=json.loads(text)
            if not value['valid']:raise ValueError('引用政策或财报原件时须标记已读取页段')
            return value
        with patch.object(store,'db',database.db),patch.object(rotation.preferences,'context',return_value={}),\
             patch.object(rotation.sessions,'remaining_seconds',return_value=None),\
             patch.object(model_transport,'complete',side_effect=complete):
            report,call_id=rotation.execute('run',plan,'system',{},validate)
        self.assertEqual(report,{'valid':True});self.assertEqual(call_id,'call2')
        self.assertEqual(attempts,[1,2]);self.assertEqual(database.calls['call1']['status'],'invalid')
        self.assertIn('须标记已读取页段',json.loads(database.calls['call1']['data'])['validation_error'])
        self.assertTrue(all(len(stage)<=40 for stage in database.saved_stages))
        self.assertTrue(any(stage.endswith('…') for stage in database.saved_stages))
        self.assertEqual(database.data['rotation']['current_model_name'],backup)
        self.assertEqual(database.data['rotation']['successful_call_id'],'call2')
        self.assertEqual(database.data['rotation']['attempt'],2)

    def test_save_caps_every_dynamic_stage_and_preserves_complete_state(self):
        database=StrictStageDB();state={'current_model_name':'长'*120,'attempt':2}
        with patch.object(store,'db',database.db):
            for status in ('running','waiting','unknown'):
                rotation.save('run',state,'任意动态标签'+('文'*120),status=status)
                self.assertEqual(len(database.saved_stages[-1]),40)
                self.assertEqual(database.data['rotation']['current_model_name'],'长'*120)
                self.assertEqual(database.status,status)

    def test_short_and_exactly_forty_character_stages_remain_unchanged(self):
        database=StrictStageDB()
        with patch.object(store,'db',database.db):
            for stage in ('模型完成，证据契约通过','界'*40):
                rotation.save('run',{},stage)
                self.assertEqual(database.saved_stages[-1],stage)


if __name__=='__main__':unittest.main()
