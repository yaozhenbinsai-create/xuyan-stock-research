"""Bounded PDF excerpt selection with fake pages, no downloads or models."""
import unittest
from unittest.mock import patch

from foundation import pdf_worker


class Page:
    def __init__(self,text,stream_size=0):self.text=text;self.stream_size=stream_size;self.extractions=0
    def get_contents(self):
        if not self.stream_size:return None
        class Stream:
            def get_data(inner):return b'x'*self.stream_size
        return Stream()
    def extract_text(self):self.extractions+=1;return self.text


class Reader:
    def __init__(self,pages):self.pages=pages


class PdfFilingReviewTests(unittest.TestCase):
    def test_wrapped_account_on_second_page_is_not_lost(self):
        page=Page('第2页\n经营活动产生的现金流\n量净额（元） -- -- 201,781,318.96 22.48%\n')
        result=pdf_worker.extract_filing(Reader([Page('封面'),page]))
        self.assertIn('【PDF 第 2 页】',result['text'])
        self.assertIn('201,781,318.96',result['text'])

    def test_same_page_unit_and_table_header_more_than_560_chars_before_cash_are_kept(self):
        heading='合并年初到报告期末现金流量表\n单位：元\n项目 本期发生额 上期发生额\n'
        text=heading+'其他经营项目 1 2\n'*100+'经营活动产生的现金流量净额 201,781,318.96 164,749,079.35\n'
        result=pdf_worker.extract_filing(Reader([Page(text)]))
        self.assertIn(heading,result['text']);self.assertIn('201,781,318.96',result['text'])

    def test_long_page_keeps_nearby_table_context(self):
        heading='合并现金流量表\n单位：元\n项目 本期发生额 上期发生额\n'
        text=heading+'其他经营项目 1 2\n'*150+'经营活动产生的现金流量净额 201 164\n'+'其他说明\n'*200
        result=pdf_worker.extract_filing(Reader([Page(text)]))
        self.assertIn('单位：元',result['text']);self.assertIn('项目 本期发生额 上期发生额',result['text'])
        self.assertIn('现金流量净额 201 164',result['text'])

    def test_later_financial_summary_precedes_generic_business_for_budget_selection(self):
        business=[Page(('主营业务为设备生产销售。\n'+'业务说明。\n'*200)) for _ in range(20)]
        summary=Page('主要会计数据\n单位：元\n本报告期 年初至报告期末\n营业收入 100 300\n经营活动产生的现金流\n量净额 20 60\n')
        result=pdf_worker.extract_filing(Reader(business+[summary]))
        self.assertIn('【PDF 第 21 页】',result['text']);self.assertIn('营业收入 100 300',result['text'])
        self.assertLessEqual(len(result['text']),11000)

    def test_eighty_page_boundary_is_unchanged(self):
        pages=[Page('无相关内容') for _ in range(80)]+[Page('主要会计数据\n营业收入 999\n')]
        result=pdf_worker.extract_filing(Reader(pages))
        self.assertEqual(pages[-1].extractions,0);self.assertNotIn('999',result['text'])
        self.assertEqual(result['total_pages'],81);self.assertIn('检查前80页',result['text_scope'])

    def test_twenty_five_second_boundary_is_unchanged(self):
        pages=[Page('主要会计数据\n单位：元\n营业收入 100\n'),Page('主要会计数据\n营业收入 999\n')]
        with patch.object(pdf_worker.time,'monotonic',side_effect=[0,1,26]):
            result=pdf_worker.extract_filing(Reader(pages))
        self.assertEqual(pages[-1].extractions,0);self.assertNotIn('999',result['text'])
        self.assertIn('检查前1页',result['text_scope'])

    def test_oversized_page_stream_is_not_extracted(self):
        large=Page('营业收入 999',8_000_001)
        result=pdf_worker.extract_filing(Reader([large,Page('以下为隔离财务资料，不涉及实际股票推荐。\n单位：元\n营业收入 100\n')]))
        self.assertEqual(large.extractions,0);self.assertNotIn('999',result['text'])
        self.assertIn('营业收入 100',result['text'])

    def test_budget_cut_does_not_publish_truncated_numeric_line(self):
        pages=[Page('主要会计数据\n单位：元\n'+'营业收入 123 456\n'*110) for _ in range(10)]
        result=pdf_worker.extract_filing(Reader(pages))
        self.assertLessEqual(len(result['text']),11000)
        for line in result['text'].splitlines():
            if line.startswith('营业收入'):self.assertEqual(line,'营业收入 123 456')


if __name__=='__main__':unittest.main()
