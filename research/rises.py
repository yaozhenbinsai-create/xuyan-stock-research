"""Versioned, point-in-time upward setups for screening and review."""
from __future__ import annotations

from collections import defaultdict
from itertools import zip_longest
from statistics import fmean


VERSION = 'rise-types-v1'
TYPES = {
    'strong': '强势上涨',
    'setup': '即将上涨',
    'early': '刚开始上涨（持续性）',
    'breakout': '突破上涨',
}
PRIORITY = ('breakout', 'early', 'strong', 'setup')
STRONG_MOVE = .04
STRONG_TODAY = .015
SETUP_NEAR = .03
SETUP_RANGE = .07
SETUP_DRY = .90
EARLY_MAX_MOVE = .10

# Risk guards: a breakout or strong run that is already too extended from the trend
# line, or moving too fast, has high mean-reversion risk the next session.
MAX_EXTENSION_ABOVE_MA20 = .12        # close more than 12% above MA20 = extended
MAX_5DAY_RETURN = .10                 # 5-day return above 10% = blow-off velocity
VOLUME_CLIMAX = 3.0                    # volume > 3x average = potential distribution, not confirmation
MIN_BREAKOUT_MARGIN = .005            # close must be at least 0.5% above the 20-day high
MIN_STRONG_CLOSE_LOCATION = .60       # strong needs to close in upper 60% of the day's range
MIN_STRONG_VOLUME_RATIO = 1.0         # strong needs at least average volume (participation)


def _extension(metrics):
    """How far above the 20-day MA the close sits, as a ratio. None if unknown."""
    ma20 = metrics.get('ma20')
    if ma20 and ma20 > 0 and metrics.get('close'):
        return metrics['close'] / ma20 - 1
    return None


def _risk_flags(metrics, kind):
    """Surface why a candidate is risky so the AI and UI can show it."""
    flags = []
    ext = _extension(metrics)
    if ext is not None and ext > MAX_EXTENSION_ABOVE_MA20:
        flags.append('extended_above_ma20')
    if kind == 'strong' and (metrics.get('return_5') or 0) > MAX_5DAY_RETURN:
        flags.append('blowoff_velocity')
    rv = metrics.get('relative_volume20')
    if rv is not None and rv > VOLUME_CLIMAX:
        flags.append('volume_climax')
    return flags


def catalog(method=None):
    method = method or {}
    descriptions = {
        'strong': ('近五日涨幅至少 4%，至少三日上涨，当日涨幅至少 1.5%，价格高于五日线，五日线高于二十日线，截点价格位于日内区间的 55% 以上。', '收盘跌回冻结的五日线下方。'),
        'setup': ('距前二十日上沿不超过 3%，前五日振幅不超过 7%，均量低于前二十日均量的 90%。价格和五日线守住二十日线，当日跌幅不超过 1.5%。', '收盘跌破收窄区间下沿，或参与窗口内始终未越过触发价。'),
        'early': ('近期重新站上二十日趋势线，随后 1 至 3 根完整日线守住启动支撑，最后一根完整日线收盘高于启动日收盘，近五日涨幅在 0% 至 10% 之间。', '收盘跌破启动时的支撑价。'),
        'breakout': (f"越过前二十日上沿，相对量至少 {method.get('relative_volume_min', 1.3):g} 倍，"
                     f"截点价格位于日内区间的上部（至少 {method.get('close_location_min', .65):.0%}）。收盘比较完整日线，盘中至少有三份同刻量样本。", '收盘跌回原区间上沿下方。'),
    }
    return {'version': VERSION, 'priority': list(PRIORITY),
            'categories': [{'id':key,'name':name,'rule':descriptions[key][0],
                            'invalidation':descriptions[key][1]} for key,name in TYPES.items()]}


def _ids(*bars):
    return list(dict.fromkeys(bar['_id'] for bar in bars if '_id' in bar))


def _complete_volumes(bars):
    return all(b.get('volume_unit') in (None,'shares') and (b.get('volume') or 0)>0 for b in bars)


def _early(bars, metrics):
    complete = bars[:-1] if metrics['partial_bar'] else bars
    move = metrics['return_5']
    if not (0 < move <= EARLY_MAX_MOVE and metrics['close'] > metrics['ma20']):
        return None
    for i in range(len(complete)-2, len(complete)-5, -1):
        base = fmean(b['close'] for b in complete[i-20:i])
        previous = fmean(b['close'] for b in complete[i-21:i-1])
        origin = complete[i]
        confirmation = complete[i+1:]
        if complete[i-1]['close'] <= previous and origin['close'] > base:
            if all(b['close'] > base for b in confirmation) and confirmation[-1]['close'] > origin['close'] and metrics['close'] > base:
                return {'facts':_ids(complete[i-1],origin,confirmation[0],confirmation[-1],bars[-1]),
                        'invalidation_price':round(base,4), 'age':len(confirmation),
                        'reason':f"{origin['day']} 重新站上二十日趋势线，后续 {len(confirmation)} 根完整日线守住启动支撑。"}
    return None


def _setup(bars, metrics, high_bar):
    high = high_bar['high']
    distance = (high-metrics['close'])/high
    if not (0 <= distance <= SETUP_NEAR and metrics['ma5'] >= metrics['ma20']
            and metrics['close'] >= metrics['ma20'] and metrics['return_1'] >= -.015):
        return None
    prior = bars[-21:-1]
    sample = prior[-5:]
    low_bar = min(sample,key=lambda b:b['low'])
    width = max(b['high'] for b in sample)/low_bar['low']-1
    if width > SETUP_RANGE or not _complete_volumes(prior):
        return None
    contraction = fmean(b['volume'] for b in sample)/fmean(b['volume'] for b in prior)
    if contraction >= SETUP_DRY:
        return None
    return {'facts':_ids(high_bar,low_bar,sample[0],sample[-1],bars[-1]),
            'trigger_above':high,'invalidation_price':low_bar['low'],'distance':round(distance,6),
            'reason':f"距区间上沿 {distance:.1%}，前五日振幅 {width:.1%}，完整日线均量降至前二十日的 {contraction:.0%}。"}


def classify(bars, metrics):
    if not metrics['comparable'] or metrics.get('return_1') is None:
        return None
    last = bars[-1]
    high_bar = max(bars[-21:-1],key=lambda b:b['high'])
    matches = {}
    up = sum(a['close']<b['close'] for a,b in zip(bars[-6:-1],bars[-5:]))
    if (metrics['return_5']>=STRONG_MOVE and metrics['return_1']>=STRONG_TODAY and up>=3
            and metrics['close']>metrics['ma5']>metrics['ma20']
            and (metrics.get('close_location') or 0)>=MIN_STRONG_CLOSE_LOCATION
            and (metrics.get('relative_volume20') or 0)>=MIN_STRONG_VOLUME_RATIO):
        strong_flags = _risk_flags(metrics,'strong')
        matches['strong'] = {'facts':_ids(bars[-6],last),'invalidation_price':round(metrics['ma5'],4),
                             'risk_flags':strong_flags,
                             'reason':f"近五日涨幅 {metrics['return_5']:.1%}，有 {up} 日上涨，截点涨幅 {metrics['return_1']:.1%}，价格在五日线和二十日线之上。"}
    if (setup := _setup(bars,metrics,high_bar)) is not None:
        matches['setup'] = setup
    if (early := _early(bars,metrics)) is not None:
        matches['early'] = early
    if 'O03' in metrics['paths'] and (metrics['partial_bar'] or _complete_volumes(bars[-21:])):
        breakout_margin = metrics['close'] / high_bar['high'] - 1
        if breakout_margin >= MIN_BREAKOUT_MARGIN:
            breakout_flags = _risk_flags(metrics,'breakout')
            matches['breakout'] = {'facts':_ids(high_bar,last),'trigger_above':high_bar['high'],
                                   'invalidation_price':high_bar['high'],
                                   'distance':round(breakout_margin,6),
                                   'risk_flags':breakout_flags,
                                   'reason':f"截点价格 {metrics['close']:g} 越过前二十日上沿 {high_bar['high']:g}，相对量 {metrics['relative_volume20']:.2f} 倍。"}
    if not matches:
        return None
    primary = next(kind for kind in PRIORITY if kind in matches)
    state = 'awaiting_trigger' if primary=='setup' else 'intraday' if metrics['partial_bar'] else 'observed'
    result = {'type':primary,'name':TYPES[primary],'matches':list(matches),'state':state,**matches[primary]}
    # Surface aggregate risk flags so the AI prompt, the decision panel, and the UI
    # can all explain why a candidate is risky instead of presenting it as clean.
    all_flags = sorted({f for m in matches.values() for f in (m.get('risk_flags') or [])})
    if all_flags:
        result['risk_flags'] = all_flags
    return result


def balanced(available):
    groups = defaultdict(list)
    for symbol,metrics in available:
        if metrics.get('rise'):
            groups[metrics['rise']['type']].append((symbol,metrics))
    scores = {
        'strong': lambda m: -m['return_5'],
        'setup': lambda m: m['rise']['distance'],
        'early': lambda m: m['rise']['age'],
        'breakout': lambda m: m['rise']['distance'],
    }
    ordered = []
    for kind in TYPES:
        ordered.append(sorted(groups[kind],key=lambda x:(scores[kind](x[1]),-(x[1]['amount'] or 0),x[0])))
    for round_ in zip_longest(*ordered):
        for item in round_:
            if item is not None:
                yield item
