"""Stock-specific downside hypotheses from a frozen price setup."""
from __future__ import annotations


def reference(candidate):
    metrics = candidate['metrics']
    signal = metrics.get('rise') or {}
    kind = signal.get('type')
    trigger = signal.get('trigger_above')
    floor = signal.get('invalidation_price')
    facts = signal.get('facts') or candidate.get('bar_ids',[])[-1:]
    if not kind and metrics.get('comparable') and 'O03' in candidate.get('paths',[]):
        kind = 'breakout'
        trigger = floor = metrics.get('prior_high20')
    if not floor or not facts:
        return {'gap':'本轮没有能定位的突破位或趋势支撑，反向推演还缺可引用的价量依据。'}
    if kind in ('setup','breakout') and trigger:
        return {
            'lure':f'区间上沿 {trigger:g} 元最容易吸引追单。' +
                   ('首次冲过时，追价者可能把盘中越线看成有效突破。' if kind=='setup'
                    else '已有突破后，追价者容易把再次走高看成突破延续。'),
            'text':'如果有较多筹码待卖，突破预期聚集的买盘可以暂时承接卖单。一旦买盘退去，追价的人会暴露在回到原区间的风险中。',
            'watch_for':f'观察价格冲高后是否收回 {trigger:g} 元下方，尤其是放量却推不动价格。收盘低于 {floor:g} 元，原形态失效。',
            'counterexample':f'完整日线站稳 {trigger:g} 元之上，后续回踩也能守住上沿，说明需求仍在承接。',
            'fact_ids':facts,
        }
    if kind in ('strong','early'):
        early = kind=='early'
        move = metrics.get('return_5')
        return {
            'lure':'启动后刚站住，追价者容易把短线修复当成持续上涨。' if early
                   else (f"近五日上涨 {move:.1%}。" if move is not None else '')+'连涨和强势形态容易吸引动量追单。',
            'text':'如果卖出筹码主要由最后一段追涨买盘承接，后续需求稍弱就容易冲高回落。追价者离原支撑越远，回落时越被动。',
            'watch_for':f'留意成交放大但价格涨不动，以及完整日线是否跌破冻结支撑 {floor:g} 元。',
            'counterexample':f'回踩守住 {floor:g} 元，之后放量上行并在日内高位收盘，承接转弱的假设减轻。',
            'fact_ids':facts,
        }
    return {'gap':'现有形态资料还不足以写出具体的反向条件。'}
