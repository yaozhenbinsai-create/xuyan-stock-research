"""Freeze coverage membership compactly and serve only the requested page."""
from __future__ import annotations

import base64
from collections import Counter, defaultdict
import hashlib
import json
import struct
import zlib

from fastapi import HTTPException
from foundation import database as store
from research import stock_library


LABELS = {'covered':'按偏好筛选后可研究','missing_identity':'证券身份缺失','inactive':'名单标记为非交易',
          'missing_daily':'缺当日或前序日线','suspended':'停牌','risk_warning':'风险警示',
          'missing_quote':'缺截点快照','insufficient_history':'有效历史不足','outside_preferences':'超出本轮参与范围'}
PREFIXES = {'sh':'H','sz':'S','bj':'B'}
MARKETS = {v:k for k,v in PREFIXES.items()}
WIDTH = 7
TABLES = ('research_scope_sets','research_scope_members','research_candidates')


def apply_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS research_scope_sets(
      run_id CHAR(16) CHARACTER SET ascii PRIMARY KEY, fingerprint BINARY(32) NOT NULL,
      CONSTRAINT scope_set_run FOREIGN KEY(run_id) REFERENCES research_runs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB""")
    conn.execute("""CREATE TABLE IF NOT EXISTS research_scope_members(
      run_id CHAR(16) CHARACTER SET ascii NOT NULL, state VARCHAR(32) CHARACTER SET ascii NOT NULL,
      sequence_no INT UNSIGNED NOT NULL, symbol VARCHAR(12) CHARACTER SET ascii NOT NULL,
      PRIMARY KEY(run_id,state,sequence_no), UNIQUE KEY scope_symbol(run_id,symbol),
      CONSTRAINT scope_member_run FOREIGN KEY(run_id) REFERENCES research_runs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB""")
    conn.execute("""CREATE TABLE IF NOT EXISTS research_candidates(
      run_id CHAR(16) CHARACTER SET ascii NOT NULL, sequence_no INT UNSIGNED NOT NULL,
      symbol VARCHAR(12) CHARACTER SET ascii NOT NULL,
      PRIMARY KEY(run_id,sequence_no), UNIQUE KEY candidate_symbol(run_id,symbol),
      CONSTRAINT candidate_run FOREIGN KEY(run_id) REFERENCES research_runs(id) ON DELETE CASCADE
    ) ENGINE=InnoDB""")


def persist(conn, run_id, data):
    """Save actual coverage memberships and candidates when a bundle is frozen.

    The signed input remains an original document; SQL membership queries use
    these ordinary rows and never decompress the original document in a query.
    """
    bundle=data.get('bundle') or {}
    packed=bundle.get('scope_pack')
    if isinstance(packed,dict):
        fingerprint=hashlib.sha256(store.dumps(packed).encode()).digest()
        saved=conn.execute('SELECT fingerprint FROM research_scope_sets WHERE run_id=%s',(run_id,)).fetchone()
        if not saved or bytes(saved[0])!=fingerprint:
            members=[]
            for state,value in packed.items():
                if state not in LABELS:
                    raise ValueError('冻结覆盖分类未知')
                compressed=base64.b64decode(value,validate=True)
                raw=zlib.decompress(compressed[4:])
                if len(compressed)<4 or len(raw)!=struct.unpack('<I',compressed[:4])[0] or len(raw)%WIDTH:
                    raise ValueError('冻结覆盖成员长度不一致')
                for position in range(0,len(raw),WIDTH):
                    code=raw[position:position+WIDTH].decode('ascii')
                    if code[0] not in MARKETS or not code[1:].isdigit():
                        raise ValueError('冻结覆盖证券代码无效')
                    members.append((run_id,state,position//WIDTH,MARKETS[code[0]]+'.'+code[1:]))
            conn.execute('DELETE FROM research_scope_members WHERE run_id=%s',(run_id,))
            if members:
                conn.executemany('INSERT INTO research_scope_members(run_id,state,sequence_no,symbol) VALUES(%s,%s,%s,%s)',members)
            conn.execute('INSERT INTO research_scope_sets(run_id,fingerprint) VALUES(%s,%s) '
                         'ON DUPLICATE KEY UPDATE fingerprint=VALUES(fingerprint)',(run_id,fingerprint))
    if isinstance(bundle.get('candidates'),list):
        candidates=[(run_id,i,item['symbol']) for i,item in enumerate(bundle['candidates'])]
        conn.execute('DELETE FROM research_candidates WHERE run_id=%s',(run_id,))
        if candidates:
            conn.executemany('INSERT INTO research_candidates(run_id,sequence_no,symbol) VALUES(%s,%s,%s)',candidates)


def _state(symbol, directory, bars, quotes, metrics, allowed, intraday):
    member = directory.get(symbol)
    bar = bars.get(symbol)
    if not member:
        return 'missing_identity'
    if member.get('status') != '1':
        return 'inactive'
    if not bar:
        return 'missing_daily'
    if bar.get('suspended'):
        return 'suspended'
    if bar.get('is_st'):
        return 'risk_warning'
    if intraday and (symbol not in quotes or not (quotes[symbol].get('volume') or 0)>0):
        return 'missing_quote'
    if symbol not in metrics:
        return 'insufficient_history'
    return 'covered' if symbol in allowed else 'outside_preferences'


def freeze(scope, directory, bars, quotes, metrics, allowed, intraday, documents, capped):
    groups = defaultdict(list)
    counts = defaultdict(Counter)
    for symbol in sorted(scope):
        market, code = symbol.split('.')
        state = _state(symbol,directory,bars,quotes,metrics,allowed,intraday)
        groups[state].append(PREFIXES[market]+code)
        counts[market][state] += 1
    hash_input = []
    packed = {}
    for state, codes in sorted(groups.items()):
        raw = ''.join(codes).encode('ascii')
        hash_input.append((state,raw.decode('ascii')))
        packed[state] = base64.b64encode(struct.pack('<I',len(raw))+zlib.compress(raw)).decode('ascii')
    eligible = {symbol for symbol in scope if symbol in allowed}
    domains = {}
    for name in ('announcements','financials','fund_flows','actions'):
        present = sum(bool(documents.get(symbol,{}).get(name)) for symbol in eligible)
        domains[name] = {'present':present,'denominator':len(eligible),'capped':name in capped}
    excluded=sum(len(groups.get(k,[])) for k in ('inactive','suspended','risk_warning','outside_preferences'))
    missing={k:len(groups.get(k,[])) for k in ('missing_identity','missing_daily','missing_quote','insufficient_history')}
    return ({'states':[{'id':k,'name':v,'count':len(groups.get(k,[]))} for k,v in LABELS.items()],
             'readiness':{'scope':len(scope),'eligible':len(groups.get('covered',[])),
                          'policy_excluded':excluded,'data_gaps':missing,
                          'basis':'风险排除与真实缺资料分列，行情覆盖不代表财务或风险核验完成'},
             'markets':{k:dict(v) for k,v in counts.items()},'domains':domains,
             'scope_hash':hashlib.sha256(store.dumps(hash_input).encode()).hexdigest()},packed)


def page(run_id, state, limit, offset, blocked=()):
    if state not in LABELS:
        raise HTTPException(422,'研究覆盖分类无效')
    with store.db(readonly=True) as conn:
        if not conn.execute('SELECT id FROM research_runs WHERE id=%s',(run_id,)).fetchone():
            raise HTTPException(404,'该研究不存在')
        available=bool(conn.execute('SELECT run_id FROM research_scope_sets WHERE run_id=%s',(run_id,)).fetchone())
        rows=conn.execute('SELECT symbol FROM research_scope_members WHERE run_id=%s AND state=%s '
                          'ORDER BY sequence_no LIMIT %s OFFSET %s',(run_id,state,limit+1,offset)).fetchall()
        symbols=[r[0] for r in rows[:limit]]
        hidden = stock_library.retired(conn,symbols) | set(blocked)
    return {'available':available, 'state':state,'label':LABELS[state],
            'items':[{'symbol':symbol} for symbol in symbols if symbol not in hidden],
            'next':offset+limit if len(rows)>limit else None}


def status_for(conn, run_id, symbol):
    row=conn.execute('SELECT state FROM research_scope_members WHERE run_id=%s AND symbol=%s LIMIT 1',
                     (run_id,symbol)).fetchone()
    return row[0] if row else None
