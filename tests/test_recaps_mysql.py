"""Recap grouping and endpoints against an isolated strict-mode MySQL schema."""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from authenticated_client import admin_client
from foundation import assets, database as store
from foundation.main import app
from research import recaps
from tools.mysql_instance import create_isolated_database, drop_isolated_database


DAY = '2026-11-13'
NOW = DAY + 'T18:00:00+08:00'


class RecapMysqlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = 'stock_test_recap_' + uuid.uuid4().hex[:10]
        cls.temp = tempfile.TemporaryDirectory(prefix='stock-recap-')
        cls.env = patch.dict(os.environ, {'STOCK_DATA_ROOT':cls.temp.name,
            'STOCK_MYSQL_DATABASE':cls.schema, 'STOCK_DISABLE_WORKER':'1'})
        cls.env.start()
        try:
            create_isolated_database(cls.schema)
            with patch.object(store, 'now', return_value=NOW):
                basics, bars, industries = [], [], []
                for index in range(14):
                    code = f'sh.{600001+index}'
                    bucket = 0 if index < 6 else 1 if index < 12 else 2
                    board = ('主板', '科创板', '样本不足板')[bucket]
                    close = (11, 9, 10)[bucket]
                    basics.append({'day':DAY, 'symbol':code, 'name':f'隔离公司{index}',
                                   'status':'1', 'board':board})
                    bars.append({'day':DAY, 'symbol':code, 'open':10, 'high':11,
                        'low':9, 'close':close, 'preclose':10, 'amount':1000,
                        'volume':100, 'suspended':False})
                    industries.append({'day':DAY, 'symbol':code,
                        'group':('金融', '科技', '未分类')[bucket],
                        'source_effective_date':DAY, 'unclassified':bucket == 2})
                assets.publish('basics', basics, 'bs_universe')
                assets.publish('daily', bars, 'bs_daily_all')
                assets.publish('industry', industries, 'bs_industry')
                assets.publish('calendar', [
                    {'day':DAY, 'symbol':'*', 'trading':True},
                    {'day':'2026-11-16', 'symbol':'*', 'trading':True}], 'bs_calendar')
        except BaseException:
            try:
                drop_isolated_database(cls.schema)
            finally:
                cls.env.stop()
                cls.temp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            drop_isolated_database(cls.schema)
        finally:
            cls.env.stop()
            cls.temp.cleanup()

    def test_companies_are_grouped_by_board_under_only_full_group_by(self):
        with store.db(readonly=True) as conn:
            self.assertIn('ONLY_FULL_GROUP_BY', conn.execute('SELECT @@sql_mode').fetchone()[0])
            rows = recaps._groups(conn, DAY, 0, 'basics')
        groups = {row['name']:row for row in rows}
        self.assertEqual(set(groups), {'主板', '科创板'})
        self.assertEqual((groups['主板']['sample'], groups['主板']['up']), (6, 6))
        self.assertEqual((groups['科创板']['sample'], groups['科创板']['down']), (6, 6))
        self.assertAlmostEqual(groups['主板']['move'], .1)
        self.assertAlmostEqual(groups['科创板']['move'], -.1)

    def test_industry_grouping_retains_valid_classification_scope(self):
        with store.db(readonly=True) as conn:
            rows = recaps._groups(conn, DAY, 0, 'industry')
        self.assertEqual({row['name']:row['sample'] for row in rows}, {'金融':6, '科技':6})

    def test_daily_monthly_and_empty_day_through_public_endpoints(self):
        with patch.object(store, 'now', return_value=NOW), admin_client(app) as client:
            daily = client.get('/api/v2/recap/daily', params={'day':DAY})
            self.assertEqual(daily.status_code, 200, daily.text)
            self.assertEqual(daily.json()['market']['sample'], 14)
            self.assertEqual(len(daily.json()['boards']), 2)
            monthly = client.get('/api/v2/recap/monthly', params={'month':'2026-11'})
            self.assertEqual(monthly.status_code, 200, monthly.text)
            self.assertEqual([row['day'] for row in monthly.json()['market_days']], [DAY])
            empty = client.get('/api/v2/recap/daily', params={'day':'2026-11-12'})
            self.assertEqual(empty.status_code, 200, empty.text)
            self.assertIsNone(empty.json()['market'])
            self.assertEqual(empty.json()['boards'], [])
            self.assertEqual(empty.json()['industries'], [])


if __name__ == '__main__':
    unittest.main()