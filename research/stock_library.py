"""Read the current pools and their research history from the existing ledger."""
from __future__ import annotations

import json

from fastapi import HTTPException

from foundation import assets, database as store
from research import pool_market


VERSION = 'stock-library-v1'
STATES = ('buy','wait','active','evidence','removed','expired','withdrawn','archive')
ACTIVE_SQL = "('waiting','active','observe','needs_evidence')"
CYCLE_SESSION_SQL = "c.session"
ADMITTED = "('select','conditional','watch')"
BUY_SQL = ("((c.state='active' AND c.formal=1) OR (c.state='waiting' AND "
           "c.action='select'))")
WITHDRAWN_SQL = """(r.withdrawn_at IS NOT NULL OR EXISTS (
    SELECT 1 FROM research_notices n WHERE n.run_id=r.id AND n.kind='correction'
    AND n.action='withdraw'
    AND n.symbol=d.symbol))"""
_DECISIONS = """
    SELECT r.id run_id,r.market_day,r.created,r.kind,d.symbol,d.name,d.action,d.cycle_id,d.review_event,
           d.rise_type,d.rise_name,d.stars,
           r.session,
           DATE_FORMAT(r.published_at,'%%Y-%%m-%%dT%%H:%%i:%%s+08:00') published_at,
           c.market_day original_day,c.created original_published_at,
           """+CYCLE_SESSION_SQL+""" original_session,
           c.pool_review_day checked_day,
           c.pool_review_reason check_reason,
           CASE
             WHEN """+WITHDRAWN_SQL+""" THEN 'withdrawn'
             WHEN c.state IN ('removed','expired','active') THEN c.state
             WHEN c.state='needs_evidence' THEN 'evidence'
             WHEN c.id IS NULL THEN 'archive'
             WHEN """+BUY_SQL+""" THEN 'buy'
             ELSE 'wait'
           END state,
           CASE WHEN """+BUY_SQL+""" THEN 'buy' WHEN c.state IN """+ACTIVE_SQL+""" THEN 'watch' END cycle_pool
      FROM research_runs r
      JOIN research_decisions d ON d.run_id=r.id
      LEFT JOIN research_cycles c ON c.id=d.cycle_id
     WHERE r.report IS NOT NULL AND (
        d.action IN """+ADMITTED+"""
        OR (d.review_event=1 AND c.action IN """+ADMITTED+""")
     )
"""
_MEMBERS = ("SELECT c.symbol,IF(MAX("+BUY_SQL+"),'buy','watch') pool FROM research_cycles c "
            "WHERE c.state IN "+ACTIVE_SQL)
_ORDER = ('market_day','created','run_id','symbol')
_FIELDS = ('run_id','market_day','created','session','published_at','kind','symbol','name','action','cycle_id',
           'original_day','original_published_at','original_session','rise_type','rise_name','stars',
           'state','pool','checked_day','check_reason')


def retired_sql(symbol):
    return ("EXISTS (SELECT 1 FROM research_cycles gone WHERE gone.symbol="+symbol+" AND gone.state='removed') "
            "AND NOT EXISTS (SELECT 1 FROM research_cycles live WHERE live.symbol="+symbol+" AND live.state IN "+ACTIVE_SQL+")")


def retired(conn, symbols):
    if not symbols:
        return set()
    placeholders = ','.join(['%s']*len(symbols))
    return {row[0] for row in conn.execute(
        "SELECT DISTINCT h.symbol FROM research_cycles h WHERE h.symbol IN ("+placeholders+") AND "+retired_sql('h.symbol'),
        tuple(symbols),
    )}


def visible(run, conn):
    candidates = run['data'].get('bundle',{}).get('candidates',[])
    symbols = {item['symbol'] for item in candidates}
    if run['data'].get('withdrawn_at'):
        return set()
    hidden = retired(conn,symbols)
    hidden.update(item['symbol'] for item in run.get('corrections',[]) if item['action']=='withdraw')
    decisions = (run.get('report') or {}).get('decisions',[])
    ids = [item['cycle_id'] for item in decisions if item.get('cycle_id')]
    if ids:
        rows = conn.execute("SELECT id FROM research_cycles WHERE state='removed' AND id IN ("+
                            ','.join(['%s']*len(ids))+")",tuple(ids)).fetchall()
        closed = {row[0] for row in rows}
        hidden.update(item['symbol'] for item in decisions if item.get('cycle_id') in closed)
    return symbols-hidden


def require_visible(run, symbol, conn):
    if symbol not in visible(run,conn):
        raise HTTPException(404,'股票已退出展示，或不在本轮研究范围')


def _counts(conn, symbols):
    if not symbols:
        return {}
    query = _DECISIONS+' AND d.symbol IN ('+','.join(['%s']*len(symbols))+')'
    rows = conn.execute("WITH decisions AS ("+query+") "
        "SELECT symbol,COUNT(DISTINCT run_id) selected_count,"
        "COUNT(DISTINCT CASE WHEN action='select' THEN run_id END) recommended_count "
        "FROM decisions WHERE state!='withdrawn' AND kind!='review' AND COALESCE(review_event,0)=0 GROUP BY symbol",tuple(symbols))
    return {row['symbol']:{key:row[key] for key in ('selected_count','recommended_count')} for row in rows}


def _card(conn, symbol, candidate, decision, quote, counts):
    return {'quote':quote,'levels':pool_market.scenario(conn,symbol,candidate,decision),
            'counts':counts or {'selected_count':0,'recommended_count':0}}


def presentation(run, *, panels=True):
    if not run:
        return None
    with store.db(readonly=True,isolation='REPEATABLE READ') as conn:
        shown = visible(run,conn)
        candidates = run['data'].get('bundle',{}).get('candidates',[])
        run['hidden_stocks'] = [{key:item[key] for key in ('symbol','name')} for item in candidates if item['symbol'] not in shown]
        if panels and run.get('report'):
            counts = _counts(conn,shown)
            decisions = {d['symbol']:d for d in run['report']['decisions']}
            now = store.now()
            run['price_cards'] = {s:_card(conn,s,item,decisions.get(s),pool_market.quote(conn,s,now),counts.get(s))
                                  for item in candidates if (s := item['symbol']) in shown}
    return run


def _member_scope(session):
    return _MEMBERS+(' AND '+CYCLE_SESSION_SQL+'=%s' if session else '')+' GROUP BY c.symbol'


def pool_counts(conn,session=None):
    rows = conn.execute("SELECT pool,COUNT(*) n FROM ("+_member_scope(session)+") m GROUP BY pool",(session,) if session else ())
    return {'buy':0,'watch':0,**{row['pool']:row['n'] for row in rows}}


def _enrich(conn, rows):
    if not rows:
        return []
    ids = list(dict.fromkeys(row['run_id'] for row in rows))
    report_rows = conn.execute("SELECT id,data,report FROM research_runs "
                               "WHERE id IN ("+','.join(['%s']*len(ids))+")", tuple(ids))
    contents = {row['id']:({c['symbol']:c for c in (json.loads(row['data']).get('bundle') or {}).get('candidates',[])},
                          {d['symbol']:d for d in json.loads(row['report'])['decisions']}) for row in report_rows}
    symbols = {row['symbol'] for row in rows}
    counts = _counts(conn,symbols)
    now = store.now()
    quotes = {symbol:pool_market.quote(conn,symbol,now) for symbol in symbols}
    result = []
    for row in rows:
        symbol = row['symbol']
        candidates, decisions = contents[row['run_id']]
        decision = decisions.get(symbol)
        result.append({**dict(row),'card':_card(conn,symbol,candidates.get(symbol),decision,quotes[symbol],counts.get(symbol)),
                       'thesis':(decision or {}).get('thesis')})
    return result


def page(limit, before=None, *, after=None, pool=None, start=None, end=None, state=None, stars=None, rise_type=None,
         query='', symbol=None, session=None, cycle_session=None, run_id=None):
    if start and end and start>end:
        raise HTTPException(422,'开始日期须早于或等于结束日期')
    if before and after:
        raise HTTPException(422,'请选择一个翻页方向')
    window, params = [], []
    if run_id:
        window.append('r.id=%s')
        params.append(run_id)
    for value, clause in ((start,'r.market_day>=%s'),(end,'r.market_day<=%s')):
        if value:
            window.append(clause)
            params.append(assets.day(value))
    if symbol:
        window.append('d.symbol=%s')
        params.append(assets.symbol(symbol))
    if rise_type:
        window.append('d.rise_type=%s')
        params.append(rise_type)
    if query.strip():
        name = query.strip().lower()
        window.append("(LOCATE(%s,LOWER(d.name))>0 OR LOCATE(%s,CONVERT(REPLACE(d.symbol,'.','') USING utf8mb4))>0)")
        params.extend((name,name.replace('.','') or name))
    if cycle_session:
        window.append(CYCLE_SESSION_SQL+'=%s')
        params.extend((cycle_session,cycle_session))
    filters = ["d.state NOT IN ('removed','withdrawn')"]
    if symbol:
        filters.append("NOT ("+retired_sql('d.symbol')+")")
    else:
        filters.append('m.pool=d.cycle_pool')
    for value, clause in ((session,'d.session=%s'),(state,'d.state=%s'),(pool,'m.pool=%s')):
        if value:
            filters.append(clause)
            params.append(value)
    if stars=='unrated':
        filters.append('d.stars IS NULL')
    elif stars:
        filters.append('d.stars=%s')
        params.append(int(stars))
    order = ','.join(key+' DESC' for key in _ORDER)
    asc = ','.join(_ORDER)
    base = 'WITH decisions AS ('+_DECISIONS+(' AND '+' AND '.join(window) if window else '')+'), members AS ('+_member_scope(cycle_session)+')'
    base += (', ranked AS (SELECT d.*,m.pool,ROW_NUMBER() OVER(PARTITION BY d.symbol ORDER BY '
             'd.market_day DESC,d.created DESC,d.run_id DESC) n FROM decisions d LEFT JOIN members m ON m.symbol=d.symbol WHERE '
             +' AND '.join(filters)+')')
    scope = ' WHERE 1=1' if symbol else ' WHERE n=1'
    with store.db(readonly=True,isolation='REPEATABLE READ') as conn:
        total = conn.execute(base+' SELECT COUNT(*) FROM ranked'+scope,params).fetchone()[0]
        cursor = before or after
        if cursor:
            cursor_run, separator, cursor_symbol = cursor.partition(':')
            if (not separator or len(cursor_run)!=16 or not all(ch in '0123456789abcdef' for ch in cursor_run)
                    or run_id and cursor_run!=run_id):
                raise HTTPException(422,'股票库游标无效')
            cursor_symbol = assets.symbol(cursor_symbol)
            stamp = conn.execute('SELECT market_day,created FROM research_runs WHERE id=%s',(cursor_run,)).fetchone()
            if not stamp:
                raise HTTPException(409,'股票库游标已失效')
            scope += ' AND ('+asc+')'+('>' if after else '<')+'(%s,%s,%s,%s)'
            params.extend((stamp['market_day'],stamp['created'],cursor_run,cursor_symbol))
        rows = conn.execute(base+' SELECT '+','.join(_FIELDS)+' FROM ranked'+scope+
                            ' ORDER BY '+(asc if after else order)+' LIMIT %s',(*params,limit+1)).fetchall()
        more = len(rows)>limit
        rows = rows[:limit]
        if after:
            rows.reverse()
        items = _enrich(conn,rows)
        counts = pool_counts(conn,cycle_session)
    return {'items':items,'total':total,'pools':counts,'limit':limit,
            'next':_cursor(rows[-1]) if rows and (after or more) else None,
            'previous':_cursor(rows[0]) if rows and (more if after else before) else None}


def _cursor(row):
    return f"{row['run_id']}:{row['symbol']}"
