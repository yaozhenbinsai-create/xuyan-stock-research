from datetime import date, timedelta
import unittest

from research import discovery, rises


def describe(closes, volumes=None, *, high=None, partial=False, volume_refs=None):
    bars = []
    for i, close in enumerate(closes):
        previous = closes[i-1] if i else close
        bars.append({'day':(date(2026, 1, 1)+timedelta(days=i)).isoformat(),
                     'close':close,'high':max(close,previous)+.02,'low':min(close,previous)-.02,
                     'preclose':previous,'volume':volumes[i] if volumes else 100,
                     'volume_unit':'shares','amount':10000,'_id':i+1})
    if high is not None:
        bars[49]['high'] = high
    if partial:
        bars[-1]['partial_bar'] = True
    return discovery.describe_bars(bars, classify=True, volume_refs=volume_refs)


class RiseTests(unittest.TestCase):
    momentum = [10]*60+[10.10,10.20,10.30,10.38,10.60]
    contraction = [10]*59+[10.20,10.21,10.20,10.23,10.25,10.28]
    recovery = [10]*60+[9.80,9.85,10.05,10.12,10.16]

    def test_distinct_rules_and_overlap_keep_one_primary(self):
        strong = describe(self.momentum)['rise']
        setup = describe(self.contraction,[100]*59+[50]*6,high=10.4)['rise']
        early = describe(self.recovery)['rise']
        breakout = describe(self.momentum,[100]*64+[200])['rise']
        self.assertEqual([s['type'] for s in (strong,setup,early,breakout)],list(rises.TYPES))
        self.assertEqual(set(breakout['matches']),{'strong','breakout'})
        self.assertEqual(early['age'],2)
        self.assertEqual(setup['state'],'awaiting_trigger')
        self.assertEqual(setup['trigger_above'],10.4)
        self.assertLess(setup['invalidation_price'],setup['trigger_above'])
        self.assertNotIn('rise',describe(self.contraction,high=10.4))
        self.assertNotIn('breakout',describe(self.momentum,[100]*63+[None,200])['rise']['matches'])

    def test_partial_quotes_do_not_supply_complete_confirmation_or_daily_volume(self):
        series = [10]*62+[9.8,10.05,10.2]
        self.assertEqual(describe(series)['rise']['type'],'early')
        self.assertNotIn('rise',describe(series,partial=True))
        volume_refs=[(50,101),(60,102),(40,103)]
        observed=describe(self.momentum,partial=True)['rise']
        self.assertIn('strong',observed['matches'])
        self.assertNotIn('breakout',observed['matches'])
        signal=describe(self.momentum,partial=True,volume_refs=volume_refs)['rise']
        self.assertEqual((signal['type'],signal['state']),('breakout','intraday'))
        self.assertTrue({101,102,103}.issubset(signal['facts']))

    def test_round_robin_serves_each_available_type_before_a_second(self):
        samples = [describe(self.momentum),describe(self.contraction,[100]*59+[50]*6,high=10.4),
                   describe(self.recovery),describe(self.momentum,[100]*64+[200])]
        eligible=[(str(n),m) for n,m in enumerate(samples*2)]
        arranged=list(rises.balanced(reversed(eligible)))
        self.assertEqual([m['rise']['type'] for _,m in arranged],list(rises.TYPES)*2)
        self.assertEqual(len({symbol for symbol,_ in arranged}),len(eligible))
