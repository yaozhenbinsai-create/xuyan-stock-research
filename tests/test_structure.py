from datetime import date, timedelta
import unittest

from research import structure


def bars_for_range(bearish=False):
    prices = [12.4 - i * .15 for i in range(10)] + [10.15, 10.46, 10.89, 10.77] * 4
    rows = [(p, p - .1, p + .1, 100) for p in prices]
    rows += [(10.26, 9.70, 10.34, 160), (10.32, 10.06, 10.34, 72),
             (11.35, 10.70, 11.41, 200), (11.31, 11.00, 11.35, 75),
             (11.41, 11.20, 11.45, 95), (11.47, 11.27, 11.52, 110),
             (10.90, 10.72, 11.43, 140)]
    bars = []
    for i, (close, low, high, volume) in enumerate(rows):
        day = (date(2026, 8, 1) + timedelta(days=i)).isoformat()
        if bearish:
            close, low, high = 22 - close, 22 - high, 22 - low
        bars.append({'day': day, 'open': close, 'high': high, 'low': low, 'close': close,
                     'preclose': bars[-1]['close'] if bars else close, 'volume': volume,
                     'volume_unit': 'shares', 'id': i + 1})
    return bars


class StructureTests(unittest.TestCase):
    def test_confirmation_keeps_plot_date_and_uses_only_visible_prefix(self):
        bars = bars_for_range()
        bars[27]['first_seen'] = '2026-10-01T18:00:00+08:00'
        replay = structure.analyze(bars, actions_verified=True, frames=True)['frames']
        kinds = [e['kind'] for f in replay for e in f['events']]
        self.assertEqual(kinds[-8:], ['range', 'spring', 'test', 'sos', 'lps', 'extension', 'invalidated'][-8:])
        probe = replay[26]['events'][0]
        test = replay[27]['events'][0]
        self.assertEqual(probe['kind'], 'spring')
        self.assertEqual(test['plot_day'], bars[26]['day'])
        self.assertEqual(test['observed_at'][:10], bars[27]['day'])
        self.assertEqual(test['known_at'], bars[27]['first_seen'])
        for index in range(len(bars)):
            prefix = structure.analyze(bars[:index + 1], actions_verified=True, frames=True)
            self.assertEqual(replay[index], prefix['frames'][-1])
        self.assertIsNotNone(structure.decision_guard(structure.analyze(bars, actions_verified=True)))

    def test_supply_confirmation_and_intraday_provisional_invalidation(self):
        bars = bars_for_range(bearish=True)
        study = structure.analyze(bars[:29], actions_verified=True)
        self.assertEqual(study['bias'], 'supply')
        self.assertIsNotNone(structure.decision_guard(study))
        partial = [*bars[:-1], {**bars[-1], 'partial_bar': True}]
        last = structure.analyze(partial, actions_verified=True)
        self.assertEqual(last['events'][-1]['state'], 'provisional')
        self.assertIsNone(structure.decision_guard(last))
        self.assertFalse(structure.analyze(bars[:29])['gate_eligible'])

    def test_company_action_resets_range_and_projection_has_frozen_count(self):
        bars = bars_for_range()
        early = structure.analyze(bars[:26], actions_verified=True)
        later = structure.analyze(bars[:-1], actions_verified=True)
        early_pf = structure.point_figure(bars[:26], early, .1, 3)
        later_pf = structure.point_figure(bars[:-1], later, .1, 3)
        self.assertIsNone(early_pf['projection'])
        self.assertEqual(early_pf['count'], later_pf['count'])
        adjusted = structure.analyze(bars, actions_verified=True, action_days=[bars[28]['day']])
        self.assertEqual(adjusted['state'], 'incomparable')
        self.assertFalse(adjusted['gate_eligible'])


if __name__ == '__main__':
    unittest.main()
