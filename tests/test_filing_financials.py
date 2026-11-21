"""Financial import account, period, currency and provenance regressions."""
import hashlib
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from foundation import assets, database as store, record_store
from research import evidence, filing_financials, originals
from tools.mysql_instance import create_isolated_database, drop_isolated_database


FOUR = ('项目 本报告期 本报告期比上年同期增减变动幅度(%) '
        '年初至报告期末 年初至报告期末比上年同期增减变动幅度(%)\n')
SIX = ('项目 本报告期 上年同期 本报告期比上年同期增减变动幅度(%) '
       '年初至报告期末 上年同期 年初至报告期末比上年同期增减变动幅度(%)\n')
ADJUSTED = SIX + '调整前 调整后 调整后 调整前 调整后 调整后\n'


def filing(body, identifier=31, title='2026年第三季度报告'):
    return {'id': identifier, 'symbol': 'sh.600000', 'title': title,
            'published': '2026-10-31T10:00:00+08:00',
            'url': 'https://static.cninfo.com.cn/isolated-financial.pdf',
            'text': '【PDF 第 2 页】\n单位：万元 币种：人民币\n' + body}


def financial(record, fid, source='cninfo_financials'):
    return {**record, 'id': fid, 'source': source}


class FilingFinancialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='stock-filing-test-')
        self.environment = patch.dict(os.environ, {
            'STOCK_DATA_ROOT': self.temp.name,
            'STOCK_MYSQL_DATABASE': 'stock_test_filing_' + uuid.uuid4().hex[:10],
            'STOCK_DISABLE_WORKER': '1',
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.addCleanup(self.temp.cleanup)

    def test_explicit_summary_amounts_keep_cumulative_and_parent_account(self):
        source = filing(FOUR + '营业收入 100 5% 300 10%\n'
                        '归属于上市公司股东的净利润 10 5% 30 10%\n'
                        '经营活动产生的现金流量净额 不适用 不适用 40 10%\n')
        records = filing_financials.records_from_filing(source)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual((record['report_type'], record['report_date'], record['day']),
                         ('filing', '2026-09-30', '2026-10-31'))
        fields = record['fields']
        self.assertEqual((fields['operating_revenue'], fields['parent_profit'], fields['operating_cash']),
                         ('3000000', '300000', '400000'))
        self.assertNotIn('netProfit', fields)
        self.assertNotIn('MBRevenue', fields)
        self.assertTrue(all(value is None or type(value) in (str, int, float, bool) for value in fields.values()))
        for metric in ('operating_revenue', 'parent_profit', 'operating_cash'):
            quote = source['text'][fields[metric + '_start']:fields[metric + '_end']]
            self.assertEqual(hashlib.sha256(quote.encode()).hexdigest(), fields[metric + '_anchor_sha256'])
            self.assertEqual(fields[metric + '_location'], 'PDF 第 2 页')

    def test_explicit_comparatives_keep_same_period_and_adjusted_column(self):
        source = filing(ADJUSTED + '营业收入 100 60 80 25% 300 150 200 50%\n'
                        '归属于上市公司股东的净利润 20 10 15 33% 60 25 40 50%\n')
        records = filing_financials.records_from_filing(source)
        self.assertEqual([(r['report_date'], r['fields']['operating_revenue']) for r in records],
                         [('2025-09-30', '2000000'), ('2026-09-30', '3000000')])
        self.assertEqual(records[0]['published'], '2026-10-31')
        self.assertEqual(records[0]['fields']['parent_profit'], '400000')

    def test_missing_comparative_is_unknown_and_percentage_not_a_money_column(self):
        records = filing_financials.records_from_filing(filing(SIX + '营业收入 100 -- 25% 300 -- 50%\n'))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['report_date'], '2026-09-30')
        self.assertEqual(filing_financials.records_from_filing(filing(FOUR + '营业收入 10% 20% 30% 40%\n')), [])

    def test_bank_currency_heading_is_explicit_and_not_guessed(self):
        source = filing('（除另有标明外，人民币百万元）\n'
                        '项目 2026年7至9月 同比增减(%) 2026年1至9月 同比增减(%)\n'
                        '营业收入 100 5 300 10\n')
        source['text'] = source['text'].replace('单位：万元 币种：人民币\n', '')
        record = filing_financials.records_from_filing(source)[0]
        self.assertEqual(record['fields']['operating_revenue'], '300000000')
        source['text'] = source['text'].replace('人民币百万元', '外币百万元')
        self.assertEqual(filing_financials.records_from_filing(source), [])

    def test_named_bank_periods_unit_suffixes_and_parentheses_percentages(self):
        headers = [
            ('（单位：百万元 币种：人民币 未经审计）',
             '项目 2026年7-9月 2026年7-9月比上年同期增减（%） 2026年1-9月 2026年1-9月比上年同期增减（%）', '300000000'),
            ('单位：千元人民币',
             '项目 2026年7-9月 同比增减(%) 2026年1-9月 同比增减(%)', '300000'),
            ('（除特别注明外，以人民币百万元列示）',
             '截至2026年9月30日止三个月 比上年同期增减(%) 截至2026年9月30日止九个月 比上年同期增减(%)', '300000000'),
            ('单位：千元',
             '项目 2026年7-9月 比上年同期增减 2026年1-9月 比上年同期增减', '300000'),
        ]
        for unit, header, expected in headers:
            with self.subTest(unit=unit, header=header):
                source = filing(unit + '\n' + header + '\n营业收入 100 5% 300 (0.99%)\n')
                source['text'] = source['text'].replace('单位：万元 币种：人民币\n', '')
                records = filing_financials.records_from_filing(source)
                self.assertEqual(records[0]['fields']['operating_revenue'], expected)

    def test_explicit_foreign_currency_is_not_recorded_as_cny(self):
        source = filing(FOUR + '营业收入 100 5% 300 10%\n')
        source['text'] = source['text'].replace('币种：人民币', '币种：美元')
        self.assertEqual(filing_financials.records_from_filing(source), [])

    def test_explicit_ytd_statement_provides_comparative_without_yoy_inversion(self):
        source = filing('合并年初至报告期末利润表\n单位：元\n项目 本期发生额 上期发生额\n'
                        '营业收入 300 200\n归属于母公司股东的净利润 60 40\n')
        records = filing_financials.records_from_filing(source)
        self.assertEqual([(r['report_date'], r['fields']['operating_revenue']) for r in records],
                         [('2025-09-30', '200'), ('2026-09-30', '300')])
        source['text'] = source['text'].replace('合并年初至报告期末利润表', '母公司利润表')
        self.assertEqual(filing_financials.records_from_filing(source), [])

    def test_plain_q3_amount_without_proven_header_is_not_promoted(self):
        self.assertEqual(filing_financials.records_from_filing(filing('营业收入 300 10%\n')), [])
        source = filing(FOUR + '营业收入 100 5% 300 10% 999\n')
        self.assertEqual(filing_financials.records_from_filing(source), [])

    def test_filing_operating_uses_one_account_family_and_provenance_closure(self):
        source = filing(SIX + '营业收入 100 80 25% 300 200 50%\n'
                        '归属于上市公司股东的净利润 20 10 100% 60 40 50%\n'
                        '经营活动产生的现金流量净额 不适用 不适用 不适用 90 50 80%\n')
        rows = [financial(r, 100+i) for i, r in enumerate(filing_financials.records_from_filing(source))]
        result = evidence.operating_facts(rows)
        self.assertEqual(result['profit_basis'], 'parent_profit')
        self.assertEqual((result['revenue'], result['prior_revenue']), (3000000, 2000000))
        self.assertEqual((result['parent_profit'], result['prior_parent_profit']), (600000, 400000))
        self.assertNotIn('net_profit', result)
        self.assertTrue(result['improving'])
        self.assertEqual(set(result['fact_ids']), {31, 100, 101})
        assessment = {'triggers': {'O06': result['fact_ids']}, 'operating': result}
        compact = evidence.compact({'financials': rows}, assessment, {})
        self.assertIn(31, {r['id'] for r in compact})

    def test_newer_vendor_period_is_not_replaced_by_stale_original(self):
        old = filing('营业收入 300\n', title='2026年第一季度报告')
        rows = [financial(r, 11) for r in filing_financials.records_from_filing(old)]
        rows.append({'id': 12, 'source': 'bs_profit', 'report_type': 'profit', 'report_date': '2026-09-30',
                     'published': '2026-10-31', 'fields': {'netProfit': '120', 'MBRevenue': ''}})
        result = evidence.operating_facts(rows)
        self.assertEqual(result['report_date'], '2026-09-30')
        self.assertIsNone(result['revenue'])
        self.assertEqual(result['profit_basis'], 'net_profit')

    def test_different_account_does_not_supply_missing_parent_profit(self):
        record = filing_financials.records_from_filing(filing(FOUR + '营业收入 100 5% 300 10%\n'))[0]
        rows = [financial(record, 11), {'id': 12, 'source': 'bs_profit', 'report_type': 'profit',
                 'report_date': '2026-09-30', 'published': '2026-10-31',
                 'fields': {'netProfit': '1000000', 'MBRevenue': ''}}]
        result = evidence.operating_facts(rows)
        self.assertIsNone(result['parent_profit'])
        self.assertFalse(result['improving'])

    def test_underscore_title_is_parsed_without_changing_saved_title(self):
        source = filing(FOUR + '营业收入 100 5% 300 10%\n', title='600498_2025年_三季度报告')
        source['published'] = '2025-10-30T10:00:00+08:00'
        source['text'] = '【PDF 第 1 页】\n公司2025年第三季度报告\n' + source['text']
        records = filing_financials.records_from_filing(source)
        self.assertEqual(records[0]['report_date'], '2025-09-30')
        self.assertEqual(records[0]['fields']['filing_title'], '600498_2025年_三季度报告')
        self.assertEqual(source['title'], '600498_2025年_三季度报告')

    def test_missing_title_year_uses_explicit_unique_original_heading_with_anchor(self):
        source = filing(FOUR + '营业收入 100 5% 300 10%\n', title='公司第三季度报告')
        source['published'] = '2026-10-31T10:00:00+08:00'
        source['text'] = '【PDF 第 1 页】\n某公司2025年第三季度报告\n重要内容提示\n' + source['text']
        record = filing_financials.records_from_filing(source)[0]
        self.assertEqual(record['report_date'], '2025-09-30')
        self.assertEqual(record['fields']['filing_title'], '公司第三季度报告')
        fields = record['fields']
        quote = source['text'][fields['report_period_start']:fields['report_period_end']]
        self.assertIn('2025年第三季度报告', quote)
        self.assertEqual(hashlib.sha256(quote.encode()).hexdigest(), fields['report_period_anchor_sha256'])

    def test_missing_or_ambiguous_original_report_year_is_not_inferred_from_publication(self):
        source = filing(FOUR + '营业收入 100 5% 300 10%\n', title='公司第三季度报告')
        self.assertEqual(filing_financials.records_from_filing(source), [])
        source['text'] = '【PDF 第 1 页】\n2025年第三季度报告\n2026年第三季度报告\n' + source['text']
        self.assertEqual(filing_financials.records_from_filing(source), [])
        source['title'] = '2026年第三季度报告'
        source['text'] = source['text'].replace('2026年第三季度报告\n', '', 1)
        self.assertEqual(filing_financials.records_from_filing(source), [])

    def test_non_report_announcements_are_rejected_even_if_text_quotes_a_report_heading(self):
        titles = ['2025年第三季度业绩说明会', '关于召开2025年第三季度业绩说明会的公告',
                  '关于2025年第三季度报告业绩说明会的公告', '2025年第三季度经营数据简报',
                  '2025年第三季度主要经营数据的公告', '2025年第三季度计提资产减值准备的公告',
                  '2025年第三季度报告披露提示性公告', '关于2025年第三季度报告的更正公告']
        for title in titles:
            with self.subTest(title=title):
                source = filing(FOUR + '营业收入 100 5% 300 10%\n', title=title)
                source['text'] = '【PDF 第 1 页】\n某公司2025年第三季度报告\n' + source['text']
                self.assertEqual(filing_financials._parsing_fact(source), (None, None))
                self.assertEqual(filing_financials.records_from_filing(source), [])
                self.assertEqual(originals._financial_rows(source), [])

    def test_yearless_title_quarter_must_agree_with_original_heading(self):
        source = filing(FOUR + '营业收入 100 5% 300 10%\n', title='公司第一季度报告')
        source['text'] = '【PDF 第 1 页】\n某公司2025年第三季度报告\n' + source['text']
        self.assertEqual(filing_financials.records_from_filing(source), [])
        source['title'] = '公司三季度报告'
        records = filing_financials.records_from_filing(source)
        self.assertEqual(records[0]['report_date'], '2025-09-30')

    def test_annual_year_overlap_is_canonicalized_only_for_parsing(self):
        for title in ('公司2025年度报告', '公司2025年年度报告'):
            with self.subTest(title=title):
                source = filing('营业收入 300\n', title=title)
                records = filing_financials.records_from_filing(source)
                self.assertEqual(records[0]['report_date'], '2025-12-31')
                self.assertEqual(records[0]['fields']['filing_title'], title)
        source = filing('营业收入 300\n', title='公司年度报告')
        source['text'] = '【PDF 第 1 页】\n某公司2025年度报告\n' + source['text']
        records = filing_financials.records_from_filing(source)
        self.assertEqual(records[0]['report_date'], '2025-12-31')
        self.assertEqual(records[0]['fields']['report_period_basis'], 'original_heading')

    def test_line_wrapped_grouped_currency_amounts_keep_original_offsets(self):
        source = filing(ADJUSTED + '营业\n收入 1,093,768,584\n.05 330,368,127.\n37 '
                        '782,162,049\n.61 39.84 3,134,457,1\n81.70 '
                        '932,354,928\n.36 2,118,064,2\n23.68 47.99\n')
        source['text'] = source['text'].replace('单位：万元', '单位：元')
        records = filing_financials.records_from_filing(source)
        self.assertEqual([(r['report_date'], r['fields']['operating_revenue']) for r in records],
                         [('2025-09-30', '2118064223.68'), ('2026-09-30', '3134457181.70')])
        current = records[-1]['fields']
        quote = source['text'][current['operating_revenue_start']:current['operating_revenue_end']]
        self.assertTrue(quote.endswith('3,134,457,1\n81.70'))
        self.assertEqual(hashlib.sha256(quote.encode()).hexdigest(), current['operating_revenue_anchor_sha256'])
        cells = filing_financials._cells('20\n26')
        self.assertEqual([cell['number'] for cell in cells], ['20', '26'])

    def test_comma_boundary_linewraps_preserve_full_money_columns(self):
        source = filing(ADJUSTED + '营业收入 840,336\n,868.94 857,784\n,654.18 '
                        '858,123\n,954.18 -2.07 2,645,24\n9,887.98 '
                        '2,761,58\n5,175.38 2,762,03\n7,575.38 -4.23\n')
        source['text'] = source['text'].replace('单位：万元', '单位：元')
        records = filing_financials.records_from_filing(source)
        self.assertEqual([(r['report_date'], r['fields']['operating_revenue']) for r in records],
                         [('2025-09-30', '2762037575.38'), ('2026-09-30', '2645249887.98')])
        current = records[-1]['fields']
        quote = source['text'][current['operating_revenue_start']:current['operating_revenue_end']]
        self.assertTrue(quote.endswith('2,645,24\n9,887.98'))
        self.assertEqual(hashlib.sha256(quote.encode()).hexdigest(), current['operating_revenue_anchor_sha256'])
        cells = filing_financials._cells('1,200\n300 20\n26')
        self.assertEqual([cell['number'] for cell in cells], ['1,200', '300', '20', '26'])

    def test_total_operating_revenue_without_operating_revenue_is_not_substituted(self):
        source = filing(FOUR + '营业总收入 100 5% 300 10%\n')
        self.assertEqual(filing_financials.records_from_filing(source), [])


class FilingFinancialStorageTests(unittest.TestCase):
    def test_collector_roundtrip_dedup_and_source_fact_retention(self):
        schema = 'stock_test_filing_store_' + uuid.uuid4().hex[:10]
        with tempfile.TemporaryDirectory(prefix='stock-filing-store-') as isolated:
            with patch.dict(os.environ, {'STOCK_DATA_ROOT': isolated,
                                        'STOCK_MYSQL_DATABASE': schema, 'STOCK_DISABLE_WORKER': '1'}):
                create_isolated_database(schema)
                try:
                    source = filing(SIX + '营业收入 100 80 25% 300 200 50%\n'
                                    '归属于上市公司股东的净利润 20 10 100% 60 40 50%\n'
                                    '经营活动产生的现金流量净额 不适用 不适用 不适用 90 50 80%\n')
                    source.pop('id')
                    source.update(day='2026-10-31', key='isolated-financial-filing')
                    assets.publish('announcements', [source], 'cninfo_text')
                    params = {'start': '2026-10-01', 'end': '2026-11-20', 'symbols': ['sh.600000']}
                    records = filing_financials.collect(params)
                    self.assertEqual(len(records), 2)
                    result = assets.publish('financials', records, 'cninfo_financials')
                    duplicate = assets.publish('financials', filing_financials.collect(params), 'cninfo_financials')
                    self.assertEqual((result['added'], duplicate['added'], duplicate['unchanged']), (2, 0, 2))
                    with store.db(readonly=True) as conn:
                        ids = [r[0] for r in conn.execute("SELECT fact_id FROM heads WHERE dataset='financials'")]
                        hydrated = record_store.load_many(conn, ids)
                        self.assertEqual({r['fields']['operating_revenue'] for r in hydrated.values()}, {'2000000', '3000000'})
                        refs = conn.execute("SELECT COUNT(*) FROM evidence_refs WHERE owner_type='financial_filing'").fetchone()[0]
                        self.assertEqual(refs, 2)
                        documents, _ = evidence.collect(conn, '2026-11-20', store.now(), False, ['sh.600000'])
                    operating = evidence.operating_facts(documents['sh.600000']['financials'])
                    self.assertTrue(operating['improving'])
                    self.assertEqual(operating['profit_basis'], 'parent_profit')
                finally:
                    drop_isolated_database(schema)


if __name__ == '__main__':
    unittest.main()
