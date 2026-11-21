"""Translate explicit intent into a visible, bounded research plan."""
from __future__ import annotations

import re

from fastapi import HTTPException
from foundation import assets
from research import preferences, rises, sessions


DEFAULT_INTENT = '寻找未来1至3个交易日值得重点研究的股票，比较量价、行业与历史活性，并明确反证、缺口和后续去留。'
TOOLS = {
    'fact_detail': '展开冻结事实的价格、公告或财务字段',
    'original_passage': '核对冻结原文的具体页段及金额',
    'structure_review': '复算结构、供需和首次确认时点',
}
CODE = re.compile(r'(?<![A-Za-z0-9.])(?:(?:sh|sz|bj)[.]?)?[0-9]{6}(?![A-Za-z0-9.])', re.I)


def plan(raw, profile):
    intent = raw.get('intent') or DEFAULT_INTENT
    if not isinstance(intent, str) or not 1 <= len(intent.strip()) <= 2000:
        raise HTTPException(422, '研究意图须为1至2000字')
    intent = intent.strip()
    explicit = raw.get('symbols') or []
    if not isinstance(explicit,list) or len(explicit)>8:
        raise HTTPException(422,'自主研究每次最多8只股票')
    symbols = list(dict.fromkeys(assets.symbol(value) for value in explicit))
    codes = list(dict.fromkeys(CODE.findall(intent)))
    if len(codes) > 8:
        raise HTTPException(422, '意图中识别到超过8个证券代码，请在范围栏明确本次比较对象')
    if not symbols and raw.get('kind') != 'review':
        for value in codes:
            try:
                symbol = assets.symbol(value)
            except HTTPException:
                if not value.isdigit() or re.search(r'(?:股票|代码)\s*'+value,intent):
                    raise
                continue
            if symbol not in symbols:
                symbols.append(symbol)
    sessions_in_text = {key for key, spec in sessions.SESSIONS.items() if spec['name'] in intent}
    dates = set(re.findall(r'(?<!\d)20\d{2}-\d{2}-\d{2}(?!\d)', intent))
    if not raw.get('session') and len(sessions_in_text)>1:
        raise HTTPException(422, '意图提到多个时段，请明确本轮使用午盘、尾盘或收盘')
    if not raw.get('market_day') and len(dates)>1:
        raise HTTPException(422, '意图提到多个日期，请明确本轮市场日')
    session = raw.get('session') or next(iter(sessions_in_text), 'close')
    if not isinstance(session, str) or session not in sessions.SESSIONS:
        raise HTTPException(422,'研究时段无效')
    market_day = raw.get('market_day') or next(iter(dates), None)
    market_day = assets.day(market_day) if market_day else None
    spec=sessions.SESSIONS[session]
    if not raw.get('intent') and raw.get('kind')!='review':
        intent=spec['goal']+'。逐股说明支持证据、反证、触发和失效条件。'
    constraints = preferences.interpret(intent)
    # Intent overrides confirmed defaults only when it is explicit and within the current study contract.
    risk = re.search(r'(?:风险|风格)\s*[:：]?\s*(稳健|均衡|进取)', intent)
    if risk:
        constraints['risk'] = {'稳健':'conservative','均衡':'balanced','进取':'opportunistic'}[risk[1]]
    task = 'review' if raw.get('kind') == 'review' else 'compare' if re.search('比较|对比|谁更', intent) and symbols else 'discover'
    rise_type = raw.get('rise_type') or None
    if rise_type is not None and (not isinstance(rise_type,str) or rise_type not in rises.TYPES):
        raise HTTPException(422,'请选择有效的上涨类型')
    if rise_type and (symbols or task=='review'):
        raise HTTPException(422,'按上涨类型筛选时请清空指定股票及复核周期')
    questions = ['逐股核对可得资料和原失效条件'] if task == 'review' else [
        f"筛选{rises.TYPES[rise_type]}并核对触发与失效条件" if rise_type else '从四类上涨与八条发现路径筛选，并标出范围缺口']
    if re.search('原文|财报|公告|政策|披露|数字', intent):
        questions.append('定位公告、财报或政策原页段，复算关键数字')
    if re.search('错因|复盘|失效|去留', intent):
        questions.append('区分原判、触发、失效和评价样本')
    questions.extend(['按十四维读取支持与反证，并比较候选与同行','校验参与条件、失效位、期限和后续核查'])
    extended = re.search(r'(?<!\w)D(?:5|10|20)(?!\w)',intent,re.I)
    extended_horizon = extended[0].upper() if extended else 'D20' if re.search('中期|波段',intent) else None
    if extended_horizon and session!='close':
        raise HTTPException(422,'扩展期限请选择收盘研究。午盘看当日下午，尾盘看下一交易日。')
    preferred=constraints.get('horizon',profile.get('horizon'))
    primary=preferred if session=='close' and preferred in ('D1','D3') else spec['horizon']
    return {'intent':intent,'task':task,'symbols':symbols,'session':session,'market_day':market_day,
            'rise_type':rise_type,'rise_type_name':rises.TYPES.get(rise_type),
            'horizon':primary,'goal':spec['goal'],'evaluation':spec['evaluation'],
            'constraints':constraints,'steps':questions,'tool_names':list(TOOLS) if raw.get('allow_evidence_round') is True else [],
            'extended_horizon':extended_horizon}
