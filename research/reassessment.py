"""Find changed evidence and broken terms of an existing observation cycle."""
from __future__ import annotations

import json
from datetime import datetime

from foundation import database as store, record_store
from research import evidence, outlook, sessions, state_store


ACTIVE = ('waiting', 'active', 'observe', 'needs_evidence')
CRITICAL = frozenset(('price_invalidation', 'structure_invalidation'))


def cited_ids(decision):
    return sorted({fid for group in ('supports', 'counterarguments', 'relations')
                   for item in decision.get(group, []) for fid in item['fact_ids']}
                  | {fid for item in outlook.statements(decision.get('outlook')) for fid in item['fact_ids']}
                  | {fid for key in ('rating','counterparty') for fid in (decision.get(key) or {}).get('fact_ids', [])}
                  | set(decision.get('gate_fact_ids', []))
                  | set((decision.get('rise') or {}).get('facts', [])))


def _dependencies(conn, cycle, data):
    if 'dependency_ids' in data:
        return data['dependency_ids']
    original = conn.execute('SELECT report FROM research_runs WHERE id=%s', (cycle['run_id'],)).fetchone()
    if not original or not original['report']:
        return []
    return sorted({fid for d in json.loads(original['report']).get('decisions', [])
                   if d.get('cycle_id') == cycle['id'] for fid in cited_ids(d)})


def revisions(conn, ids, knowledge):
    if not ids:
        return []
    placeholders = ','.join(['%s'] * len(ids))
    return conn.execute(
        "SELECT old.id old_id,new.id fact_id,new.dataset,new.day,new.first_seen FROM facts old "
        "JOIN heads h ON h.dataset=old.dataset AND h.day=old.day AND h.symbol=old.symbol AND h.record_key=old.record_key "
        f"JOIN facts new ON new.id=h.fact_id WHERE old.id IN ({placeholders}) AND new.id<>old.id "
        "AND new.first_seen<=%s ORDER BY new.first_seen DESC,new.id DESC LIMIT 17", (*ids, knowledge),
    ).fetchall()


def _price_flag(conn, cycle, data, after, cutoff, knowledge, field, threshold):
    if not threshold:
        return None
    deadline = sessions.final_day(data.get('endpoints',{}))
    stop = min(cutoff, deadline + 'T15:00:00+08:00') if deadline else cutoff
    if stop <= after:
        return None
    when = "CONCAT(f.day,'T15:00:00+08:00')" if field == 'daily' else 'q.source_time'
    table = 'daily_bars' if field == 'daily' else 'quote_snapshots'
    volume = "AND q.volume>0" if field == 'quotes' else "AND COALESCE(q.suspended,0)=0"
    observed_column = when if field=='daily' else 'q.source_time'
    after_value,stop_value = ((after,stop) if field=='daily' else
                              tuple(datetime.fromisoformat(value).astimezone(store.SHANGHAI).replace(tzinfo=None)
                                    for value in (after,stop)))
    rows = conn.execute(
        f"SELECT f.id,q.close,{when} observed FROM heads h JOIN facts f ON f.id=h.fact_id JOIN {table} q ON q.fact_id=f.id "
        f"WHERE h.dataset=%s AND h.symbol=%s AND h.day BETWEEN %s AND %s AND {observed_column}>%s AND {observed_column}<=%s "
        f"AND f.first_seen<=%s {volume} AND q.close>0 AND q.close<%s "
        "ORDER BY observed ASC,f.id ASC LIMIT 1",
        (field, cycle['symbol'], after[:10], stop[:10], after_value, stop_value, knowledge, threshold),
    ).fetchone()
    if not rows:
        return None
    price = float(rows['close'])
    return {'fact_id': rows['id'], 'kind': 'structure_invalidation' if field == 'daily' else 'price_invalidation',
            'detail': f"观察价 {price:g} 触及原{'结构' if field == 'daily' else '参与'}失效位 {threshold:g}",
            'observed_at': state_store.iso_time(rows['observed']), 'price': price, 'threshold': threshold}


def flags(conn, cycle, data, *, knowledge=None, cutoff=None):
    knowledge = knowledge or store.now()
    cutoff = cutoff or knowledge
    last = data.get('last_review', {})
    after = last.get('evidence_through') or last.get('at') or data['published_at']
    original = data['published_at']
    reviewed = set(last.get('flag_facts', []))
    result = []
    close_guard = max((data.get('structure_guard') or {}).get('below') or 0,
                      (data.get('rise') or {}).get('invalidation_price') or 0)
    for dataset, threshold in (('quotes', data['entry'].get('invalidate_below')),
                               ('daily', close_guard)):
        flag = _price_flag(conn, cycle, data, after, cutoff, knowledge, dataset, threshold)
        if flag and flag['fact_id'] not in reviewed:
            result.append(flag)
    rows = conn.execute(
        "SELECT f.id,h.day,f.first_seen FROM heads h JOIN facts f ON f.id=h.fact_id JOIN announcements a ON a.fact_id=f.id WHERE h.dataset='announcements' "
        "AND h.symbol=%s AND h.day BETWEEN %s AND %s AND f.first_seen>=%s AND f.first_seen<=%s "
        "AND a.title REGEXP %s "
        "ORDER BY h.day DESC,f.id DESC LIMIT 8",
        (cycle['symbol'], original[:10], knowledge[:10], after, knowledge, evidence.RISK_ALERT.pattern),
    ).fetchall()
    for row in record_store.hydrate(conn, rows):
        payload = row['payload']
        if row['id'] not in reviewed and evidence.visible(row, payload, knowledge, cutoff[:10], False, knowledge):
            result.append({'fact_id': row['id'], 'detail': payload['title'], 'kind': 'risk_notice',
                           'observed_at': row['first_seen']})
    for row in revisions(conn, _dependencies(conn, cycle, data), knowledge):
        if row['fact_id'] not in reviewed:
            result.append({'fact_id': row['fact_id'], 'prior_fact_id': row['old_id'], 'dataset': row['dataset'],
                           'observed_at': row['first_seen'], 'kind': 'fact_revised',
                           'detail': f"原证据 #{row['old_id']} 已更正为 #{row['fact_id']}"})
    return result[:8]


def active_cycles(conn, cutoff, knowledge, symbols=None, *, limit=4, require_alert=False, session=None):
    if not limit:
        return []
    scope = ' AND symbol IN (' + ','.join(['%s'] * len(symbols)) + ')' if symbols else ''
    slot = " AND session=%s" if session else ''
    rows = conn.execute(
        "SELECT * FROM research_cycles WHERE state IN ('waiting','active','observe','needs_evidence') "
        "AND created<%s AND end_day>=%s" + scope + slot +
        " ORDER BY updated,id LIMIT 48", (knowledge, cutoff[:10], *(symbols or []),*((session,) if session else ())),
    ).fetchall()
    targets = []
    for row in state_store.overlay_cycles(conn,rows):
        data = row['data']
        alerts = flags(conn, row, data, cutoff=cutoff, knowledge=knowledge)
        if alerts or not require_alert:
            targets.append({**{key: row[key] for key in ('id', 'symbol', 'state', 'formal')},
                            'data': data, 'flags': alerts})
    targets.sort(key=lambda target: (not any(f['kind'] in CRITICAL for f in target['flags']),
                                     not target['flags']))
    return targets[:limit]


def attach(conn, candidate, cycles):
    alerts = [f for cy in cycles for f in cy['flags']]
    ids = sorted({f['fact_id'] for f in alerts})
    if not ids:
        return
    rows = conn.execute("SELECT id,dataset,symbol FROM facts WHERE id IN ("
                        + ','.join(['%s'] * len(ids)) + ')', ids).fetchall()
    payloads = {row['id']: row['payload'] for row in record_store.hydrate(conn, rows)
                if row['symbol'] in ('*', candidate['symbol'])}
    allowed = ('day','title','source_time','close','published','report_type','report_date','key','record_kind')
    candidate['followup_evidence'] = [{**f, 'facts': {k: payloads[f['fact_id']][k] for k in allowed if k in payloads[f['fact_id']]}}
                                      for f in alerts if f['fact_id'] in payloads]
    candidate['evidence_ids'] = list(dict.fromkeys(candidate['evidence_ids'] + list(payloads)))
