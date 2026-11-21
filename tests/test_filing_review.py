"""Strict column and provenance checks for already-extracted filing text."""
import hashlib
import unittest
from unittest.mock import patch

from research import multidim, originals


HEADER='项目 本报告期\n本报告期比\n上年同期增\n减变动幅度\n(%)\n年初至报告期末\n年初至报告期\n末比上年同期\n增减变动幅度\n(%)\n'
CASH_HEADER='合并年初到报告期末现金流量表\n单位：元\n项目 本期发生额 上期发生额\n'


def fact(body,identifier=31,title='2026年第三季度报告'):
    return {'id':identifier,'title':title,'url':'https://static.cninfo.com.cn/isolated.pdf',
            'text':'【PDF 第 1 页】\n单位：元 币种：人民币\n'+body}


class FilingReviewTests(unittest.TestCase):
    def test_cross_line_labels_and_explicit_q3_columns_keep_quarter_and_ytd_separate(self):
        f=fact(HEADER+'营业收入 8,067,375,836.96 35.48% 20,443,734,259.84 19.75%\n'
               '归属于上市公司股东的\n净利润 121,928,476.48 -46.08% 314,728,169.26 -38.61%\n'
               '经营活动产生的现金流\n量净额 不适用 不适用 5,153,501,779.87 9829.05%\n')
        rows=originals._financial_rows(f)
        actual={(r['metric'],r['basis']):r['amount_yuan'] for r in rows}
        self.assertEqual(actual,{('operating_revenue','quarterly'):'8067375836.96',
                                 ('operating_revenue','cumulative'):'20443734259.84',
                                 ('parent_profit','quarterly'):'121928476.48',
                                 ('parent_profit','cumulative'):'314728169.26',
                                 ('operating_cash','cumulative'):'5153501779.87'})
        self.assertFalse(any(r['metric'] in ('net_profit','MBRevenue','main_revenue') for r in rows))
        for row in rows:
            a=row['anchor'];quoted=f['text'][a['start']:a['end']]
            self.assertEqual(a['sha256'],hashlib.sha256(quoted.encode()).hexdigest())
            self.assertIn('项目 本报告期',quoted);self.assertEqual(a['location'],'PDF 第 1 页')

    def test_no_header_keeps_q3_amount_basis_undetermined(self):
        rows=originals._financial_rows(fact('营业收入 100 20% 300 10%\n'))
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['basis'],'undetermined')
        self.assertEqual(rows[0]['amount_yuan'],'100')

    def test_narrative_after_valid_header_never_receives_quarter_or_ytd_basis(self):
        rows=originals._financial_rows(fact(HEADER+'本公司营业收入 100 20% 300 10%\n'))
        self.assertTrue(all(r['basis']=='undetermined' for r in rows))

    def test_blank_first_column_and_explicit_month_ranges_are_supported(self):
        header=HEADER.replace('项目 ','').replace('本报告期\n','本报告期（7-9月）\n',1).replace('年初至报告期末\n','年初至报告期末（1-9月）\n',1)
        rows=originals._financial_rows(fact(header+'营业收入 100 20% 300 10%\n'))
        self.assertEqual([r['basis'] for r in rows],['quarterly','cumulative'])

    def test_header_on_missing_previous_page_is_not_inherited(self):
        f=fact(HEADER+'【PDF 第 3 页】\n单位：元\n营业收入 100 20% 300 10%\n')
        rows=originals._financial_rows(f)
        self.assertEqual([r['basis'] for r in rows],['undetermined'])

    def test_percent_cannot_occupy_amount_column(self):
        rows=originals._financial_rows(fact(HEADER+'营业收入 20% 30% 40% 50%\n'))
        self.assertEqual(rows,[])

    def test_fifth_numeric_cell_does_not_get_guessed_into_four_column_table(self):
        rows=originals._financial_rows(fact(HEADER+'营业收入 100 20% 300 10% 999\n'))
        self.assertTrue(all(r['basis']=='undetermined' for r in rows))

    def test_missing_cells_are_not_zero_and_negative_cumulative_cash_is_supported(self):
        rows=originals._financial_rows(fact(HEADER+'经营活动产生的现金流\n量净额 不适用 不适用 (52,970.25) -10%\n'))
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['basis'],'cumulative')
        self.assertEqual(rows[0]['amount_yuan'],'-52970.25')

    def test_cash_or_profit_explanation_percentage_is_not_money(self):
        rows=originals._financial_rows(fact(HEADER+'归属于上市公司股东的净利润_本\n报告期 -46.08%\n'))
        self.assertEqual(rows,[])

    def test_explicit_same_page_ytd_cash_table_handles_unit_beyond_general_window(self):
        f=fact(CASH_HEADER+'其他经营收支项目\n'*100+'经营活动产生的现金流量净额 201,781,318.96 164,749,079.35\n')
        rows=originals._financial_rows(f)
        self.assertEqual([(r['metric'],r['basis'],r['amount_yuan']) for r in rows],
                         [('operating_cash','cumulative','201781318.96')])
        a=rows[0]['anchor'];quote=f['text'][a['start']:a['end']]
        self.assertIn(CASH_HEADER,quote);self.assertTrue(quote.endswith('201,781,318.96'))
        self.assertEqual(a['sha256'],hashlib.sha256(quote.encode()).hexdigest())

    def test_two_column_cash_requires_exact_ytd_title_header_currency_and_row(self):
        for header,row in [(CASH_HEADER.replace('合并年初到报告期末现金流量表','合并现金流量表'),'经营活动产生的现金流量净额 201 164'),
                           (CASH_HEADER.replace('项目 本期发生额 上期发生额',''),'经营活动产生的现金流量净额 201 164'),
                           (CASH_HEADER,'本公司经营活动产生的现金流量净额 201 164'),
                           (CASH_HEADER+'【PDF 第 2 页】\n单位：元\n','经营活动产生的现金流量净额 201 164'),
                           (CASH_HEADER+'母公司现金流量表\n单位：元\n项目 本期发生额 上期发生额\n','经营活动产生的现金流量净额 201 164'),
                           (CASH_HEADER,'经营活动产生的现金流量净额 201 164 999')]:
            with self.subTest(header=header,row=row):
                rows=originals._financial_rows(fact(header+row+'\n'))
                self.assertFalse(any(r['basis']=='cumulative' for r in rows))

    def test_ytd_cash_branch_never_extracts_comparator_or_fills_missing_current(self):
        f=fact(CASH_HEADER+'经营活动产生的现金流量净额 -- 164\n')
        self.assertEqual(originals._financial_rows(f),[])

    def test_currency_is_required_and_row_currency_is_explicitly_accepted(self):
        f=fact(HEADER+'营业收入（万元） 100 20% 300 10%\n')
        f['text']=f['text'].replace('单位：元 币种：人民币\n','')
        rows=originals._financial_rows(f)
        self.assertEqual([r['amount_yuan'] for r in rows],['1000000','3000000'])
        f['text']=f['text'].replace('（万元）','')
        self.assertEqual(originals._financial_rows(f),[])

    def test_existing_q1_single_column_extraction_keeps_anchor_and_yuan_conversion(self):
        f=fact('营业收入 1300\n',title='2026年第一季度报告')
        f['text']=f['text'].replace('单位：元','单位：万元')
        rows=originals._financial_rows(f)
        self.assertEqual(rows[0]['amount_yuan'],'13000000');self.assertEqual(rows[0]['basis'],'cumulative')
        anchor=rows[0]['anchor'];self.assertEqual(f['text'][anchor['start']:anchor['end']],'营业收入 1300')

    def test_packet_keeps_fourteen_field_limit_and_one_anchor_per_field(self):
        body=HEADER+'营业收入 100 20% 300 10%\n归属于上市公司股东的净利润 10 20% 30 10%\n经营活动产生的现金流量净额 5 20% 15 10%\n'
        filings=[fact(body,i) for i in range(1,5)]
        with patch.object(originals,'_original_filings',return_value=filings):
            packet=originals.packet(None,'sh.600000','2026-11-13','2026-11-13',[])
        self.assertEqual(len(packet['financial_fields']),14)
        self.assertEqual(len(packet['anchors']),14)
        self.assertTrue(all(r['anchor_id'] in {a['id'] for a in packet['anchors']} for r in packet['financial_fields']))

    def test_multidim_original_money_labels_show_account_and_period_basis(self):
        rows=[{'metric':'operating_revenue','label':'营业收入','period':'2026-09-30','basis':'quarterly','amount_yuan':'100','anchor_id':'a'},
              {'metric':'parent_profit','label':'归属于上市公司股东的净利润','period':'2026-09-30','basis':'cumulative','amount_yuan':'20','anchor_id':'b'},
              {'metric':'operating_cash','label':'经营活动产生的现金流量净额','period':'2026-09-30','basis':'undetermined','amount_yuan':'30','anchor_id':'c'}]
        candidate={'metrics':{},'documents':[],'evidence_ids':[31],
                   'originals':{'financial_fields':rows,'anchors':[{'id':x,'fact_id':31} for x in ('a','b','c')]}}
        result=multidim.build({},candidate)
        f07=next(r for r in result['items'] if r['id']=='F07')
        labels=[r['label'] for r in f07['readings']]
        self.assertTrue(any('营业收入（本季）' in s for s in labels))
        self.assertTrue(any('归属于上市公司股东的净利润（累计）' in s for s in labels))
        self.assertTrue(any('经营活动产生的现金流量净额（口径待核）' in s for s in labels))

    def test_multidim_five_fields_show_three_accounts_and_prefer_ytd_at_same_period(self):
        body=HEADER+'营业收入 100 20% 300 10%\n归属于上市公司股东的净利润 10 20% 30 10%\n经营活动产生的现金流量净额 不适用 不适用 15 10%\n'
        with patch.object(originals,'_original_filings',return_value=[fact(body)]):
            packet=originals.packet(None,'sh.600000','2026-11-13','2026-11-13',[])
        self.assertEqual(len(packet['financial_fields']),5)
        candidate={'metrics':{},'documents':[],'evidence_ids':[31],'originals':packet}
        f07=next(r for r in multidim.build({},candidate)['items'] if r['id']=='F07')
        original_readings=[r for r in f07['readings'] if r['label'].endswith('原文金额')]
        self.assertEqual(len(original_readings),3)
        self.assertEqual([r['value'] for r in original_readings],[300,30,15])
        self.assertTrue(all('累计' in r['label'] for r in original_readings))


if __name__=='__main__':unittest.main()
