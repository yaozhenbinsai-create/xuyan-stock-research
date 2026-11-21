"""Read the evidence that a candidate actually has at its frozen cutoff."""
from __future__ import annotations

from research import evidence, methods


VERSION = 'dimension-evidence-v1'
GAPS = {
    'F01':'需有效的同截点市场样本才可比较市场强弱。',
    'F02':'行业涨跌按样本等权计算，样本包含本股。政策或产业链受益仍需核对公司业务。',
    'F03':'需带发布时间、主体、适用条件的政策或公司公告原文。',
    'F04':'完整日线与盘中同刻量分开比较。无有效对照时保留量能缺口。',
    'F05':'异动簇描述历史正负活性。复权断点、停牌和催化需分别核对。',
    'F06':'用完整价柱、公司行动覆盖和后续回踩核验结构。',
    'F07':'需可比报告期的营收、利润和经营现金流，并核对披露口径。',
    'F08':'同行正市盈率仅供相对比较。已定价预期需要盈利口径和历史预期证据。',
    'F09':'需库存、销售价格、原料成本或合同的时间序列及公司实际敞口。',
    'F10':'需同定义、同口径的资金或持仓数据，才能核对持续性。',
    'F11':'情绪和关注的变化需可去重的传播样本、独立来源及时间窗。',
    'F12':'成交额描述当期活跃度。实际参与还需报价、价差和可成交条件。',
    'F13':'需有效期内的风险核验，覆盖监管、诉讼、偿债及减持等反证。',
    'F14':'需确认的组合约束与原定参与窗口内的有效报价。',
}
BIAS = {'demand':'偏需求','supply':'偏供给','demand_test':'待核需求测试',
        'supply_test':'待核供给测试','two_sided':'保留供需两种解释'}


def _readings(*triples):
    return [{'label':label,'value':value,'unit':unit} for label,value,unit in triples if value is not None]


def _difference(left, right):
    return left-right if left is not None and right is not None else None


def _ids(*groups):
    return list(dict.fromkeys(fid for group in groups for fid in group if type(fid) is int))


def _refs(items, label):
    return [{'id':row['id'],'day':(row.get('published') or row.get('day') or '')[:10],
             'title':row.get('title') or label} for row in items[:3]]


def _market(market, pointed):
    if market.get('breadth_context') is not None:
        return market['breadth_context']
    if pointed or not market.get('covered'):
        return {}
    return {'sample':market['covered'],'advancing':market.get('advancing'),
            'declining':market.get('declining'),'mean_return':market.get('mean_return'),
            'basis':'本轮具备有效研究历史的证券'}


def build(market, candidate, *, pointed=False):
    """Combine frozen facts without re-reading today's prices into an old report."""
    m = candidate['metrics']
    sector = candidate.get('sector') or {}
    study = m.get('structure') or {}
    bounds = study.get('range') or {}
    comparable = m.get('comparable')
    last_ids = candidate.get('bar_ids', [])[-1:]
    docs = candidate.get('documents', [])
    notices = [row for row in docs if row['dataset']=='announcements']
    flows = [row for row in docs if row['dataset']=='fund_flows']
    finances = [row for row in docs if row['dataset']=='financials']
    checks = candidate.get('checks') or {}
    risk = checks.get('risk') or {}
    quote = candidate.get('reference_quote') or {}
    originals = candidate.get('originals') or {}
    actual = candidate.get('research_evidence') or {}
    triggers = actual.get('triggers') or {}
    operating = actual.get('operating') or {}
    valuation = actual.get('industry_valuation') or {}
    market_data = _market(market, pointed)
    move = m.get('return_1')
    market_excess = _difference(move,market_data.get('mean_return'))
    peers = sector.get('members', 0)
    sector_excess = _difference(move,sector.get('mean_return')) if peers>=2 else None
    market_sample = market_data.get('sample')
    advancing = market_data.get('advancing')
    sector_advancing = sector.get('advancing')
    spread = m.get('close_location')
    relative = m.get('relative_volume20')
    close = m.get('close')
    previous_high = m.get('prior_high20')
    pe = m.get('pe')
    median = valuation.get('median_pe')
    pe_ratio = pe/median if pe and pe>0 and median and median>0 else None
    latest_cluster = m.get('last_positive_cluster') if comparable else None
    positive = m.get('positive_clusters') if comparable else None
    negative = m.get('negative_clusters') if comparable else None
    clusters = m.get('activity_clusters', []) if comparable else []
    recent_up = next((row for row in reversed(clusters) if row['direction']=='up'),{})
    recent_down = next((row for row in reversed(clusters) if row['direction']=='down'),{})
    original_fields=sorted(originals.get('financial_fields', []),
                           key=lambda row:(row.get('period',''),{'cumulative':2,'quarterly':1}.get(row.get('basis'),0)),
                           reverse=True)
    original_rows=[]
    for accounts in (('main_revenue','operating_revenue'),('parent_profit',),('operating_cash',)):
        selected=next((row for row in original_fields if row.get('metric') in accounts),None)
        if selected:original_rows.append(selected)
    policy_links = originals.get('industry_links', [])
    anchors = {row['id']:row['fact_id'] for row in originals.get('anchors', [])}
    allowed_ids = set(candidate.get('evidence_ids', []))
    current = {code:{'id':code,'name':name,'question':question,'state':'missing',
                    'reason':'原稿未登记该项状态'} for code,name,question,_ in methods.DIMENSIONS}
    current.update({row['id']:row for row in candidate.get('dimensions', [])})
    result = {}

    def put(code, readings=(), *, note=None, state=None, facts=(), documents=()):
        row = current[code]
        result[code] = {**row, 'state':state or row['state'],'readings':readings,
                        'note':note or GAPS[code],
                        'facts':[fid for fid in _ids(facts) if fid in allowed_ids],
                        'documents':list(documents)}

    put('F01', _readings(('有效市场样本',market_sample,'stocks'),
                         ('样本上涨比例',advancing/market_sample if market_sample and advancing is not None else None,'ratio'),
                         ('样本平均涨跌',market_data.get('mean_return'),'ratio'),('个股相对市场',market_excess,'pp')),
        note=f"{market_data['basis']}。等权样本用于比较相对表现。" if market_data else None,
        state=None if market_data else 'missing')
    put('F02', _readings(('所属行业',candidate.get('industry'),'text'),('行业样本',peers or None,'stocks'),
                         ('行业上涨比例',sector_advancing/peers if peers and sector_advancing is not None else None,'ratio'),
                         ('行业平均涨跌',sector.get('mean_return'),'ratio'),('个股相对行业',sector_excess,'pp')),
        facts=[candidate.get('industry_fact_id')])
    put('F03', _readings(('已冻结公司公告',len(notices) or None,'items'),('政策与业务词项交集',len(policy_links) or None,'items')),
        facts=[anchors.get(anchor) for link in policy_links[:2] for anchor in link['anchors']],
        documents=_refs(notices,'公司公告'))
    put('F04', _readings(('截点价',close,'price'),('截点涨跌',move,'ratio'),
                         ('近五日涨跌',m.get('return_5') if comparable else None,'ratio'),
                         ('相对量',relative,'times'),('价格在日内区间的位置',spread,'ratio')),
        note='盘中成交量与历史同刻快照比较。' if m.get('partial_bar') and relative is not None
             else '已读取价格，相对量缺可比样本。' if relative is None else '相对量使用已存的完整日线成交量。',
        state='partial' if relative is None or spread is None or move is None else None, facts=last_ids)
    put('F05', _readings(('正向异动簇',positive,'items'),('负向异动簇',negative,'items'),
                         ('最近正向异动',latest_cluster,'text'),('最近正向簇后三日涨跌',recent_up.get('follow_through_3'),'ratio')),
        note=None if comparable else '原始价格的跨日可比性不足，暂停比较异动簇。',
        state=None if comparable else 'missing',
        facts=_ids(recent_up.get('fact_ids', []),recent_down.get('fact_ids', [])))
    put('F06', _readings(('结构观察',BIAS.get(study.get('bias')),'text'),
                         ('区间下沿',bounds.get('low'),'price'),('区间上沿',bounds.get('high'),'price')),
        note=study.get('counterpoint'),
        facts=_ids([(study.get('guard') or {}).get('fact_id')],study.get('action_fact_ids', [])))
    period = operating.get('report_date')
    prior_revenue = operating.get('prior_revenue')
    revenue = operating.get('revenue')
    revenue_growth = revenue/prior_revenue-1 if revenue is not None and prior_revenue and prior_revenue>0 else None
    parent_profit=operating.get('profit_basis')=='parent_profit'
    profit=operating.get('parent_profit') if parent_profit else operating.get('net_profit')
    put('F07', _readings(('财务报告期',period,'text'),('营业收入',operating.get('revenue'),'cny'),
                         ('归母净利润' if parent_profit else '净利润',profit,'cny'),('经营现金流／营业收入',operating.get('operating_cash_to_revenue'),'ratio'),
                         ('可比口径营收同比',revenue_growth,'ratio'),
                         *[(f"{row['period']} {row.get('label') or row['metric']}（{ {'quarterly':'本季','cumulative':'累计'}.get(row.get('basis'),'口径待核') }）原文金额",
                            evidence.numeric(row['amount_yuan']),'cny') for row in original_rows]),
        facts=_ids(operating.get('fact_ids', []),[anchors.get(row['anchor_id']) for row in original_rows]),
        documents=_refs(finances,'财务披露'))
    put('F08', _readings(('个股市盈率字段',pe,'times'),('同行正市盈率中位数',median,'times'),
                         ('可比同行样本',valuation.get('members'),'stocks'),('相对同行中位数',pe_ratio,'times')),
        facts=last_ids if pe is not None else ())
    cost_ids = set(triggers.get('O07', []))
    cost_docs = [row for row in notices if row['id'] in cost_ids or evidence.COST.search(row.get('title') or '')]
    put('F09', _readings(('涉及供需或成本的公告',len(cost_docs) or None,'items')),
        state='partial' if cost_docs else None, documents=_refs(cost_docs,'供需与成本公告'))
    latest_flow = next((row for row in flows if row.get('unit') in ('CNY','yuan','元')
                        and evidence.numeric(row.get('net_main')) is not None),{})
    put('F10', _readings(('已冻结资金资料',len(flows) or None,'items'),
                         ('最近主力净额字段',evidence.numeric(latest_flow.get('net_main')),'cny')),
        documents=_refs(flows,'资金资料'))
    put('F11')
    put('F12', _readings(('截点成交额',m.get('amount'),'cny'),('参考报价时间',quote.get('at'),'text')),
        facts=_ids(last_ids,[quote.get('id')]))
    risk_docs = [row for row in notices if evidence.RISK_ALERT.search(row.get('title',''))]
    put('F13', _readings(('风险核验有效期',risk.get('valid_until'),'text'),('风险关键词公告',len(risk_docs) or None,'items')),
        note=risk.get('note'), state='partial' if risk.get('state')=='blocked' else None,
        facts=risk.get('fact_ids', []), documents=_refs(risk_docs,'公司风险公告'))
    signal = m.get('rise') or {}
    put('F14', _readings(('价格须高于',signal.get('trigger_above'),'price'),
                         ('收盘失效位',signal.get('invalidation_price'),'price'),('截点参考报价',quote.get('price'),'price')),
        facts=_ids(signal.get('facts', []),[quote.get('id')]))

    def combination(codes, title, state, summary):
        rows = [result[code] for code in codes]
        sources = _ids(*(row['facts'] + [doc['id'] for doc in row['documents']] for row in rows))
        return {'dimensions':codes,'names':[row['name'] for row in rows],
                'title':title,'state':state,'summary':summary,'facts':sources[:6]}

    relative_text = '个股相对市场或行业样本不足，等待可比对照。'
    if market_excess is not None and sector_excess is not None:
        relative_text = f"个股相对市场 {market_excess*100:+.2f} 个百分点，相对行业 {sector_excess*100:+.2f} 个百分点。"
    if not comparable:
        structure_text = '原始价格存在跨日断点，历史活性和结构暂不能互相验证。'
    elif 'O03' in candidate.get('paths', []) and previous_high is not None and relative is not None:
        structure_text = f"价格越过前二十日上沿 {previous_high:g}，相对量 {relative:.2f} 倍。" + (
            '已有完整价柱与公司行动核验，后续观察能否守住上沿。' if study.get('gate_eligible')
            else '结构仍待核验，回落至原上沿之下会削弱突破依据。')
    elif close is not None and previous_high is not None and close>previous_high:
        structure_text = '价格越过前二十日上沿，量能或截点收强条件尚未同时成立。'
    else:
        structure_text = '已有截点价量和历史活性，量价突破条件未同时成立。结合区间位置等待下一截点确认。'
    if comparable and positive is not None and negative is not None:
        structure_text += f" 观察窗口有 {positive} 个正向簇、{negative} 个负向簇。"
    financial_conflict = any(row['state']=='differs' for row in originals.get('comparisons', []))
    profit_fields=('parent_profit','prior_parent_profit') if parent_profit else ('net_profit','prior_net_profit')
    financial_complete = all(operating.get(key) is not None for key in
                             (*profit_fields,'revenue','prior_revenue','operating_cash_to_revenue'))
    if financial_conflict:
        financial_text = '同期间财报原文金额出现差异，先核对口径和版本。'
    elif financial_complete and pe_ratio is not None:
        financial_text = f"已有同期利润、现金流和同行估值。个股市盈率为同行中位数 {pe_ratio:.2f} 倍，盈利预期仍待核。"
    elif pe_ratio is not None:
        financial_text = f"个股市盈率为同行中位数 {pe_ratio:.2f} 倍。可比财务和经营现金流仍待补齐，才能核对估值的经营依据。"
    else:
        financial_text = '对照估值与业绩需要可比财务、现金流和同行口径。当前组合证据不足。'
    if policy_links:
        policy_text = f"政策原文与公司披露均出现“{policy_links[0]['term']}”，适用范围与利润影响待核。"
    elif cost_docs and operating:
        policy_text = '成本或供需公告与经营指标已有交集，影响方向与兑现期仍需核对。'
    elif cost_docs:
        policy_text = '发现涉及供需或成本的公告。经营数字和实际传导仍待核。'
    else:
        policy_text = '需同时取得事件原文、公司业务敞口和兑现证据，当前保留缺口。'
    pending = [label for label,ready in (
        ('风险核验',risk.get('state')=='verified'),
        ('公司行动核验',m.get('action_adjustment_verified')),
        ('参与时核价' if result['F14']['state']=='conditional' else '参与报价与组合约束',result['F14']['state']=='verified'),
    ) if not ready]
    combinations = [
        combination(['F01','F02','F04'],'市场、行业与个股相对强弱',
                    'compared' if market_excess is not None and sector_excess is not None else 'partial',relative_text),
        combination(['F04','F05','F06'],'量价、历史活性与结构',
                    'compared' if study.get('gate_eligible') and relative is not None else 'partial',structure_text),
        combination(['F07','F08'],'经营兑现与估值',
                    'conflict' if financial_conflict else 'compared' if financial_complete and pe_ratio is not None else 'partial',financial_text),
        combination(['F02','F03','F07','F09'],'政策产业到公司兑现',
                    'partial' if policy_links or cost_docs else 'missing',policy_text),
        combination(['F04','F10','F11'],'资金关注与量价印证',
                    'partial' if flows else 'missing','当前尚缺同口径资金与可去重关注数据的交叉核验。'),
        combination(['F12','F13','F14'],'流动性、风险与参与',
                    'partial' if pending else 'compared','正式入选待补齐'+'、'.join(pending)+'。' if pending
                    else '已有风险与核价所需的基础材料，等待签发校验。'),
    ]
    states = [row['state'] for row in result.values()]
    return {'version':VERSION,'items':list(result.values()),'combinations':combinations,
            'coverage':{'supported':sum(s in ('available','verified') for s in states),
                        'partial':sum(s in ('partial','conditional') for s in states),'missing':states.count('missing')}}
