"""Bounded, dated evidence for discovery and formal decision checks."""
from collections import defaultdict
from datetime import datetime, timedelta
import math
import re

from fastapi import HTTPException

from foundation import assets, database as store, record_store


DOMAINS = {'announcements': (45, 12000), 'financials': (560, 24000), 'fund_flows': (30, 12000), 'actions': (100, 6000)}
CHECKS = {'risk': 'announcements', 'actions': 'actions'}
EVENT = re.compile(r'签订|中标|收到.*批复|正式实施|完成回购|订单|业绩预告')
COST = re.compile(r'原材料|采购价格|库存|产能利用率|单位成本|销售价格')
RISK_CLEARANCE = re.compile(r'撤销.*风险警示|解除.*风险警示|撤销.*立案|终结.*调查|解除.*冻结')
RISK_CONFLICT = re.compile(r'尚未|存在不确定|风险未|未生效|终止|取消|意向|无法确定')
RISK_ALERT = re.compile(r'风险警示|立案|监管措施|行政处罚|重大诉讼|冻结|停牌|减持|非标准审计')


def numeric(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def visible(row, value, cutoff, market_day, intraday, publication_cutoff):
    if row['first_seen'] > cutoff:
        return False
    stamp = value.get('published')
    if stamp and (stamp[:10] > publication_cutoff[:10] or len(stamp) > 10 and stamp > publication_cutoff):
        return False
    # Day-only releases and flows cannot be assigned to an earlier intraday timestamp.
    if intraday and row['day'] == market_day and value.get('record_kind')!='coverage' and (not stamp or len(stamp) == 10):
        return False
    return True


def collect(conn, day, cutoff, intraday, symbols=None, *, historical_cutoff=None):
    evidence = defaultdict(lambda: defaultdict(list))
    limits = []
    for dataset, (days, maximum) in DOMAINS.items():
        start = (datetime.fromisoformat(day) - timedelta(days=days)).date().isoformat()
        end = cutoff[:10] if not historical_cutoff and dataset in ('announcements', 'financials') else day
        scope = ' AND h.symbol IN (' + ','.join(['%s'] * len(symbols)) + ')' if symbols else ''
        text_priority = bool(symbols and dataset == 'announcements')
        order = '(a.text IS NOT NULL) DESC,' if text_priority else ''
        join = ' JOIN announcements a ON a.fact_id=f.id ' if text_priority else ' '
        rows = conn.execute(
            "SELECT f.id,h.day,h.symbol,f.source,f.first_seen FROM heads h JOIN facts f ON f.id=h.fact_id" + join +
            "WHERE h.dataset=%s AND h.day BETWEEN %s AND %s AND f.first_seen<=%s" + scope
            + " ORDER BY " + order + "h.day DESC,h.symbol,h.record_key LIMIT %s",
            (dataset, start, end, cutoff, *(symbols or []), maximum + 1),
        ).fetchall()
        if len(rows) > maximum:
            limits.append(dataset)
        # Load bounded typed records after the metadata query is exhausted. This
        # avoids a second query while an unbuffered server cursor is active.
        for offset in range(0, min(len(rows), maximum), 1000):
            for row in record_store.hydrate(conn, rows[offset:min(offset + 1000, maximum)]):
                value = row['payload']
                if not visible(row, value, cutoff, day, intraday, historical_cutoff or cutoff):
                    continue
                slot = evidence[row['symbol']][dataset]
                if len(slot) >= (12 if dataset == 'financials' else 6):
                    continue
                if value.get('text'):
                    value['text'] = value['text'][:1200]
                slot.append({**value, 'id': row['id'], 'source': row['source'], 'observed_at': row['first_seen']})
    return evidence, limits


def _filing_operating(financials, latest_period):
    filings = [row for row in financials if row.get('report_type') == 'filing'
               and row.get('report_date') and row.get('fields', {}).get('period_basis') == 'cumulative'
               and row.get('fields', {}).get('currency') == 'CNY']
    current = max((row for row in filings if row['report_date'] == latest_period
                   and numeric(row.get('fields', {}).get('operating_revenue')) is not None),
                  key=lambda row: (row.get('published', ''), row['id']), default=None)
    if current is None:
        return None
    fields = current['fields']
    year, period = current['report_date'].split('-', 1)
    prior = max((row for row in filings if row['report_date'] == f'{int(year)-1}-{period}'
                 and row.get('source') == current.get('source')),
                key=lambda row: (row.get('published', ''), row['id']), default=None)
    revenue = numeric(fields.get('operating_revenue'))
    profit = numeric(fields.get('parent_profit'))
    cash = numeric(fields.get('operating_cash'))
    cash_ratio = cash/revenue if cash is not None and revenue is not None and revenue > 0 else None
    result = {'report_date': current['report_date'], 'published': current['published'],
              'revenue': revenue, 'parent_profit': profit, 'profit_basis': 'parent_profit',
              'revenue_basis': 'operating_revenue', 'period_basis': 'cumulative',
              'source': current.get('source'), 'operating_cash_to_revenue': cash_ratio,
              'fact_ids': [current['id']], 'improving': False}
    compared = [current]
    if prior:
        previous = prior['fields']
        previous_revenue = numeric(previous.get('operating_revenue'))
        previous_profit = numeric(previous.get('parent_profit'))
        result.update(prior_parent_profit=previous_profit, prior_revenue=previous_revenue)
        result['fact_ids'].append(prior['id'])
        compared.append(prior)
        result['improving'] = (profit is not None and previous_profit is not None
                               and profit > max(previous_profit, 0)
                               and revenue is not None and previous_revenue is not None
                               and revenue > previous_revenue > 0
                               and cash_ratio is not None and cash_ratio > 0)
    for row in compared:
        source_id = row['fields'].get('filing_fact_id')
        if type(source_id) is int and source_id > 0:
            result['fact_ids'].append(source_id)
    result['fact_ids'] = list(dict.fromkeys(result['fact_ids']))
    return result


def operating_facts(financials):
    periods = [row['report_date'] for row in financials if row.get('report_type') in ('profit', 'filing')
               and row.get('report_date')]
    if periods:
        filing = _filing_operating(financials, max(periods))
        if filing is not None:
            return filing
    profit = [row for row in financials if row.get('report_type') == 'profit' and row.get('report_date')]
    if not profit:
        return None
    current = max(profit, key=lambda row: row['report_date'])
    year, period = current['report_date'].split('-', 1)
    prior = next((row for row in profit if row['report_date'] == f'{int(year)-1}-{period}' and row['source'] == current['source']), None)
    cash = next((row for row in financials if row.get('report_type') == 'cashflow' and row.get('report_date') == current['report_date'] and row['source'].startswith('bs_')), None)
    fields = current.get('fields', {})
    result = {'report_date': current['report_date'], 'published': current['published'],
              'net_profit': numeric(fields.get('netProfit')), 'revenue': numeric(fields.get('MBRevenue')),
              'profit_basis': 'net_profit', 'revenue_basis': 'main_revenue',
              'fact_ids': [current['id']], 'improving': False}
    if prior and cash:
        previous_profit = numeric(prior.get('fields', {}).get('netProfit'))
        previous_revenue = numeric(prior.get('fields', {}).get('MBRevenue'))
        cash_ratio = numeric(cash.get('fields', {}).get('CFOToOR'))
        result.update(prior_net_profit=previous_profit, prior_revenue=previous_revenue, operating_cash_to_revenue=cash_ratio)
        result['fact_ids'].extend((prior['id'], cash['id']))
        result['improving'] = all(result[key] is not None for key in ('net_profit', 'revenue')) and (
            previous_profit is not None and previous_revenue is not None and cash_ratio is not None
            and result['net_profit'] > max(previous_profit, 0) and result['revenue'] > previous_revenue > 0 and cash_ratio > 0
        )
    return result


def assess(documents, metrics, industry_valuations):
    """Return verifiable recall triggers, never an inferred buy action."""
    paths = {}
    announcements = documents.get('announcements', [])
    def notice(pattern):
        return next((r for r in announcements if r.get('text') and pattern.search(r['title'] + r['text'][:1200])
                     and not RISK_CONFLICT.search(r['title'])), None)

    event = notice(EVENT)
    if event:
        paths['O01'] = [event['id']]
    operating = operating_facts(documents.get('financials', []))
    if operating and operating['improving']:
        paths['O06'] = operating['fact_ids']
    cost = notice(COST)
    if cost and operating and operating['improving']:
        paths['O07'] = [cost['id'], *operating['fact_ids']]
    risk = notice(RISK_CLEARANCE)
    if risk and industry_valuations and 0 < (metrics.get('pe') or 0) < industry_valuations['median_pe']:
        paths['O08'] = [risk['id']]
    return {'triggers': paths, 'operating': operating, 'industry_valuation': industry_valuations}


def read_checks(conn, symbol, cutoff):
    result = {}
    for kind in CHECKS:
        row = store.setting(f'research_check:{symbol}:{kind}', {}, conn=conn)
        if row and row['confirmed_at'] <= cutoff and row['valid_until'] >= cutoff[:10]:
            ids = row['fact_ids']
            count = conn.execute('SELECT COUNT(*) FROM facts WHERE id IN (' + ','.join(['%s'] * len(ids)) + ')', ids).fetchone()[0]
            if count == len(ids):
                if kind == 'risk' and row['state'] == 'verified':
                    newer = conn.execute(
                        "SELECT a.title FROM heads h JOIN facts f ON f.id=h.fact_id JOIN announcements a ON a.fact_id=f.id WHERE h.dataset='announcements' "
                        "AND h.symbol=%s AND f.first_seen>%s AND f.first_seen<=%s "
                        "AND a.title REGEXP %s "
                        "ORDER BY f.first_seen DESC,f.id DESC LIMIT 1", (symbol,row['confirmed_at'],cutoff,RISK_ALERT.pattern),
                    ).fetchall()
                    if any(RISK_ALERT.search(r['title'] or '') for r in newer):
                        row = {**row, 'state':'blocked','note':'核验后发现新风险公告，待复核'}
                result[kind] = row
    return result


def save_check(raw):
    if set(raw) - {'symbol', 'kind', 'fact_ids', 'note', 'valid_until', 'state'}:
        raise HTTPException(422, '证据核验包含未知字段')
    symbol = assets.symbol(raw.get('symbol'))
    kind = raw.get('kind')
    if kind not in CHECKS or raw.get('state') not in ('verified', 'blocked', 'revoke'):
        raise HTTPException(422, '核验类型或结论无效')
    ids = raw.get('fact_ids', [])
    if not isinstance(ids, list) or len(ids) > 8 or any(type(i) is not int or i <= 0 for i in ids):
        raise HTTPException(422, '核验须引用至多8条真实事实编号')
    key = f'research_check:{symbol}:{kind}'
    with store.db() as conn:
        if raw['state'] == 'revoke':
            conn.execute('DELETE FROM settings WHERE `key`=%s', (key,))
            assets.bump_generation(conn, 'research_check')
            store.audit('research_check_revoke', {'symbol': symbol, 'kind': kind}, conn=conn)
            return {'symbol': symbol, 'kind': kind, 'state': 'revoked'}
        ids = list(dict.fromkeys(ids))
        note = raw.get('note', '')
        until = assets.day(raw.get('valid_until'))
        today = store.today()
        if not isinstance(note, str) or not note.strip() or len(note) > 700 or not ids or not today <= until <= (datetime.fromisoformat(today) + timedelta(days=30)).date().isoformat():
            raise HTTPException(422, '请登记事实、核查范围与结论，并设置不超过30日的有效期')
        rows = conn.execute("SELECT id,day FROM facts WHERE dataset=%s AND symbol=%s AND id IN ("
                            + ','.join(['%s'] * len(ids)) + ')', (CHECKS[kind], symbol, *ids)).fetchall()
        if len(rows) != len(ids) or any(r['day'] > today for r in rows):
            raise HTTPException(422, '引用必须来自该证券的对应已公开事实')
        values = record_store.load_many(conn, ids) if kind == 'risk' else {}
        if kind == 'risk' and any(not values[r['id']].get('text') for r in rows):
            raise HTTPException(422, '风险核验须引用已有正文摘录的公告')
        result = {'symbol': symbol, 'kind': kind, 'state': raw['state'], 'fact_ids': ids, 'note': note.strip(),
                  'valid_until': until, 'confirmed_at': store.now(), 'basis': '本机人工核验记录'}
        store.set_setting(key, result, conn=conn)
        assets.bump_generation(conn, 'research_check')
        store.audit('research_check', {'symbol': symbol, 'kind': kind, 'state': raw['state'], 'fact_ids': ids}, conn=conn)
        return result


def compact(documents, assessment, checks):
    references = set(fid for ids in assessment['triggers'].values() for fid in ids)
    # The operating comparison is evidence even when missing revenue keeps its
    # improving trigger false; preserve every fact used by that comparison.
    references.update((assessment.get('operating') or {}).get('fact_ids', []))
    references.update(fid for r in checks.values() for fid in r['fact_ids'])
    result = []
    for domain, rows in documents.items():
        required = [row for row in rows if row['id'] in references]
        # Source collection and check schemas already bound these rows. Do not
        # truncate declared citations just to satisfy the optional context count.
        selected = required + [row for row in rows if row['id'] not in references][:max(0, 3-len(required))]
        for row in selected:
            values = {k: row[k] for k in ('id', 'source', 'day', 'observed_at', 'published', 'title', 'url', 'report_date', 'report_type', 'net_main', 'unit', 'basis', 'time_precision','coverage_from','coverage_through','event_count') if k in row}
            if row.get('text'):
                values.update(excerpt=row['text'][:1200], excerpt_limited=True)
            values['dataset'] = domain
            result.append(values)
            fields = row.get('fields', {}) if domain == 'financials' and row.get('report_type') == 'filing' else {}
            original_id = fields.get('filing_fact_id')
            if type(original_id) is int and original_id > 0 and not any(item['id'] == original_id for item in result):
                result.append({'id': original_id, 'dataset': 'announcements', 'source': 'cninfo_text',
                               'title': fields.get('filing_title'), 'url': fields.get('filing_url'),
                               'published': fields.get('filing_published'), 'derived_financial_reference': True})
    return result


def restore_excerpts(conn, documents):
    ids = [d['id'] for d in documents if d.get('excerpt_limited')]
    if not ids:
        return
    rows = conn.execute("SELECT f.id,a.text FROM facts f JOIN announcements a ON a.fact_id=f.id WHERE f.dataset='announcements' AND f.id IN ("
                        + ','.join(['%s']*len(ids))+")", ids).fetchall()
    texts = {r['id']:r['text'] for r in rows}
    for doc in documents:
        if doc.get('excerpt_limited'):
            text = texts.get(doc['id'])
            if not text:
                raise HTTPException(409, '本轮冻结的公告正文已不可读，请重新核对证据')
            doc['excerpt'] = text[:1200]
