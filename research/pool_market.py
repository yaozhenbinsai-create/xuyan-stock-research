"""Price context for research cards, derived only from saved market facts."""
from __future__ import annotations

import math
from statistics import fmean

from foundation import record_store
from research import market, outlook


def _price(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def latest_close_day(conn, now):
    return conn.execute(
        "SELECT MAX(day) FROM heads WHERE dataset='daily' AND day<=%s" if now[11:16]>='15:00'
        else "SELECT MAX(day) FROM heads WHERE dataset='daily' AND day<%s", (now[:10],),
    ).fetchone()[0]


def quote(conn, symbol, now):
    cutoff = '<=' if now[11:16]>='15:00' else '<'
    rows = conn.execute(
        "SELECT h.day,f.id FROM heads h JOIN facts f ON f.id=h.fact_id "
        f"WHERE h.dataset='daily' AND h.symbol=%s AND h.day{cutoff}%s ORDER BY h.day DESC,h.record_key LIMIT 20",
        (symbol, now[:10]),
    ).fetchall()
    daily = [{**row['payload'], 'day':row['day'], 'fact_id':row['id']}
             for row in reversed(record_store.hydrate(conn, rows))]
    last = daily[-1] if daily else None
    saved = ({'price':last['close'], 'at':last['day']+'T15:00:00+08:00', 'kind':'close',
              'fact_id':last['fact_id'], 'preclose':last.get('preclose'), 'suspended':last.get('suspended',False)}
             if last and _price(last.get('close')) else {})
    rows = conn.execute(
        "SELECT f.id FROM heads h JOIN facts f ON f.id=h.fact_id "
        "WHERE h.dataset='quotes' AND h.symbol=%s AND h.day BETWEEN %s AND %s "
        "ORDER BY h.day DESC,h.record_key DESC LIMIT 24", (symbol, last['day'] if last else now[:10], now[:10]),
    ).fetchall()
    for row in record_store.hydrate(conn, rows):
        q = row['payload']
        at = q.get('source_time') or ''
        if saved.get('at', '') < at <= now and _price(q.get('close')) and (q.get('volume') or 0)>0:
            saved = {'price':q['close'], 'at':at, 'kind':'quote','fact_id':row['id'],'preclose':q.get('preclose')}
    valid = (len(daily)==20 and len({bar['day'] for bar in daily})==20
             and all(_price(bar.get('close')) for bar in daily)
             and all(_price(b.get('preclose')) and abs(b['preclose']/a['close']-1)<=.001 for a,b in zip(daily,daily[1:])))
    if valid:
        action = conn.execute("SELECT 1 FROM heads WHERE dataset='actions' AND symbol=%s AND day>%s AND day<=%s "
                              "AND record_key NOT LIKE 'coverage:%%' LIMIT 1", (symbol,daily[0]['day'],daily[-1]['day'])).fetchone()
        valid = not action
    previous = saved.pop('preclose',None)
    return {**saved, 'change':saved['price']/previous-1 if saved and _price(previous) else None,
            'ma20':round(fmean(bar['close'] for bar in daily),4) if valid else None,
            'ma_day':last['day'] if last else None,
            'trend':[[bar['day'],bar['close']] for bar in daily] if valid else []}


def scenario(conn, symbol, candidate, decision):
    if not candidate:
        return {}
    metrics = candidate['metrics']
    signal = metrics.get('rise') or {}
    decision = decision or {}
    entry = decision.get('original_entry') or decision.get('entry') or {}
    view = decision.get('outlook') or {}
    up = view.get('upside') or {}
    reference = candidate.get('outlook_reference')
    if not reference:
        bars = market.frozen_bars(symbol,candidate.get('bar_ids',candidate['evidence_ids']),65,conn)
        days = outlook.trading_dates(conn,bars[0]['day'],bars[-1]['day']) if bars else []
        reference = outlook.reference(bars,days,metrics,candidate.get('structure_basis',{}).get('action_days',()))
    horizon = up.get('horizon') or next(iter(reference['horizons']),'D1')
    band = reference['horizons'].get(horizon,{}).get('upside')
    floor = signal.get('invalidation_price') or entry.get('invalidate_below')
    return {'anchor':reference['reference_price'],'as_of':reference['as_of'],'trigger':signal.get('trigger_above'),
            'floor':floor,'floor_kind':'收盘失效' if signal.get('invalidation_price') else '原参与失效',
            'target':{**band,'horizon':horizon,'kind':'条件上行区间' if up.get('use_history') else '历史上行参考',
                      'condition':up.get('condition') if up.get('use_history') else None} if band else None}
