"""Regression checks for review meaning and immutable research association.

These tests use in-memory results and never connect to any project database.
"""
from contextlib import contextmanager
import copy
import unittest
from unittest.mock import patch

from research import recaps


DAY = '2026-11-13'
MARKET = {'day':DAY,'sample':5216,'up':744,'down':4415,'flat':57,
          'move':-.020547,'amount':1655516018850.92,'breadth':744/5216,'markets':['sh','sz']}


class _Result:
    def __init__(self,row):
        self.row=row

    def fetchone(self):
        return self.row


class _ResearchQuery:
    def __init__(self,attempt,note=None):
        self.attempt,self.note=attempt,note

    def execute(self,sql,params=()):
        if 'report IS NOT NULL' in sql:
            return _Result(self.note)
        if 'FROM research_runs' in sql:
            return _Result(self.attempt)
        raise AssertionError('Unexpected database query in isolated recap check')


class RecapMeaningTests(unittest.TestCase):
    def test_relative_resilience_does_not_turn_widespread_declines_into_mixed(self):
        row={'sample':3189,'up':542,'breadth':542/3189,'move':-.0171}
        result=recaps._describe_group(row,-.0205)
        self.assertEqual(result['direction'],'weak')
        self.assertEqual(result['relative_direction'],'stronger')
        self.assertFalse(result['sample_limited'])
        self.assertNotIn('分化',result['observation'])

    def test_small_group_keeps_observed_direction_and_separate_sample_limit(self):
        result=recaps._describe_group({'sample':6,'up':0,'breadth':0,'move':-.0373},-.0205)
        self.assertEqual(result['direction'],'weak')
        self.assertTrue(result['sample_limited'])
        self.assertIn('样本较少',result['observation'])

    def test_monthly_improvement_addresses_actual_failures_before_sample_accumulation(self):
        text=recaps._improvement('close',{'tasks':10,'failures':6,'targeted':2,'data_limited':0},[])
        self.assertIn('10 次提交',text)
        self.assertIn('6 次运行问题',text)
        self.assertIn('原请求',text)
        self.assertNotIn('达到 10 个',text)

    def test_width_scenario_retains_rule_and_discloses_its_limit(self):
        history=[{**MARKET,'move':.01,'breadth':.65},
                 {**MARKET,'move':-.01,'breadth':.16},MARKET]
        result=recaps._market_tomorrow(MARKET,history,'2026-11-16')
        self.assertEqual(result['state'],'weak')
        self.assertEqual(result['samples'],3)
        self.assertIn('不代表经验证',result['limitation'])
        self.assertEqual(recaps._scope_label(MARKET),'沪深已存股票样本')

    def _daily(self,attempt,archive=None,note=None):
        @contextmanager
        def db(**kwargs):
            yield _ResearchQuery(attempt,note)
        with patch.object(recaps.store,'db',db), \
             patch.object(recaps.store,'now',return_value='2026-11-14T15:00:00+08:00'), \
             patch.object(recaps.store,'setting',return_value=copy.deepcopy(archive)), \
             patch.object(recaps.pool_market,'latest_close_day',return_value=DAY), \
             patch.object(recaps,'_daily_series',return_value=[copy.deepcopy(MARKET)]), \
             patch.object(recaps,'_groups',return_value=[]), \
             patch.object(recaps.sessions,'next_trading_day',return_value='2026-11-16'):
            return recaps.daily(DAY)

    def test_failed_attempt_is_returned_even_when_no_report_was_published(self):
        attempt={'id':'failed-close','status':'failed','stage':'模型未完成，未签发报告','published_at':None}
        result=self._daily(attempt)
        self.assertEqual(result['research_attempt'],attempt)
        self.assertIsNone(result['research_note'])

    def test_archive_keeps_original_research_identity_and_labels_later_context(self):
        old={'id':'original-close','status':'failed','published_at':None}
        current={'id':'later-close','status':'completed','published_at':'2026-11-14T10:00:00+08:00'}
        archive={'summary':{'market':copy.deepcopy(MARKET),'boards':[],'industries':[],
                            'research_context':{'attempt':old,'note':None}},
                 'receipt':{'at':'2026-11-13T19:00:00+08:00'}}
        result=self._daily(current,archive)
        self.assertEqual(result['research_attempt']['id'],'original-close')
        self.assertTrue(result['research_context_frozen'])
        self.assertEqual(result['current_research_context']['attempt']['id'],'later-close')
        self.assertEqual(archive['summary']['research_context']['attempt']['id'],'original-close')

    def test_old_archive_does_not_claim_current_research_was_frozen(self):
        attempt={'id':'current-close','status':'failed','published_at':None}
        archive={'summary':{'market':copy.deepcopy(MARKET),'boards':[],'industries':[]},
                 'receipt':{'at':'2026-11-13T19:00:00+08:00'}}
        result=self._daily(attempt,archive)
        self.assertFalse(result['research_context_frozen'])
        self.assertIn('当前补充',result['research_context_note'])


if __name__=='__main__':
    unittest.main()
