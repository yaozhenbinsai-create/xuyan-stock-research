"""Compacting genuine but incomplete finance must preserve comparison citations."""
import unittest
from research import evidence

class CompactOperatingReferencesTests(unittest.TestCase):
    def documents(self):
        return {'financials':[
            {'id':4,'day':'2026-10-31','report_type':'cashflow'},
            {'id':3,'day':'2026-10-31','report_type':'profit'},
            {'id':2,'day':'2025-10-31','report_type':'cashflow'},
            {'id':1,'day':'2025-10-31','report_type':'profit'},
        ]}

    def test_incomplete_operating_comparison_keeps_current_profit_prior_profit_current_cash(self):
        assessment={'triggers':{},'operating':{'fact_ids':[3,1,4],'improving':False,'revenue':None}}
        result=evidence.compact(self.documents(),assessment,{})
        self.assertEqual({r['id'] for r in result},{1,3,4})
        self.assertEqual(len(result),3)

    def test_real_operating_facts_with_missing_revenue_still_have_reference_closure(self):
        rows=[
            {'id':4,'source':'bs_cashflow','published':'2026-10-31','day':'2026-10-31','report_type':'cashflow','report_date':'2026-09-30','fields':{'CFOToOR':'0.2'}},
            {'id':3,'source':'bs_profit','published':'2026-10-31','day':'2026-10-31','report_type':'profit','report_date':'2026-09-30','fields':{'netProfit':'120','MBRevenue':''}},
            {'id':2,'source':'bs_cashflow','published':'2025-10-31','day':'2025-10-31','report_type':'cashflow','report_date':'2025-09-30','fields':{'CFOToOR':'0.1'}},
            {'id':1,'source':'bs_profit','published':'2025-10-31','day':'2025-10-31','report_type':'profit','report_date':'2025-09-30','fields':{'netProfit':'80','MBRevenue':''}},
        ]
        assessment=evidence.assess({'financials':rows},{},None)
        self.assertFalse(assessment['operating']['improving'])
        self.assertIsNone(assessment['operating']['revenue'])
        result=evidence.compact({'financials':rows},assessment,{})
        self.assertLessEqual(len(result),3)
        self.assertTrue(set(assessment['operating']['fact_ids'])<={r['id'] for r in result})

    def test_more_than_three_declared_references_are_not_truncated(self):
        rows=[{'id':i,'day':'2026-10-31'} for i in range(1,7)]
        assessment={'triggers':{'O01':[1,2],'O08':[3,4]},'operating':None}
        result=evidence.compact({'announcements':rows},assessment,{})
        self.assertEqual([r['id'] for r in result],[1,2,3,4])

    def test_references_from_checks_are_kept_with_trigger_references(self):
        rows=[{'id':i,'day':'2026-10-31'} for i in range(1,7)]
        result=evidence.compact({'announcements':rows},{'triggers':{'O01':[5]}},{'risk':{'fact_ids':[1,2,3,4]}})
        self.assertEqual({r['id'] for r in result},{1,2,3,4,5})

    def test_legacy_assessment_without_operating_retains_existing_bounded_selection(self):
        result=evidence.compact(self.documents(),{'triggers':{}},{})
        self.assertEqual([r['id'] for r in result],[4,3,2])

if __name__=='__main__':unittest.main()
