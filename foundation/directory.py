"""Read the market roster without promoting security-detail queries to a census."""
from __future__ import annotations

import json

from fastapi import HTTPException

from foundation import database as store, record_store


def market_roster(conn, day, *, known_at=None, limit=10000):
    known_at = known_at or store.now()
    # Read retained versions, not just mutable heads: an old detail query may
    # already have replaced an empty-key universe head. An origin learned later
    # must not establish an earlier knowledge view of a deduplicated fact.
    universe_known = "CASE WHEN f.source='bs_universe' THEN f.first_seen ELSE o.first_seen END"
    universe_last = "CASE WHEN f.source='bs_universe' THEN f.last_seen ELSE o.last_seen END"
    visible = "(f.source='bs_universe' OR o.source='bs_universe') AND " + universe_known + '<=%s'
    origin_join = " LEFT JOIN fact_origins o ON o.fact_id=f.id AND o.source='bs_universe' "
    latest = conn.execute(
        'SELECT f.day FROM facts f FORCE INDEX (fact_identity)' + origin_join +
        "WHERE f.dataset='basics' AND f.day<=%s AND f.record_key IN ('','universe') AND " + visible +
        ' ORDER BY f.day DESC LIMIT 1',
        (day, known_at),
    ).fetchone()
    roster_day = latest[0] if latest else None
    if not roster_day:
        return None, {}
    rows = conn.execute(
        'SELECT f.symbol,f.id,' + universe_known + ' AS universe_known,'
        'CASE WHEN h.fact_id IS NOT NULL AND f.last_seen<=%s AND ' + universe_last + '<=%s THEN 1 ELSE 0 END AS current_universe '
        'FROM facts f FORCE INDEX (fact_identity)' + origin_join +
        'LEFT JOIN heads h ON h.dataset=f.dataset AND h.day=f.day AND h.symbol=f.symbol '
        'AND h.record_key=f.record_key AND h.fact_id=f.id '
        "WHERE f.dataset='basics' AND f.day=%s AND f.record_key IN ('','universe') AND " + visible +
        ' ORDER BY f.symbol,current_universe,universe_known,f.id LIMIT %s',
        (known_at, known_at, roster_day, known_at, limit * 4 + 1),
    ).fetchall()
    values = record_store.load_many(conn,[r[1] for r in rows])
    roster = {r[0]: {'fact_id': r[1], **values[r[1]]} for r in rows}
    if len(rows) > limit * 4 or len(roster) > limit:
        raise HTTPException(409, '证券基础资料超出本批处理边界，未静默截断')
    return roster_day, roster
