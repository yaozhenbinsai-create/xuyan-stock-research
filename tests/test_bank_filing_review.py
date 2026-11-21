"""Explicit thousand-yuan bank filing fixture; no database or provider calls."""
import hashlib
import importlib.util
from pathlib import Path
import unittest


source=Path(__file__).resolve().parents[1]/'research/originals.py'
spec=importlib.util.spec_from_file_location('bank_review_originals',source)
reviewed=importlib.util.module_from_spec(spec);spec.loader.exec_module(reviewed)

HEADER=('本报告期\n（7-9 月）\n本报告期比上年同期\n增减变动幅度(%)\n'
        '年初至报告\n期末（1-9月）\n年初至报告期末比\n上年同期增减变动\n幅度(%)\n')


def filing(body,title='2026年第三季度报告'):
    return {'id':17,'title':title,'url':'https://static.cninfo.com.cn/isolated-bank.pdf',
            'text':'【PDF 第 2 页】\n'+body}


class BankFilingReviewTests(unittest.TestCase):
    def test_explicit_bank_thousand_yuan_rows_and_bare_header_percentages(self):
        fact=filing(HEADER+'营业收入（千元） 3,409,061 16.17 10,616,247 12.52\n'
                    '归属于母公司股东的净利\n润（千元） 1,149,803 -20.18 3,510,071 -10.34\n'
                    '经营活动产生的现金流量\n净额（千元） 不适用 不适用 33,466,738 116.18\n')
        rows=reviewed._financial_rows(fact)
        self.assertEqual({(r['metric'],r['basis']):r['amount_yuan'] for r in rows},
                         {('operating_revenue','quarterly'):'3409061000',
                          ('operating_revenue','cumulative'):'10616247000',
                          ('parent_profit','quarterly'):'1149803000',
                          ('parent_profit','cumulative'):'3510071000',
                          ('operating_cash','cumulative'):'33466738000'})
        self.assertFalse(any(r['metric'] in ('net_profit','main_revenue','MBRevenue') for r in rows))
        for row in rows:
            anchor=row['anchor'];quote=fact['text'][anchor['start']:anchor['end']]
            self.assertEqual(anchor['sha256'],hashlib.sha256(quote.encode()).hexdigest())
            self.assertEqual(anchor['location'],'PDF 第 2 页')

    def test_same_page_unit_directive_is_converted_without_header_inference(self):
        rows=reviewed._financial_rows(filing('单位：人民币千元\n营业收入 12,345\n'))
        self.assertEqual(rows[0]['amount_yuan'],'12345000')
        self.assertEqual(rows[0]['basis'],'undetermined')

    def test_existing_currency_scales_remain_exact(self):
        for unit,expected in [('元','12'),('千元','12000'),('万元','120000'),('亿元','1200000000')]:
            with self.subTest(unit=unit):
                rows=reviewed._financial_rows(filing('单位：'+unit+'\n营业收入 12\n','2026年第一季度报告'))
                self.assertEqual(rows[0]['amount_yuan'],expected)

    def test_unknown_unit_is_not_guessed(self):
        self.assertEqual(reviewed._financial_rows(filing(HEADER+'营业收入（百万元） 1 2% 3 4%\n')),[])

    def test_unit_support_does_not_assign_bare_percentages_without_a_complete_header(self):
        rows=reviewed._financial_rows(filing('营业收入（千元） 1 2 3 4\n'))
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['basis'],'undetermined')
        self.assertEqual(rows[0]['amount_yuan'],'1000')


if __name__=='__main__':unittest.main()
