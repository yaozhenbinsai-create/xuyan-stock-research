"""Causal, bounded Wyckoff hypotheses from the frozen OHLCV sequence."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from math import isfinite
from statistics import median


VERSION = 'wyckoff-visible-v1'
WINDOW = 65
RANGE_BARS = 16
TREND_BARS = 10
MAX_AGE = 32
MIN_BREAK_VOLUME = 1.3


def positive(value):
    return type(value) in (int, float) and isfinite(value) and value > 0


def _price_bar(bar):
    opening, high, low, close = (bar.get(k) for k in ('open', 'high', 'low', 'close'))
    return (not bar.get('suspended') and all(map(positive, (opening, high, low, close)))
            and low <= min(opening, close) <= max(opening, close) <= high)


def _id(bar):
    return bar.get('_id', bar.get('id'))


def _at(bar):
    return bar.get('source_time') or bar['day'] + 'T15:00:00+08:00'


def _known(bar):
    return max(_at(bar), bar.get('_known_at') or bar.get('first_seen') or '')


def _location(bar):
    spread = bar['high'] - bar['low']
    return (bar['close'] - bar['low']) / spread if spread > 0 else .5


def _volume(bar):
    value = bar.get('volume')
    return value if positive(value) and bar.get('volume_unit') in (None, 'shares') else None


def _effort(history, bar, partial_ratio):
    peers = [volume for b in history[-20:] if (volume := _volume(b)) is not None]
    volume = _volume(bar)
    ratio = partial_ratio if bar.get('partial_bar') else volume / median(peers) if volume and len(peers) >= 10 else None
    previous = history[-1]['close'] if history else None
    spread_ref = median([b['high'] - b['low'] for b in history[-20:]]) if history else 0
    location = _location(bar)
    progress = (bar['close'] - previous) / previous if previous else None
    label = '量能比较不足' if ratio is None else '量价响应普通'
    if ratio is not None and ratio >= MIN_BREAK_VOLUME and progress is not None:
        if progress > .01 and location >= .65:
            label = '较高成交推动上涨，收盘靠近高位'
        elif progress < -.01 and location <= .35:
            label = '较高成交推动下跌，收盘靠近低位'
        elif abs(progress) < .008 or location < .45:
            label = '较高成交未带来相称的上行结果'
    return {'relative_volume': round(ratio, 4) if ratio is not None else None,
            'spread_ratio': round((bar['high'] - bar['low']) / spread_ref, 4) if spread_ref else None,
            'progress': round(progress, 6) if progress is not None else None,
            'close_location': round(location, 4), 'description': label}


@dataclass
class Range:
    low: float
    high: float
    began: str
    established: str
    prior: str
    evidence: tuple
    phase: str = 'B'
    age: int = 0
    direction: int = 0
    probe: dict | None = None
    confirmed: dict | None = None
    breakout: dict | None = None
    pullback: dict | None = None
    held: int = 0


def _range(history):
    if len(history) < RANGE_BARS + TREND_BARS:
        return None
    prior = history[-RANGE_BARS-TREND_BARS:-RANGE_BARS]
    sample = history[-RANGE_BARS:]
    move = prior[-1]['close'] / prior[0]['close'] - 1
    if abs(move) < .04:
        return None
    low_bar = min(sample, key=lambda b: b['low'])
    high_bar = max(sample, key=lambda b: b['high'])
    low, high = low_bar['low'], high_bar['high']
    if not .035 <= high / low - 1 <= .25:
        return None
    band = (high - low) * .24
    # Two visits to each side, separated in time, distinguish a range from a drifting trend.
    lower = [i for i, b in enumerate(sample) if b['low'] <= low + band]
    upper = [i for i, b in enumerate(sample) if b['high'] >= high - band]
    if not lower or not upper or lower[-1] - lower[0] < 3 or upper[-1] - upper[0] < 3:
        return None
    return Range(low, high, sample[0]['day'], sample[-1]['day'], 'down' if move < 0 else 'up',
                 (prior[0], prior[-1], low_bar, high_bar, sample[lower[-1]], sample[upper[-1]]))


def _stopping(history, effort):
    if len(history) < TREND_BARS or (effort['relative_volume'] or 0) < MIN_BREAK_VOLUME:
        return None
    move = history[-1]['close'] / history[-TREND_BARS]['close'] - 1
    if move <= -.04 and effort['close_location'] >= .65:
        return 'down'
    if move >= .04 and effort['close_location'] <= .35:
        return 'up'
    return None


LABELS = {'stopping': '原趋势减速线索', 'range': '区间成立', 'spring': 'Spring 下探收回',
          'ut': 'UT 上探回落', 'utad': 'UTAD 反复上探后回落', 'test': 'Test 低量复测',
          'supply_test': '上方供给复测', 'sos': 'SOS 需求突破', 'sow': 'SOW 供给破位',
          'lps': 'LPS 回踩承接', 'lpsy': 'LPSY 反抽受压', 'extension': '区间外延续',
          'invalidated': '结构失效', 'expired': '等待窗口结束', 'incomparable': '价量不可比'}


def _event(kind, bar, *, origin=None, sources=(), note='', partial=False, direction=0):
    rows = [*sources, origin or bar, bar]
    return {'kind': kind, 'label': LABELS[kind], 'plot_day': (origin or bar)['day'],
            'observed_at': _at(bar), 'known_at': max(map(_known, rows)),
            'fact_ids': list(dict.fromkeys(fid for b in rows if (fid := _id(b)) is not None)),
            'state': 'provisional' if partial else 'invalidated' if kind in ('invalidated', 'expired', 'incomparable') else 'confirmed',
            'direction': direction, 'note': note}


def _outside_setup(active, bar, effort):
    """Evaluate an established breakout on subsequent bars only."""
    up = active.direction == 1
    boundary = active.high if up else active.low
    close, width = bar['close'], active.high - active.low
    if (close < boundary if up else close > boundary):
        return 'invalidated', '突破后收盘重返原区间，原方向假设失效'
    active.held += 1
    tested = bar['low'] <= boundary + width * .15 if up else bar['high'] >= boundary - width * .15
    volume_ratio = effort['relative_volume']
    location = effort['close_location'] if up else 1 - effort['close_location']
    if not active.pullback and tested and volume_ratio is not None and volume_ratio <= .9 and location >= .55:
        active.pullback = bar
        return ('lps' if up else 'lpsy'), '低量回测后收在区间外'
    if active.phase != 'E' and active.held >= 3 and (active.pullback or active.held >= 5):
        active.phase = 'E'
        return 'extension', '后续可见收盘持续留在原区间外'
    return None, ''


def _inside_setup(active, bar, effort):
    low, high = active.low, active.high
    price, location, ratio = bar['close'], effort['close_location'], effort['relative_volume']
    if price > high and ratio is not None and ratio >= MIN_BREAK_VOLUME and location >= .65:
        active.phase, active.direction, active.breakout = 'D', 1, bar
        return 'sos', None, '收盘站上既定上沿且成交响应达到门槛'
    if price < low and ratio is not None and ratio >= MIN_BREAK_VOLUME and location <= .35:
        active.phase, active.direction, active.breakout = 'D', -1, bar
        return 'sow', None, '收盘跌破既定下沿且成交响应达到门槛'
    probe = active.probe
    if probe:
        direction, origin = probe['direction'], probe['bar']
        if (price <= origin['low'] if direction == 1 else price >= origin['high']):
            active.probe, active.confirmed, active.phase = None, None, 'B'
            return 'invalidated', origin, '收盘越过探测极值，原测试假设失效'
        if not active.confirmed and active.age - probe['age'] > 5:
            active.probe, active.phase = None, 'B'
            return 'expired', origin, '五根后续柱内没有合格复测'
        original_volume, volume = _volume(origin), _volume(bar)
        near_edge = bar['low'] <= low + (high - low) * .25 if direction == 1 else bar['high'] >= high - (high - low) * .25
        holds = bar['low'] >= origin['low'] and price >= low if direction == 1 else bar['high'] <= origin['high'] and price <= high
        if (not active.confirmed and original_volume and volume and volume <= original_volume * .8
                and near_edge and holds and (location >= .55 if direction == 1 else location <= .45)):
            active.confirmed = bar
            return ('test' if direction == 1 else 'supply_test'), origin, '后续柱成交缩减，未越过原探测极值'
    direction = 1 if bar['low'] < low < price and location >= .55 else -1 if bar['high'] > high > price and location <= .45 else 0
    if direction:
        repeat = bool(probe and probe['direction'] == direction)
        active.probe = {'direction': direction, 'bar': bar, 'age': active.age}
        active.confirmed, active.phase = None, 'C'
        return ('spring' if direction == 1 else 'utad' if repeat else 'ut'), None, '区间外极值回收，等待独立复测'
    return None, None, ''


def _summary(active, events, effort, *, eligible, incomplete, latest, known_at, stop, partial=False):
    bias = 'two_sided'
    if active and active.direction:
        bias = 'demand' if active.direction == 1 else 'supply'
    elif active and active.confirmed:
        bias = 'demand_test' if active.probe['direction'] == 1 else 'supply_test'
    opposite = {'demand': '若跌回原上沿，须检查假突破或区间仍在延续',
                'supply': '若收复原下沿，须检查假破位或需求重新承接',
                'demand_test': '低量复测后仍可能缺乏推进需求，等待突破',
                'supply_test': '供给测试后仍可能被需求吸收，等待下沿确认',
                'two_sided': '横盘可由换手或趋势暂停形成，保留需求与供给两种解释'}
    return {'version': VERSION, 'as_of': _at(latest), 'knowledge_at': known_at,
            'phase': active.phase if active else 'A' if stop else None, 'bias': bias,
            'state': 'incomparable' if incomplete else 'provisional' if partial or not eligible else 'observed',
            'gate_eligible': eligible and not incomplete and not partial,
            'range': {'low': active.low, 'high': active.high, 'began': active.began,
                      'established': active.established, 'prior_trend': active.prior} if active else None,
            'guard': {'below': active.high, 'fact_id': _id(active.breakout)} if active and active.direction == 1 else None,
            'effort': effort, 'counterpoint': opposite[bias], 'events': events}


def analyze(bars, *, actions_verified=False, partial_ratio=None, action_days=(), frames=False,
            actions_known_at=None, action_available=None, action_ids=()):
    """Reveal one observation per step. A partial last bar can propose, never confirm."""
    history, events, replay = [], [], []
    active, stopping = None, None
    complete_since_break = True
    action_days = set(action_days)
    knowledge_clock = ''
    action_available = action_available or {}
    summary = {'version': VERSION, 'state': 'insufficient', 'phase': None, 'events': [], 'gate_eligible': False}
    for bar in list(bars)[-WINDOW:]:
        knowledge_clock = max(knowledge_clock, _known(bar),action_available.get(bar['day'],''))
        step = []
        partial = bool(bar.get('partial_bar'))
        effort = None
        cross = history and (not positive(bar.get('preclose')) or abs(bar['preclose'] / history[-1]['close'] - 1) > .001)
        if not _price_bar(bar) or cross or bar['day'] in action_days:
            history, active, stopping = [], None, None
            complete_since_break = False
            step.append(_event('incomparable', bar, note='价格跳变、公司行动、停牌或行情字段不足，重新累计可比历史', partial=partial))
        if _price_bar(bar):
            effort = _effort(history, bar, partial_ratio)
            if stopping is not None and len(history) - stopping > TREND_BARS:
                stopping = None
            if not active and not partial and (new_range := _range([*history, bar])):
                active = new_range
                step.append(_event('range', bar, sources=active.evidence, note='已看到两侧反复交互并核对前趋势'))
            elif active and not partial:
                active.age += 1
                if active.age > MAX_AGE and active.phase != 'E':
                    step.append(_event('expired', bar, sources=active.evidence, note='区间假设已超出有效观察窗口'))
                    active = None
                elif active.phase == 'E' and active.held >= 9:
                    active = None
                else:
                    if active.breakout:
                        origin = active.breakout
                        direction = active.direction
                        kind, note = _outside_setup(active, bar, effort)
                    else:
                        direction = active.probe['direction'] if active.probe else 0
                        kind, origin, note = _inside_setup(active, bar, effort)
                        direction = active.direction or (active.probe['direction'] if active.probe else direction)
                    if kind:
                        step.append(_event(kind, bar, origin=origin, sources=active.evidence, note=note, direction=direction))
                    if kind == 'invalidated' and active.breakout:
                        active = None
            elif not partial and _stopping(history, effort):
                stopping = len(history)
                step.append(_event('stopping', bar, sources=(history[-TREND_BARS], history[-1]), note='成交与收盘位置出现反向响应'))
            elif partial and active:
                if active.breakout:
                    boundary = active.high if active.direction == 1 else active.low
                    if (bar['close'] < boundary if active.direction == 1 else bar['close'] > boundary):
                        step.append(_event('invalidated', bar, origin=active.breakout, sources=active.evidence,
                                           note='截点报价重返区间，须用完整柱确认', partial=True))
                elif bar['low'] < active.low < bar['close'] or bar['high'] > active.high > bar['close']:
                    kind = 'spring' if bar['low'] < active.low < bar['close'] else 'ut'
                    step.append(_event(kind, bar, sources=active.evidence, note='盘中候选，等待完整柱及后续测试', partial=True))
            if not partial:
                history.append(bar)
                if len(history) >= RANGE_BARS + TREND_BARS:
                    complete_since_break = True
        events.extend(step)
        for event in step:
            event['known_at'] = max(event['known_at'], knowledge_clock)
        checked = actions_verified and (not frames or not actions_known_at or actions_known_at <= knowledge_clock)
        summary = _summary(active, events[-8:], effort, eligible=checked,
                           incomplete=not complete_since_break or len(history) < TREND_BARS,
                           latest=bar, known_at=knowledge_clock, stop=stopping is not None, partial=partial)
        if frames:
            replay.append({**summary, 'events': step})
    summary['action_fact_ids'] = list(action_ids)
    if actions_verified and not frames:
        summary['knowledge_at'] = max(summary.get('knowledge_at', ''), actions_known_at or '')
    return {'summary': summary, 'frames': replay} if frames else summary


def decision_guard(value):
    if not value.get('gate_eligible'):
        return None
    if value['bias'] == 'supply':
        event = next((e for e in reversed(value['events']) if e['kind'] in ('sow', 'lpsy', 'extension') and e['direction'] == -1), None)
        return {'fact_ids': event['fact_ids'], 'reason': '可比价量已确认下沿破位，需先看到收复或重新论证'} if event else None
    event = next((e for e in reversed(value['events']) if e['kind'] == 'invalidated' and e['direction'] == 1 and e['observed_at'] == value['as_of']), None)
    return {'fact_ids': event['fact_ids'], 'reason': '可比价量已确认原多头结构失效，需重新建立参与条件'} if event else None


def point_figure(bars, study, box_value, reversal):
    """Close-only fixed-grid X/O columns. Count a contiguous row in the declared range."""
    box = Decimal(str(box_value))
    rows = [b for b in bars if not b.get('partial_bar')]
    if not study.get('gate_eligible') or not rows:
        return {'state': 'limited', 'reason': '需要核验过公司行动的完整可比日线'}
    boundary = study.get('range')
    if not boundary:
        return {'state': 'limited', 'reason': '没有同截点成立的结构区间，无法确定计数区域'}
    rows = [b for b in rows if b['day'] >= boundary['began']]
    if any(not _price_bar(b) for b in rows):
        return {'state': 'limited', 'reason': '计数区有停牌或缺少有效行情'}
    levels = [int((Decimal(str(b['close'])) / box).to_integral_value(rounding=ROUND_FLOOR)) for b in rows]
    if not levels or max(levels) - min(levels) > 150 or min(levels) < 1:
        return {'state': 'limited', 'reason': '格值与本股票价差不匹配，请使用覆盖不超过150格的格值'}
    columns = []
    frozen_count_columns = []
    initial = levels[0]
    for bar, level in zip(rows[1:], levels[1:]):
        if not columns:
            if level == initial:
                continue
            columns.append({'type': 'X' if level > initial else 'O', 'low': min(level, initial),
                            'high': max(level, initial), 'from': rows[0]['day'], 'to': bar['day']})
            if bar['day'] <= boundary['established']:
                frozen_count_columns = [dict(c) for c in columns]
            continue
        current = columns[-1]
        rising = current['type'] == 'X'
        extreme = current['high'] if rising else current['low']
        change = (level - extreme) * (1 if rising else -1)
        if change > 0:
            current['high' if rising else 'low'] = level
            current['to'] = bar['day']
        elif change <= -reversal:
            columns.append({'type': 'O' if rising else 'X', 'low': level if rising else extreme + 1,
                            'high': extreme - 1 if rising else level, 'from': bar['day'], 'to': bar['day']})
        if bar['day'] <= boundary['established']:
            frozen_count_columns = [dict(c) for c in columns]
    line = int((Decimal(str((boundary['high'] + boundary['low']) / 2)) / box).to_integral_value(rounding=ROUND_FLOOR))
    # Restrict the count to columns that began before the range was confirmed.
    longest = streak = 0
    for column in frozen_count_columns:
        streak = streak + 1 if column['low'] <= line <= column['high'] else 0
        longest = max(longest, streak)
    projection = None
    if longest >= 3 and study['bias'] in ('demand', 'supply'):
        span = float(box * longest * reversal)
        target = boundary['low'] + span if study['bias'] == 'demand' else boundary['high'] - span
        if target > 0:
            projection = round(target, 4)
    return {'state': 'exploratory', 'version': 'pf-close-fixed-v1', 'box': float(box), 'reversal': reversal,
            'range': boundary, 'count_line': float(box * line), 'count': longest, 'projection': projection,
            'columns': [{**c, 'low': float(box * c['low']), 'high': float(box * c['high'])} for c in columns],
            'reason': '固定格值的情景测算，只有计数充分且方向已观察确认时给出投影，不参与正式推荐或收益评价'}
