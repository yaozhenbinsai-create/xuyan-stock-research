from datetime import date, timedelta
import unittest

from research import contracts, outlook


def series():
    bars = []
    close = 10
    for index in range(61):
        day = (date(2026,1,1)+timedelta(days=index)).isoformat()
        previous = close
        if index:
            move = (.01,.02,.03,-.01,-.02,-.035)[(index-1)%6]
            close *= 1+move
        bars.append({'day':day,'preclose':previous,'close':close})
    return bars,[bar['day'] for bar in bars]


class OutlookTests(unittest.TestCase):
    def test_independent_spans_exclude_gaps_and_known_company_actions(self):
        bars, days = series()
        state={'day':days[-1],'close':10}
        full=outlook.reference(bars,days,state)
        self.assertEqual(full['horizons']['D1']['samples'],60)
        self.assertEqual(full['horizons']['D3']['samples'],20)
        self.assertEqual(full['horizons']['D3']['rising'],10)
        self.assertLess(full['horizons']['D3']['downside']['return_high'],0)
        partial=outlook.reference(bars[:2]+bars[3:],days,state,[days[10]])
        self.assertEqual(partial['horizons']['D1']['samples'],57)
        self.assertEqual(partial['horizons']['D3']['samples'],18)

    def test_intraday_uses_quote_as_anchor_but_keeps_complete_history(self):
        bars, days = series()
        quote_day=(date.fromisoformat(days[-1])+timedelta(days=1)).isoformat()
        future=(date.fromisoformat(quote_day)+timedelta(days=1)).isoformat()
        stamp=quote_day+'T11:29:00+08:00'
        current={'day':quote_day,'close':20,'partial_bar':True,'quote_time':stamp}
        result=outlook.reference(bars+[current,{'day':future,'close':100}],days+[quote_day,future],current)
        self.assertEqual(result['history_end'],days[-1])
        self.assertEqual(result['horizons']['D1']['samples'],60)
        self.assertEqual(result['as_of'],stamp)
        self.assertGreater(result['horizons']['D1']['upside']['price_low'],20)

    def test_contract_rejects_unsampled_range_and_accepts_a_documented_gap(self):
        reference=outlook.reference([],[],{'day':'2026-11-06','close':10})
        case={'horizon':'D1','use_history':True}
        with self.assertRaisesRegex(ValueError,'历史幅度样本不足'):
            contracts._verify_outlook({'outlook':{'downside':case,'gap':'历史样本不足'}},reference)
        contracts._verify_outlook({'outlook':{'gap':'历史样本不足'}},reference)
