"""Derive dated financial amounts from retained, anchored CNINFO filings.

No provider requests or writes occur here. The collector persists these records
through its normal fact/version path after it has acquired original excerpts.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import re

from foundation import assets, database as store, record_store
from research import originals


VERSION = 'filing-financials-v4'
METRICS = {'operating_revenue', 'parent_profit', 'operating_cash'}
_UNITS = {**originals.UNITS, '百万元': originals.Decimal(1000000)}
_UNIT = re.compile(r'(?:单位\s*[:：]\s*(?:人民币\s*)?|人民币\s*)(亿元|百万元|万元|千元|元)(?:人民币)?(?:列示)?(?![\u4e00-\u9fff])')
_PAREN_PERCENT = re.compile(r'\s*(?P<percent>[（(][+-]?\d[\d,，]*(?:\.\d+)?[%％][)）])(?P<number>)(?P<missing>)')
_LABELS = {**originals.METRICS, '归属于本行股东的净利润': 'parent_profit',
           '净利润（归属于母公司股东）': 'parent_profit',
           '经营活动使用的现金流量净额': 'operating_cash'}
_SIX_COLUMNS = re.compile(
    r'(?:项目)?本报告期上年同期本报告期比上年同期(?:增减(?:变动)?(?:幅度)?|增长率?)(?:\([%％]\))?'
    r'年初至报告期末上年同期年初至报告期末比上年同期(?:增减(?:变动)?(?:幅度)?|增长率?)(?:\([%％]\))?')
_TWO_COLUMNS = re.compile(r'合并(?P<ytd>年初(?:到|至)报告期末)?(?:利润表|现金流量表).*?项目本期发生额上期发生额')
_HEADING = re.compile(r'(?m)^[^\n]{0,100}?(20\d{2})\s*(?:年\s*)?'
                      r'(第一季度|一季度|半年度|第三季度|三季度|年度)\s*报告'
                      r'(?:\s*[（(][^\n()（）]{0,25}[)）])?\s*$')
_WRAPPED_AMOUNT = re.compile(
    r'\s*(?P<number>[-−+]?(?:\d{1,3}(?:\s*,\s*\d(?:\s*\d){2})+|\d+)'
    r'(?:\s*\.\s*\d(?:\s*\d)?)?)')


class _WrappedCell:
    def __init__(self, match):
        self.values = {'number': re.sub(r'\s+', '', match['number']), 'percent': None, 'missing': None}
        self.position = match.end()

    def __getitem__(self, key):
        return self.values[key]

    def end(self):
        return self.position


def _parsing_fact(fact):
    """Resolve an explicit report heading without changing the saved title."""
    title = re.sub(r'[_＿]+', ' ', fact.get('title') or '')
    identified = originals.filing_title_period(title)
    # An invalid announcement title is distinct from an actual yearless report.
    # A report heading quoted in an investor meeting notice cannot rescue it.
    if identified is None:
        return None, None
    canonical = lambda value: {'一季度': '第一季度', '三季度': '第三季度'}.get(value, value)
    title_year, title_period = identified[0], canonical(identified[1])
    title_periods = {(m[1], canonical(m[2])) for m in originals.FISCAL.finditer(title)}
    prefix = fact['text'][:2500]
    # Only report headings before financial data or narrative declarations are
    # evidence of this document's period, not comparative years in a table.
    boundary = re.search(r'重要内容提示|主要财务数据|主要会计数据|本公司董事会|本行董事会', prefix)
    if boundary:
        prefix = prefix[:boundary.start()]
    headings = list(_HEADING.finditer(prefix))
    body_periods = {(m[1], canonical(m[2])) for m in headings}
    if len(title_periods) > 1 or len(body_periods) > 1:
        return None, None
    if title_year:
        if body_periods and body_periods != {(title_year, title_period)}:
            return None, None
        return {**fact, 'title': f'{title_year}年{title_period}报告'}, None
    if len(body_periods) != 1:
        return None, None
    year, period = next(iter(body_periods))
    if period != title_period:
        return None, None
    heading = headings[0]
    return {**fact, 'title': f'{year}年{period}报告'}, originals._anchor(
        fact, heading.start(), heading.end(), '财务报告期间')


def _cells(tail):
    result = []
    position = 0
    while len(result) < 10:
        wrapped = _WRAPPED_AMOUNT.match(tail, position)
        # Join a line-broken comma group or fractional part only. Bare numeric
        # rows such as "20\n26" must remain two independent table cells.
        if wrapped and '\n' in wrapped['number']:
            item = _WrappedCell(wrapped)
        else:
            item = originals._CELL.match(tail, position) or _PAREN_PERCENT.match(tail, position)
        if item is None:
            break
        result.append(item)
        position = item.end()
    return result


def _amount(cell, unit):
    if cell['missing'] or cell['percent']:
        return None
    literal = cell['number'].replace(',', '').replace('，', '').replace('−', '-').replace('（', '(').replace('）', ')')
    try:
        value = originals.Decimal('-' + literal[1:-1] if literal.startswith('(') and literal.endswith(')') else literal)
        return str(value * _UNITS[unit]) if value.is_finite() else None
    except originals.InvalidOperation:
        return None


def _summary_rows(fact):
    """Recognize complete same-table headers, never infer prior amounts from %.

The existing original-review parser remains unchanged. This additional typed
import handles explicit comparative columns and bank currency headings as well.
"""
    period = originals.FISCAL.search(fact['title'])
    if not period:
        return []
    end = {'第一季度': '03-31', '一季度': '03-31', '半年度': '06-30',
           '第三季度': '09-30', '三季度': '09-30', '年度': '12-31'}[period[2]]
    current_period = period[1] + '-' + end
    previous_period = str(int(period[1]) - 1) + '-' + end
    body = fact['text']
    result = []
    quarter_span = period[1] + r'年7(?:至|[-—])9月'
    cumulative_span = period[1] + r'年1(?:至|[-—])9月'
    growth = r'(?:比上年同期增减|同比增减)(?:\([%％]\))?'
    bank_columns = quarter_span + '(?:' + quarter_span + ')?' + growth + cumulative_span + '(?:' + cumulative_span + ')?' + growth
    through_september = r'截至' + period[1] + r'年9月30日止'
    duration_columns = through_september + r'三个月' + growth + through_september + r'九个月' + growth
    for label, metric in _LABELS.items():
        if metric not in METRICS:
            continue
        for match in re.finditer(r'\s*'.join(map(re.escape, label)), body):
            line_start = body.rfind('\n', 0, match.start()) + 1
            if re.fullmatch(r'\s*(?:其中\s*[:：])?\s*', body[line_start:match.start()]) is None:
                continue
            page_start = max((m.end() for m in originals.PAGE.finditer(body, 0, match.start())), default=0)
            context_start = max(page_start, match.start() - 2800)
            context = body[context_start:match.start()]
            positions = [i for i, char in enumerate(context) if not char.isspace()]
            compact = ''.join(context[i] for i in positions).replace('（', '(').replace('）', ')')
            four = originals._Q3_COLUMNS.search(compact) if end == '09-30' else None
            six = _SIX_COLUMNS.search(compact) if end == '09-30' else None
            bank = re.search('(?:' + bank_columns + '|' + duration_columns + ')', compact) if end == '09-30' else None
            two = _TWO_COLUMNS.search(compact)
            if two and end == '09-30' and not two['ytd']:
                two = None
            header = six or four or bank or two
            if not header:
                continue
            header_start = context_start + positions[header.start()]
            units = list(_UNIT.finditer(context))
            # A heading on the immediately preceding page can belong to a
            # split summary table only when this page has its complete header.
            if not units and (four or six or bank):
                previous = body[max(0, page_start - 350):page_start]
                candidates = list(_UNIT.finditer(previous))
                if candidates and not re.search(r'利润表|现金流量表|资产负债表', previous[candidates[-1].end():]):
                    units = candidates
            tail_start = match.end()
            tail = body[tail_start:tail_start + 240]
            row_unit = re.match(r'\s*[（(]\s*(?:人民币\s*)?(亿元|百万元|万元|千元|元)\s*[)）]', tail)
            unit = row_unit[1] if row_unit else units[-1][1] if units else None
            if unit is None:
                continue
            if row_unit:
                tail_start += row_unit.end()
                tail = body[tail_start:tail_start + 240]
            cells = _cells(tail)
            columns = []
            if six:
                adjusted = compact[six.end():].startswith('调整前调整后调整后调整前调整后调整后')
                expected = 8 if adjusted else 6
                amount_indices = (0, 1, 2, 4, 5, 6) if adjusted else (0, 1, 3, 4)
                if len(cells) == expected and all(not cells[i]['percent'] for i in amount_indices):
                    columns = [(4 if adjusted else 3, current_period), (6 if adjusted else 4, previous_period)]
            elif four or bank:
                if len(cells) == 4 and not any(cells[i]['percent'] for i in (0, 2)):
                    columns = [(2, current_period)]
            elif two:
                after_header = compact[two.end():]
                another_table = re.search(r'(?:母公司|合并).{0,24}(?:现金流量表|利润表|资产负债表)|项目本期发生额上期发生额', after_header)
                if not another_table and len(cells) == 2 and not any(item['percent'] for item in cells):
                    columns = [(0, current_period), (1, previous_period)]
            for index, observed_period in columns:
                amount = _amount(cells[index], unit)
                if amount is not None:
                    result.append({'metric': metric, 'period': observed_period, 'basis': 'cumulative',
                                   'amount_yuan': amount, 'document': fact['id'],
                                   'anchor': originals._anchor(fact, header_start, tail_start + cells[index].end(), label)})
            if columns:
                break
    return result


def records_from_filing(fact):
    published = str(fact.get('published') or fact.get('day') or '')[:10]
    if not published or not fact.get('text') or not fact.get('id'):
        return []
    date.fromisoformat(published)
    parsing_fact, period_anchor = _parsing_fact(fact)
    if parsing_fact is None:
        return []
    grouped = defaultdict(dict)
    rows = [*originals._financial_rows(parsing_fact), *_summary_rows(parsing_fact)]
    for row in rows:
        if row['metric'] not in METRICS or row['basis'] != 'cumulative' or row['period'] > published:
            continue
        anchor_start = row['anchor']['start']
        page_start = max((m.end() for m in originals.PAGE.finditer(fact['text'], 0, anchor_start)), default=0)
        currency = re.findall(r'币种\s*[:：]\s*([^\s，,；;]+)', fact['text'][page_start:anchor_start])
        if currency and currency[-1] not in ('人民币', 'CNY', 'RMB'):
            continue
        metric = row['metric']
        saved = grouped[row['period']].get(metric)
        if saved and originals.Decimal(saved['amount_yuan']) != originals.Decimal(row['amount_yuan']):
            # Contradictory extracted amounts need review, not first-match selection.
            grouped[row['period']][metric] = None
        elif metric not in grouped[row['period']]:
            grouped[row['period']][metric] = row
    records = []
    for period, metrics in sorted(grouped.items()):
        fields = {'period_basis': 'cumulative', 'currency': 'CNY', 'filing_fact_id': fact['id'],
                  'filing_url': fact.get('url') or '', 'document_sha256': fact.get('document_sha256') or '',
                  'filing_title': fact.get('title') or '', 'filing_published': fact.get('published') or published,
                  'parser_version': VERSION}
        if period_anchor:
            fields.update({'report_period_basis': 'original_heading',
                           'report_period_anchor_id': period_anchor['id'],
                           'report_period_start': period_anchor['start'], 'report_period_end': period_anchor['end'],
                           'report_period_anchor_sha256': period_anchor['sha256'],
                           'report_period_location': period_anchor['location']})
        for metric, row in metrics.items():
            if row is None:
                continue
            anchor = row['anchor']
            fields.update({metric: row['amount_yuan'], metric + '_anchor_id': anchor['id'],
                           metric + '_start': anchor['start'], metric + '_end': anchor['end'],
                           metric + '_anchor_sha256': anchor['sha256'], metric + '_location': anchor['location']})
        if not any(metric in fields for metric in METRICS):
            continue
        records.append({'day': published, 'symbol': fact['symbol'],
                        'key': f'filing:{period}:cumulative:{fact["id"]}', 'report_type': 'filing',
                        'report_date': period, 'published': published, 'fields': fields})
    return records


def collect(params):
    start, end = assets.day(params['start']), assets.day(params['end'])
    if start > end:
        raise ValueError('财务原文日期范围无效')
    requested = params.get('symbols') or ([params['symbol']] if params.get('symbol') else [])
    symbols = list(dict.fromkeys(assets.symbol(value) for value in requested))
    knowledge = params.get('knowledge_cutoff') or store.now()
    scope = ' AND h.symbol IN (' + ','.join(['%s'] * len(symbols)) + ')' if symbols else ''
    with store.db(readonly=True) as conn:
        rows = conn.execute(
            "SELECT f.id,h.day,h.symbol FROM heads h JOIN facts f ON f.id=h.fact_id "
            "JOIN announcements a ON a.fact_id=f.id WHERE h.dataset='announcements' "
            "AND h.day BETWEEN %s AND %s AND f.first_seen<=%s AND f.source='cninfo_text' "
            "AND a.text IS NOT NULL AND a.title REGEXP '年度报告|半年度报告|季度报告|财务报告|业绩快报' "
            + scope + ' ORDER BY h.day,h.symbol,f.id LIMIT 2001', (start, end, knowledge, *symbols),
        ).fetchall()
        if len(rows) > 2000:
            raise ValueError('财务原文超过单批2000份，请按股票或日期分批获取')
        result = []
        for row in record_store.hydrate(conn, rows):
            payload = row['payload']
            if payload.get('published') and len(payload['published']) > 10 and payload['published'] > knowledge:
                continue
            result.extend(records_from_filing({**payload, 'id': row['id'], 'symbol': row['symbol'], 'day': row['day']}))
    return result
