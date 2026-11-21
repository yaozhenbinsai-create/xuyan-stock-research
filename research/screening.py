"""Keep compact screening receipts and query their indexed pages."""
from __future__ import annotations

import hashlib
import json
from itertools import islice

from fastapi import HTTPException
from foundation import assets, database as store
from research import coverage, methods, preferences, stock_library


BITS = {path[0]:1 << index for index,path in enumerate(methods.PATHS)}
# These persisted codes stay fixed if the interface order changes.
TYPE_INDEX = {'strong':1,'setup':2,'early':3,'breakout':4}
INDEX_TYPE = {index:kind for kind,index in TYPE_INDEX.items()}
ROW_FIELDS = ('symbol','name','mask','selected','move','relative_volume')


def _rounded(value):
    return round(value,5) if value is not None else None


def _line(symbol,name,paths,selected,metrics):
    # The low byte keeps the existing path bits. The high byte stores the primary rise type.
    mask = sum(BITS[path] for path in set(paths) if path in BITS)
    mask |= TYPE_INDEX.get(metrics.get('rise',{}).get('type'),0) << 8
    return (symbol,name,mask,int(selected),
            _rounded(metrics.get('return_1')),_rounded(metrics.get('relative_volume20')))


def snapshot_rows(available, selected, directory):
    chosen = {symbol for symbol,_ in selected}
    ordered = [*selected,*[(symbol,metrics) for symbol,metrics in available if symbol not in chosen]]
    return [_line(symbol,directory[symbol].get('name',symbol),metrics['paths'],symbol in chosen,metrics)
            for symbol,metrics in ordered]


def save_rows(conn, run_id, rows):
    if rows:
        conn.executemany(
            'INSERT INTO research_screening(run_id,sequence_no,symbol,name,path_mask,selected,price_move,relative_volume) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
            [(run_id,index,*row) for index,row in enumerate(rows,1)],
        )


def preserve_verified_replay(run):
    """Add reconstructed rows only when the original input fingerprint agrees."""
    from research import discovery

    data = run['data']
    expected = data.get('bundle',{}).get('input_hash')
    if not expected or not run['report'] or not data.get('session_policy'):
        return False
    try:
        profile = {**preferences.context(data),**data.get('intent_constraints',{})}
        if data.get('symbols'):
            profile = {**profile,'candidate_limit':len(set(data['symbols'])),'boards':[],'excluded_symbols':[]}
        replay = discovery.snapshot(run['market_day'],data.get('symbols',[]),slot=data['session_policy'],profile=profile,
                                    method=data.get('method'),review_symbols=tuple(c['symbol'] for c in data.get('original_cycles',[])),
                                    rise_version=data.get('rise_policy_version'),rise_type=data.get('research_plan',{}).get('rise_type'),
                                    market_context=bool(data.get('symbols') and data.get('dimension_evidence_version')),
                                    outlook_version=data.get('outlook_version'))
    except HTTPException:
        return False
    if replay['input_hash'] != expected:
        return False
    rows = replay['screening_rows']
    with store.db() as conn:
        record = conn.execute('SELECT data,report FROM research_runs WHERE id=%s FOR UPDATE',(run['id'],)).fetchone()
        if not record or not record['report']:
            return False
        value = json.loads(record['data'])
        if value.get('screening_capture') or value['bundle'].get('input_hash')!=expected:
            return False
        save_rows(conn,run['id'],rows)
        receipt = {'basis':'matched_replay','input_hash':expected,'captured_at':store.now(),'count':len(rows)}
        value['screening_capture']=receipt
        conn.execute("UPDATE research_runs SET data=%s WHERE id=%s",(store.dumps(value),run['id']))
        from research import state_store
        state_store.sync_run(conn,run['id'])
    return True


def _matches(row,stage,path,query,rise_type):
    return ((stage=='all' or row['selected']==(stage=='selected'))
            and (not path or row['mask']&BITS[path])
            and (not rise_type or (row['mask'] >> 8)==TYPE_INDEX[rise_type])
            and (not query or query in row['name'].lower() or query.replace('.','') in row['symbol'].replace('.','')))


def _wire(row):
    return {**dict(row),'paths':[key for key,bit in BITS.items() if row['mask']&bit],
            'rise_type':INDEX_TYPE.get(row['mask']>>8)}


def rise_preview(bundle, rise_type, query, limit, after, expected=None):
    signals = bundle['rise_preview']
    market = bundle['market']
    with store.db(readonly=True) as conn:
        hidden = stock_library.retired(conn,signals)
    frozen = hashlib.sha256(store.dumps([bundle['input_hash'],bundle['screening_rows'],signals,sorted(hidden)]).encode()).hexdigest()
    if after and not expected:
        raise HTTPException(422,'分页需要本次筛选的指纹')
    if expected and frozen != expected:
        raise HTTPException(409,'筛选行情已有更新，请从第一页重新筛选')
    query = query.strip().lower()
    rows = []
    for seq,values in enumerate(islice(bundle['screening_rows'],after,None),after+1):
        if values[0] not in signals or values[0] in hidden:
            continue
        row = dict(zip(ROW_FIELDS,values),seq=seq)
        if not _matches(row,'all',None,query,rise_type):
            continue
        rows.append({**_wire(row),'signal':signals[row['symbol']]})
        if len(rows)>limit:
            break
    counts = dict(market['rise_counts'])
    for symbol in hidden:
        counts[signals[symbol]['type']]-=1
    return {'items':rows[:limit],'next':rows[limit-1]['seq'] if len(rows)>limit else None,
            'market_day':market['market_day'],'session':market['session'],'cutoff':bundle['market_data_cutoff'],
            'mode':bundle['mode'],'checked':market['rise_universe'],
            'counts':counts,'covered':market['covered'],'scope_size':market['scope_size'],
            'advancing':market['advancing'],'input_hash':frozen,'rise_policy':bundle['rise_policy']}


def page(run, stage, path, query, limit, after, rise_type=None):
    bundle = run['data'].get('bundle',{})
    market = bundle.get('market')
    if not market:
        return {'basis':'unavailable','items':[],'next':None,'market':None,'match':None}
    basis = run['data'].get('screening_capture',{}).get('basis','selected_only')
    query = query.strip().lower()
    match = None
    with store.db(readonly=True) as conn:
        visible = stock_library.visible(run,conn)
        blocked = sorted({c['symbol'] for c in bundle.get('candidates',[])}-visible)
        if basis == 'selected_only':
            # Older runs without a receipt expose only their bounded frozen selection.
            rows = []
            for i,candidate in enumerate(bundle.get('candidates',[]),1):
                if candidate['symbol'] not in visible:
                    continue
                row = dict(zip(ROW_FIELDS,
                               _line(candidate['symbol'],candidate['name'],candidate['paths'],True,candidate['metrics'])),seq=i)
                if i>after and _matches(row,stage,path,query,rise_type):
                    rows.append(row)
            rows = rows[:limit+1]
        else:
            sql = ("SELECT sequence_no seq,symbol,name,path_mask mask,selected,price_move move,relative_volume "
                   "FROM research_screening s WHERE run_id=%s AND sequence_no>%s AND NOT ("+
                   stock_library.retired_sql('s.symbol')+')')
            args = [run['id'],after]
            if blocked:
                sql += ' AND s.symbol NOT IN ('+','.join(['%s']*len(blocked))+')'
                args.extend(blocked)
            if stage!='all':
                sql += " AND selected=%s"
                args.append(int(stage=='selected'))
            if path:
                sql += " AND (path_mask & %s)>0"
                args.append(BITS[path])
            if rise_type:
                sql += " AND (path_mask >> 8)=%s"
                args.append(TYPE_INDEX[rise_type])
            if query:
                sql += (" AND (LOCATE(%s,LOWER(name))>0 "
                        "OR LOCATE(%s,CONVERT(REPLACE(symbol,'.','') USING utf8mb4))>0)")
                args.extend((query,query.replace('.','')))
            rows = conn.execute(sql+' ORDER BY sequence_no LIMIT %s',(*args,limit+1)).fetchall()
        if query and not rows and not after and stage=='all' and not path and not rise_type:
            try:
                symbol = assets.symbol(query)
            except HTTPException:
                pass
            else:
                key = (coverage.status_for(conn,run['id'],symbol) if symbol not in blocked
                       and not stock_library.retired(conn,[symbol]) else None)
                if key:
                    detail = coverage.LABELS[key]
                    if key=='covered':
                        detail = '已覆盖行情，未命中本轮线索' if basis!='selected_only' else '已覆盖行情，本轮仅保留已送研究的明细'
                    match = {'symbol':symbol,'detail':detail}
    market_keys = ('market_day','session','scope_size','covered','participating','advancing','declining','discovered','displayed','path_counts','ordering')
    return {'basis':basis,'items':[_wire(row) for row in rows[:limit]],
            'next':rows[limit-1]['seq'] if len(rows)>limit else None,'match':match,
            'market':{k:market.get(k) for k in market_keys},'pointed':bool(run['data'].get('symbols')),
            'rise_policy':bundle.get('rise_policy'),
            'rules':{k:v for k,v in run['data'].get('method',{}).items()
                     if k in ('relative_volume_min','close_location_min','sector_breadth_min')}}
