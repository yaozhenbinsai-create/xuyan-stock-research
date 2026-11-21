"""Point-in-time price observations. An intraday quote is a partial bar."""
from datetime import datetime, timedelta

from fastapi import HTTPException

from foundation import database as store, record_store


def _sql_time(stamp):
    """Typed timestamps use the instance's Shanghai wall clock."""
    value = datetime.fromisoformat(stamp)
    return value.astimezone(store.SHANGHAI).replace(tzinfo=None) if value.tzinfo else value


def latest_quotes(conn, day, cutoff, knowledge, age_seconds, symbols=None):
    earliest = (datetime.fromisoformat(cutoff) - timedelta(seconds=age_seconds)).isoformat()
    stamp = 'q.source_time'
    scope = ' AND f.symbol IN (' + ','.join(['%s'] * len(symbols)) + ')' if symbols else ''
    rows = conn.execute(
        f"SELECT symbol,id,first_seen FROM (SELECT f.symbol,f.id,f.first_seen,ROW_NUMBER() OVER "
        f"(PARTITION BY f.symbol ORDER BY {stamp} DESC,f.first_seen DESC,f.id DESC) n "
        f"FROM facts f JOIN quote_snapshots q ON q.fact_id=f.id WHERE f.dataset='quotes' AND f.day=%s AND f.first_seen<=%s "
        f"AND {stamp} BETWEEN %s AND %s{scope}) ranked WHERE n=1 ORDER BY symbol LIMIT 10001",
        (day, knowledge, _sql_time(earliest), _sql_time(cutoff), *(symbols or [])),
    ).fetchall()
    if len(rows) > 10000:
        raise HTTPException(409, '行情快照超出本轮可核对范围')
    result = {}
    for row in record_store.hydrate(conn, rows):
        quote = row['payload']
        prices = [quote.get(k) for k in ('open', 'high', 'low', 'close', 'preclose')]
        if not all(isinstance(p, (int, float)) and p > 0 for p in prices):
            continue
        opening, high, low, close, _ = prices
        if not low <= min(opening, close) <= max(opening, close) <= high:
            continue
        result[row['symbol']] = {**quote, '_id': row['id'], '_known_at': row['first_seen'], 'partial_bar': True}
    return result


def comparable_volume(conn, quotes, prior_days, cutoff, knowledge, age_seconds):
    """Cumulative volume only compares with historical quotes from the same clock minute."""
    from collections import defaultdict

    volumes = defaultdict(list)
    for day in prior_days[:6]:
        historical = latest_quotes(conn, day, day + cutoff[10:], knowledge, age_seconds)
        for symbol, old in historical.items():
            current = quotes.get(symbol)
            if not current or old.get('volume_unit') != 'shares' or not (old.get('volume') or 0) > 0:
                continue
            present = datetime.fromisoformat(current['source_time'])
            prior = datetime.fromisoformat(old['source_time']).replace(year=present.year, month=present.month, day=present.day)
            if abs((present - prior).total_seconds()) <= 60:
                volumes[symbol].append((old['volume'], old['_id']))
    return volumes


def frozen_bars(symbol, ids, limit, conn=None):
    if not ids or len(ids) > 120:
        return []
    if conn is None:
        with store.db(readonly=True) as conn:
            return frozen_bars(symbol, ids, limit, conn)
    fields = ('day', 'open', 'high', 'low', 'close', 'preclose', 'volume', 'volume_unit', 'suspended', 'source_time')
    rows = conn.execute(
        "SELECT id,dataset,first_seen FROM facts WHERE dataset IN ('daily','quotes') AND symbol=%s AND id IN ("
        + ','.join(['%s'] * len(ids)) + ") ORDER BY day DESC,id DESC LIMIT %s", (symbol, *ids, limit),
    ).fetchall()
    return [
        {'id': row['id'], 'partial_bar': row['dataset'] == 'quotes', 'first_seen': row['first_seen'],
         **{k: v for k, v in row['payload'].items() if k in fields}}
        for row in reversed(record_store.hydrate(conn, rows))
    ]


def structure_basis(conn, symbol, first_day, last_day, knowledge, check):
    # Read the coverage receipt that actually spans these bars, and freeze the event days.
    rows = conn.execute(
        "SELECT f.id,f.day,f.first_seen FROM heads h JOIN facts f ON f.id=h.fact_id "
        "WHERE h.dataset='actions' AND h.symbol=%s AND h.day BETWEEN %s AND %s AND f.first_seen<=%s "
        "ORDER BY h.day DESC,f.id DESC LIMIT 129",
        (symbol, first_day, knowledge[:10], knowledge),
    ).fetchall()
    if len(rows) > 128:
        return {'actions_verified': False}
    receipt = None
    events = []
    for row in record_store.hydrate(conn, rows):
        payload = row['payload']
        if payload.get('record_kind') == 'coverage':
            if (payload.get('coverage_from', '9999') <= first_day
                    and payload.get('coverage_through', '') >= last_day and receipt is None):
                receipt = row
        elif row['day'] <= last_day:
            events.append(row)
    verified = bool(receipt and check.get('state') == 'verified')
    return {'actions_verified': verified, 'action_days': sorted({r['day'] for r in events}),
            'action_available': {r['day']:r['first_seen'] for r in reversed(events)},
            'actions_known_at': max(receipt['first_seen'], check['confirmed_at']) if verified else None,
            'action_ids': [r['id'] for r in events] + ([receipt['id']] if verified else [])}


def quote_near(conn, symbol, anchor, *, since=None, seconds=60):
    """Use the last observation at or before the predeclared timestamp."""
    earlier=(datetime.fromisoformat(anchor)-timedelta(seconds=seconds)).isoformat()
    if since:
        earlier=max(earlier,since)
    stamp='q.source_time'
    rows=conn.execute(f"SELECT f.id FROM facts f JOIN quote_snapshots q ON q.fact_id=f.id "
                      f"WHERE f.dataset='quotes' AND f.day=%s AND f.symbol=%s "
                      f"AND {stamp} BETWEEN %s AND %s AND f.first_seen<=%s ORDER BY {stamp} DESC,f.first_seen,f.id LIMIT 8",
                      (anchor[:10],symbol,_sql_time(earlier),_sql_time(anchor),store.now())).fetchall()
    for row in record_store.hydrate(conn, rows):
        payload=row['payload']
        at=payload['source_time'];clock=at[11:19]
        if ('09:30:00'<=clock<'11:30:00' or '13:00:00'<=clock<'14:57:00') and (payload.get('close') or 0)>0 and (payload.get('volume') or 0)>0:
            return {'symbol':symbol,'fact_id':row['id'],'at':at,'price':payload['close']}
    return None


def closing_bar(conn, symbol, day):
    if not day:return None
    row=conn.execute("SELECT f.id FROM heads h JOIN facts f ON f.id=h.fact_id WHERE h.dataset='daily' "
                     "AND h.symbol=%s AND h.day=%s ORDER BY h.record_key LIMIT 1",(symbol,day)).fetchone()
    return {**record_store.load(conn,row['id']),'fact_id':row['id']} if row else None


def comparable_window(conn, symbol, reference, endpoint):
    start=reference['at'][:10]
    if start>endpoint:return False
    if start==endpoint:return True
    action=conn.execute("SELECT 1 FROM heads WHERE dataset='actions' AND symbol=%s AND day>%s AND day<=%s AND record_key NOT LIKE 'coverage:%%' LIMIT 1",
                        (symbol,start,endpoint)).fetchone()
    if action:return False
    rows=conn.execute("SELECT f.id,h.day FROM heads h JOIN facts f ON f.id=h.fact_id WHERE h.dataset='daily' AND h.symbol=%s AND h.day BETWEEN %s AND %s ORDER BY h.day LIMIT 10",
                      (symbol,start,endpoint)).fetchall()
    calendar=conn.execute("SELECT h.day,cal.trading FROM heads h JOIN calendar_records cal ON cal.fact_id=h.fact_id WHERE h.dataset='calendar' AND h.day BETWEEN %s AND %s ORDER BY h.day LIMIT 31",
                          (start,endpoint)).fetchall()
    days=[r['day'] for r in calendar if r['trading'] == 1]
    if not days or [r['day'] for r in rows]!=days:return False
    bars=[r['payload'] for r in record_store.hydrate(conn,rows)]
    return all(a.get('close') and b.get('preclose') and not b.get('suspended') and abs(b['preclose']/a['close']-1)<=.001 for a,b in zip(bars,bars[1:]))
