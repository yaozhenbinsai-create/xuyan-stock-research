"""Lossless, bounded migration from the legacy payload schema to typed records.

Only a new isolated schema is written.  The source is held in a read-only
repeatable-read snapshot.  Production switching is deliberately a separate
operator-reviewed operation, not an implicit side effect of stage or sync.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import struct
from typing import Callable
import zlib

from foundation import database as store, record_store, storage_policy
from tools.mysql_instance import create_isolated_database


_NAME = re.compile(r'^[a-z][a-z0-9_]*$')
_ISOLATED = re.compile(r'^stock_(?:restore_|test_)[a-z0-9_]+$')
_CORE = {'facts','heads','fact_origins','evidence_refs','schema_versions','asset_stats'}
_METADATA = ('id','dataset','day','symbol','record_key','source','source_version',
             'fingerprint','first_seen','last_seen','acquisitions','original_id','last_original_id','payload_bytes')
Progress = Callable[[dict],None]


class MigrationError(RuntimeError):
    pass


def _ident(value):
    if not _NAME.fullmatch(value):
        raise MigrationError('Unsafe SQL identifier')
    return '`'+value+'`'


def _validate_names(source,target):
    store.connection_options(database=source)
    store.connection_options(database=target)
    if source==target or not _ISOLATED.fullmatch(target):
        raise MigrationError('Target must be a distinct stock_restore_* or stock_test_* schema')


def _wire(value):
    """Deterministic row hashes; credentials and source documents stay in memory."""
    if isinstance(value,(bytes,bytearray,memoryview)):
        return {'$binary':base64.b64encode(bytes(value)).decode('ascii')}
    if isinstance(value,(date,datetime)):
        return value.isoformat()
    if isinstance(value,timedelta):
        return {'$time_microseconds':(value.days*86400+value.seconds)*1000000+value.microseconds}
    if isinstance(value,Decimal):
        return {'$decimal':str(value)}
    return value


def _feed(digest,values):
    digest.update((json.dumps([_wire(v) for v in values],ensure_ascii=False,
                              allow_nan=False,separators=(',',':'))+'\n').encode('utf-8'))


def _contract_value(value):
    """Compare business-equivalent numeric values and ISO instants, preserving nulls."""
    if isinstance(value,dict):
        return {key:_contract_value(item) for key,item in value.items()}
    if isinstance(value,list):
        return [_contract_value(item) for item in value]
    if type(value) is bool:
        return ('boolean',value)
    if type(value) in (int,float):
        return Decimal(str(value))
    if isinstance(value,str) and re.fullmatch(r'\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?(?:\.\d{1,6})?(?:Z|[+-]\d\d:?\d\d)?',value):
        instant=datetime.fromisoformat(value.replace('Z','+00:00'))
        instant=instant.replace(tzinfo=store.SHANGHAI) if instant.tzinfo is None else instant
        return ('instant',instant.astimezone(store.SHANGHAI))
    return value


def _tables(conn):
    return {r[0] for r in conn.execute('SELECT TABLE_NAME FROM information_schema.tables '
                                      "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE'")}


def _columns(conn,table):
    return [r[0] for r in conn.execute('SHOW COLUMNS FROM '+_ident(table)) if 'GENERATED' not in r[5]]


def _auto_increment_values(conn):
    # Read live engine metadata, not a cached information_schema statistic. The
    # counter includes deleted/failed inserts and cannot be inferred from MAX(id).
    conn.execute('SET SESSION information_schema_stats_expiry=0')
    return {row[0]:int(row[1]) for row in conn.execute(
        'SELECT TABLE_NAME,AUTO_INCREMENT FROM information_schema.tables '
        'WHERE TABLE_SCHEMA=DATABASE() AND AUTO_INCREMENT IS NOT NULL')}


def _sync_auto_increment(source,target):
    before=_auto_increment_values(target)
    wanted={}
    for table,next_id in _auto_increment_values(source).items():
        if table not in before:
            raise MigrationError('Target lacks source AUTO_INCREMENT: '+table)
        desired=max(next_id,before[table])
        if desired>before[table]:
            target.execute('ALTER TABLE '+_ident(table)+' AUTO_INCREMENT='+str(desired))
        wanted[table]={'source_next':next_id,'target_before':before[table],'required_next':desired}
    after=_auto_increment_values(target)
    for table,values in wanted.items():
        actual=after.get(table,0)
        if actual<values['required_next']:
            raise MigrationError('AUTO_INCREMENT high-water mark differs: '+table)
        values['target_next']=actual
    return wanted


def _validate_auto_increment(source,target):
    current=_auto_increment_values(target)
    result={}
    for table,source_next in _auto_increment_values(source).items():
        target_next=current.get(table,0)
        if target_next<source_next:
            raise MigrationError('AUTO_INCREMENT high-water mark differs: '+table)
        result[table]={'source_next':source_next,'target_next':target_next}
    return result


def _primary(conn,table):
    rows=conn.execute('SHOW INDEX FROM '+_ident(table)+" WHERE Key_name='PRIMARY'").fetchall()
    keys=[r['Column_name'] for r in sorted(rows,key=lambda r:r['Seq_in_index'])]
    if not keys:
        raise MigrationError('Table has no stable primary key: '+table)
    return keys


def _pages(conn,table,columns,keys,batch_size):
    names=','.join(_ident(v) for v in columns)
    ordering=','.join(_ident(v) for v in keys)
    after=None
    while True:
        predicate='';parameters=[]
        if after is not None:
            # MySQL may scan from the start for a row-constructor inequality.
            # Equivalent prefix equality/range terms use the complete PK range.
            terms=[]
            for index,key in enumerate(keys):
                terms.append('('+' AND '.join([_ident(v)+'=%s' for v in keys[:index]]+
                                               [_ident(key)+'>%s'])+')')
                parameters.extend(after[:index+1])
            predicate=' WHERE ('+' OR '.join(terms)+')'
        rows=conn.execute('SELECT '+names+' FROM '+_ident(table)+predicate+
                          ' ORDER BY '+ordering+' LIMIT %s',(*parameters,batch_size)).fetchall()
        if not rows:
            return
        yield [dict(r) for r in rows]
        after=tuple(rows[-1][key] for key in keys)
        if len(rows)<batch_size:
            return


def _insert(conn,table,columns,rows,*,update=False):
    if not rows:
        return
    sql='INSERT INTO '+_ident(table)+' ('+','.join(_ident(v) for v in columns)+') VALUES ('+','.join(['%s']*len(columns))+')'
    if update:
        sql+=' ON DUPLICATE KEY UPDATE '+','.join(_ident(v)+'=VALUES('+_ident(v)+')' for v in columns if v!='id')
    conn.executemany(sql,[tuple(row.get(key) for key in columns) for row in rows])


def _fingerprint(value):
    if isinstance(value,str):
        try:
            value=bytes.fromhex(value)
        except ValueError as exc:
            raise MigrationError('Source fingerprint is not hexadecimal') from exc
    value=bytes(value)
    if len(value)!=32:
        raise MigrationError('Source fingerprint has an unexpected length')
    return value


def _metadata(row):
    payload=row['payload']
    raw=payload.encode('utf-8') if isinstance(payload,str) else bytes(payload)
    value=json.loads(raw)
    result={key:row[key] for key in _METADATA if key in row}
    result['fingerprint']=_fingerprint(row['fingerprint'])
    result['payload_bytes']=len(raw)
    if isinstance(result['day'],date):
        result['day']=result['day'].isoformat()
    return result,value


def _fact_batch_check(target,expected,values):
    ids=tuple(row['id'] for row in expected)
    marks=','.join(['%s']*len(ids))
    saved={r['id']:dict(r) for r in target.execute('SELECT '+','.join(_ident(v) for v in _METADATA)+
                                                 ' FROM facts WHERE id IN ('+marks+')',ids)}
    loaded=record_store.load_many(target,ids)
    for source in expected:
        fid=source['id']
        if fid not in saved or fid not in loaded:
            raise MigrationError('Missing typed fact '+str(fid))
        for field in _METADATA:
            if field not in source:
                continue
            before,after=source[field],saved[fid][field]
            if field=='fingerprint':
                before,after=_fingerprint(before),_fingerprint(after)
            elif field=='day':
                before,after=str(before),str(after)
            if before!=after:
                raise MigrationError(f'Fact metadata differs: id={fid}, field={field}')
        if store.dumps(values[fid])!=store.dumps(loaded[fid]):
            raise MigrationError(f'Typed value differs: id={fid}, dataset={source["dataset"]}')


def _copy_facts(source,target,batch_size,progress):
    if 'payload' not in _columns(source,'facts'):
        raise MigrationError('Source is not the legacy payload schema')
    target.execute('DROP TEMPORARY TABLE IF EXISTS migration_seen_fact_ids')
    target.execute('CREATE TEMPORARY TABLE migration_seen_fact_ids(id BIGINT UNSIGNED PRIMARY KEY) ENGINE=InnoDB')
    counts=Counter();digest=hashlib.sha256();mismatches=[];mismatch_count=0;total=0
    for batch in _pages(source,'facts',_columns(source,'facts'),['id'],batch_size):
        metadata=[];values={};items=[]
        ids=tuple(row['id'] for row in batch)
        marks=','.join(['%s']*len(ids))
        existing={r[0]:_fingerprint(r[1]) for r in target.execute('SELECT id,fingerprint FROM facts WHERE id IN ('+marks+')',ids)}
        for row in batch:
            meta,payload=_metadata(row);fid=meta['id']
            unknown=set(payload)-storage_policy.FIELDS[meta['dataset']]-{'day','symbol','key'}
            if unknown:
                raise MigrationError(f'Unmapped business fields at id={fid}, dataset={meta["dataset"]}: '+','.join(sorted(unknown)))
            if fid in existing and existing[fid]!=meta['fingerprint']:
                raise MigrationError('Immutable fact fingerprint changed at id='+str(fid))
            if storage_policy.content_hash(meta['dataset'],payload)!=meta['fingerprint'].hex():
                mismatch_count+=1
                if len(mismatches)<20:
                    mismatches.append({'id':fid,'dataset':meta['dataset']})
            metadata.append(meta);values[fid]=payload
            if fid not in existing:
                items.append((fid,meta['dataset'],payload))
            counts[meta['dataset']]+=1;total+=1
            _feed(digest,(fid,meta['fingerprint'],store.dumps(payload)))
        _insert(target,'facts',list(_METADATA),metadata,update=True)
        record_store.save_many(target,items)
        target.executemany('INSERT INTO migration_seen_fact_ids(id) VALUES(%s)',[(fid,) for fid in ids])
        _fact_batch_check(target,metadata,values)
        target.raw.commit()
        if progress:
            progress({'phase':'facts','processed':total,'through_id':batch[-1]['id']})
    return {'count':total,'by_dataset':dict(counts),'canonical_sha256':digest.hexdigest(),
            'current_fingerprint_algorithm_mismatches':mismatch_count,
            'mismatch_id_sample':mismatches,'original_fingerprints_preserved':True}


def _order(conn,tables):
    tables=set(tables)
    edges={name:set() for name in tables}
    for row in conn.execute('SELECT TABLE_NAME,REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE '
                            'WHERE TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL'):
        child,parent=row[0],row[1]
        if child in tables and parent in tables and child!=parent:
            edges[child].add(parent)
    result=[]
    while edges:
        ready=sorted(name for name,deps in edges.items() if not deps)
        if not ready:
            raise MigrationError('Cyclic table dependencies require explicit migration handling')
        result.extend(ready)
        for name in ready:
            del edges[name]
        for deps in edges.values():
            deps.difference_update(ready)
    return result


def _common(source,target,table):
    before=_columns(source,table);after=set(_columns(target,table))
    missing=set(before)-after
    if missing:
        raise MigrationError('Legacy columns have no explicit target mapping: '+table+':'+','.join(sorted(missing)))
    return before


def _copy_table(source,target,table,batch_size):
    columns=_common(source,target,table);keys=_primary(source,table)
    count=0;digest=hashlib.sha256()
    for batch in _pages(source,table,columns,keys,batch_size):
        _insert(target,table,columns,batch)
        for row in batch:
            _feed(digest,[row[key] for key in columns]);count+=1
        target.raw.commit()
    return {'count':count,'sha256':digest.hexdigest(),'columns':columns}


def _copy_ordinary(source,target,tables,batch_size,progress=None):
    tables=set(tables)
    ordered=_order(target,tables);copied={};skipped=[]
    if 'heads' in tables:
        columns=_common(source,target,'heads');keys=_primary(source,'heads')
        before=_table_digest(source,'heads',columns,keys,batch_size)
        after=_table_digest(target,'heads',columns,keys,batch_size)
        if before==after:
            copied['heads']=before;skipped.append('heads')
            if progress:
                progress({'phase':'skip_unchanged_table','table':'heads','processed':before['count']})
    for table in reversed(ordered):
        if table not in skipped:
            target.execute('DELETE FROM '+_ident(table))
    target.raw.commit()
    for table in ordered:
        if table in skipped:
            continue
        copied[table]=_copy_table(source,target,table,batch_size)
        if progress:
            progress({'phase':'copy_table','table':table,'processed':copied[table]['count']})
    return copied,skipped


def _asset_rows(source,batch_size):
    columns=_columns(source,'asset_stats')
    for batch in _pages(source,'asset_stats',columns,['dataset'],batch_size):
        result=[]
        for row in batch:
            if 'data' in row:
                value=json.loads(row['data'])
                if value.get('dataset',row['dataset'])!=row['dataset']:
                    raise MigrationError('Asset statistics dataset identity differs')
                row={'dataset':row['dataset'],'record_count':value.get('count',0),'first_day':value.get('first'),
                     'last_day':value.get('last'),'symbol_count':value.get('symbols',0),
                     'version_count':value.get('versions',0),'generation':value.get('generation',0)}
            result.append(row)
        yield result


_ASSET_COLUMNS=('dataset','record_count','first_day','last_day','symbol_count','version_count','generation')


def _copy_assets(source,target,batch_size):
    target.execute('DELETE FROM asset_stats')
    for rows in _asset_rows(source,batch_size):
        _insert(target,'asset_stats',_ASSET_COLUMNS,rows)
    target.raw.commit()


def _prune_missing_facts(target):
    removed=0
    while True:
        rows=target.execute('SELECT f.id FROM facts f LEFT JOIN migration_seen_fact_ids s ON s.id=f.id '
                            'WHERE s.id IS NULL ORDER BY f.id LIMIT 1000').fetchall()
        if not rows:
            return removed
        ids=tuple(r[0] for r in rows)
        target.execute('DELETE FROM facts WHERE id IN ('+','.join(['%s']*len(ids))+')',ids)
        target.raw.commit();removed+=len(ids)


def _project(target,batch_size,progress):
    from research import state_store
    from research import coverage
    from foundation import queue_state
    state_store.apply_schema(target)
    coverage.apply_schema(target)
    queue_state.apply_schema(target)
    if not hasattr(state_store,'sync_call'):
        raise MigrationError('Research call projection implementation is missing')
    specs=(('research_runs',('id',),state_store.sync_run),
           ('research_cycles',('id',),state_store.sync_cycle),
           ('research_outcomes',('cycle_id','horizon','kind'),state_store.sync_outcome),
           ('research_controls',('id',),state_store.sync_control),
           ('research_events',('id',),state_store.sync_event),
           ('research_notices',('id',),state_store.sync_notice),
           ('research_calls',('id',),state_store.sync_call),
           ('collection_jobs',('id',),queue_state.sync_job),
           ('schedules',('id',),queue_state.sync_schedule))
    tables=_tables(target);counts={}
    for table,keys,synchronize in specs:
        if table not in tables:
            raise MigrationError('Research target table is missing: '+table)
        count=0
        for rows in _pages(target,table,keys,keys,batch_size):
            for row in rows:
                synchronize(target,*(row[key] for key in keys));count+=1
            target.raw.commit()
        counts[table]=count
        if progress:
            progress({'phase':'research_projection','table':table,'processed':count})
    return counts


def _table_digest(conn,table,columns,keys,batch_size):
    digest=hashlib.sha256();count=0
    for batch in _pages(conn,table,columns,keys,batch_size):
        for row in batch:
            _feed(digest,[row[key] for key in columns]);count+=1
    return {'count':count,'sha256':digest.hexdigest()}


def _validate_projection(target,batch_size):
    """Check the native query fields against their retained immutable documents."""
    from research import state_store
    from research import coverage
    from foundation import queue_state
    counts={}
    expected_decisions=0
    for batch in _pages(target,'research_runs',_columns(target,'research_runs'),['id'],batch_size):
        for row in batch:
            data=json.loads(row['data']);report=json.loads(row['report']) if row['report'] else {}
            bundle=data.get('bundle') or {};forecast=report.get('forecast_target')
            expected={'session':data.get('session') or report.get('session') or 'close','mode':report.get('mode'),
                      'published_at':state_store.sql_time(report.get('published_at')),
                      'withdrawn_at':state_store.sql_time(data.get('withdrawn_at')),
                      'study_horizon':report.get('study_horizon'),
                      'target_version':forecast.get('version') if forecast is not None else None,
                      'has_forecast_target':forecast is not None,'has_bundle':bundle.get('market') is not None,
                      'market_coverage':(bundle.get('market') or {}).get('coverage'),
                      'market_analysis':report.get('market_analysis'),
                      'decision_count':len(report.get('decisions') or []),'intent':data.get('intent'),
                      'title':report.get('title'),'has_validated_report':data.get('validated_report') is not None,
                      'stop_requested':bool(data.get('stop_requested')),
                      'detail_compacted_at':data.get('detail_compacted_at'),
                      'rotation_retry_not_before':(data.get('rotation') or {}).get('retry_not_before')}
            for field,value in expected.items():
                if row[field]!=value:
                    raise MigrationError(f'Research projection differs: runs/{row["id"]}/{field}')
            decisions=report.get('decisions') or []
            saved=[dict(v) for v in target.execute('SELECT sequence_no,symbol,name,action,cycle_id,review_event,rise_type,rise_name,stars '
                                                  'FROM research_decisions WHERE run_id=%s ORDER BY sequence_no',(row['id'],))]
            expected_rows=[{'sequence_no':index,'symbol':decision['symbol'],'name':decision.get('name') or decision['symbol'],
                            'action':decision['action'],'cycle_id':decision.get('cycle_id'),
                            'review_event':bool(decision.get('review_event')),'rise_type':(decision.get('rise') or {}).get('type'),
                            'rise_name':(decision.get('rise') or {}).get('name'),'stars':(decision.get('rating') or {}).get('stars')}
                           for index,decision in enumerate(decisions)]
            if saved!=expected_rows:
                raise MigrationError('Research decision projection differs: '+row['id'])
            packed=bundle.get('scope_pack')
            if isinstance(packed,dict):
                scope=target.execute('SELECT fingerprint FROM research_scope_sets WHERE run_id=%s',(row['id'],)).fetchone()
                if not scope or bytes(scope[0])!=hashlib.sha256(store.dumps(packed).encode()).digest():
                    raise MigrationError('Research coverage fingerprint differs: '+row['id'])
                members=[]
                for state,value in sorted(packed.items()):
                    compressed=base64.b64decode(value,validate=True)
                    raw=zlib.decompress(compressed[4:])
                    if state not in coverage.LABELS or len(raw)!=struct.unpack('<I',compressed[:4])[0] or len(raw)%coverage.WIDTH:
                        raise MigrationError('Source coverage membership is invalid: '+row['id'])
                    for offset in range(0,len(raw),coverage.WIDTH):
                        code=raw[offset:offset+coverage.WIDTH].decode('ascii')
                        members.append((state,offset//coverage.WIDTH,coverage.MARKETS[code[0]]+'.'+code[1:]))
                saved_members=[tuple(v) for v in target.execute('SELECT state,sequence_no,symbol FROM research_scope_members '
                                                               'WHERE run_id=%s ORDER BY state,sequence_no',(row['id'],))]
                if saved_members!=members:
                    raise MigrationError('Research coverage members differ: '+row['id'])
            if isinstance(bundle.get('candidates'),list):
                candidates=[(index,entry['symbol']) for index,entry in enumerate(bundle['candidates'])]
                saved_candidates=[tuple(v) for v in target.execute('SELECT sequence_no,symbol FROM research_candidates '
                                                                  'WHERE run_id=%s ORDER BY sequence_no',(row['id'],))]
                if saved_candidates!=candidates:
                    raise MigrationError('Research candidates differ: '+row['id'])
            expected_decisions+=len(decisions)
    actual_decisions=int(target.execute('SELECT COUNT(*) FROM research_decisions').fetchone()[0])
    if actual_decisions!=expected_decisions:
        raise MigrationError('Research decision row total differs')
    counts['research_decisions']=actual_decisions
    specs={
        'research_outcomes':(('cycle_id','horizon','kind'),lambda d:{'direction':d.get('direction'),'return_value':d.get('return'),'excess_return':d.get('excess_return')}),
        'research_controls':(('id',),lambda d:{'method_version':d.get('method'),'return_value':d.get('return_'),'target_version':d.get('target_version'),'reference_basis':d.get('reference_basis')}),
        'research_events':(('id',),lambda d:{'to_state':d.get('to')}),
        'research_notices':(('id',),lambda d:{'action':d.get('action'),'symbol':d.get('symbol')}),
        'research_calls':(('id',),lambda d:{'attempt_no':d.get('attempt') or 0,'recoverable':bool(d.get('recoverable')),'usage_state':d.get('usage_state'),
                                           **{field:(d.get('usage_normalized') or {}).get(key) for field,key in
                                              (('input_tokens','input'),('output_tokens','output'),('total_tokens','total'),
                                               ('cached_input_tokens','cached_input'),('reasoning_output_tokens','reasoning_output'))}}),
    }
    def cycle_values(data):
        pool=data.get('pool_review') or {}
        ends=[day for day in (data.get('endpoints') or {}).values() if day]
        return {'action':data.get('action'),'session':(data.get('session_policy') or {}).get('session') or 'close',
                'rise_type':(data.get('rise') or {}).get('type'),'rise_policy_version':data.get('rise_policy_version'),
                'method_version':data.get('method_version'),'thesis_key':data.get('thesis_key') or 'structure',
                'research_horizon':data.get('research_horizon') or 'D3',
                'first_removed_at':state_store.sql_time(data.get('first_removed_at')),'end_day':max(ends) if ends else None,
                'pool_review_day':pool.get('day'),'pool_review_fact_id':pool.get('fact_id'),
                'pool_review_outcome':pool.get('outcome'),'pool_review_reason':pool.get('reason')}
    specs['research_cycles']=(('id',),cycle_values)
    def job_values(data):
        return {**{key:data.get(key,0) for key in queue_state.COUNTERS},
                'retry_at':float(data.get('retry_at',0)),'expires_at':queue_state.local_datetime(data.get('expires_at')),
                'trigger':data.get('trigger',''),'last_yield':float(data.get('last_yield',0)),
                'source':data.get('source'),'dataset':data.get('dataset'),'schedule_id':data.get('schedule_id'),
                'mode':data.get('mode','missing'),'name':data.get('name',''),'message':data.get('message'),
                'updated':data.get('updated') or data.get('created'),'range_start':data.get('start'),'range_end':data.get('end')}
    def schedule_values(data):
        at=data.get('at')
        return {'last_job':data.get('last_job'),'last_slot':data.get('last_slot'),
                'source':data.get('source'),'kind':data.get('kind'),'enabled':data.get('enabled',False),
                'trading_only':data.get('trading_only',False),'interval_seconds':data.get('interval',0),
                'run_at':timedelta(hours=int(at[:2]),minutes=int(at[3:5])) if at else None}
    specs['collection_jobs']=(('id',),job_values)
    specs['schedules']=(('id',),schedule_values)
    for table,(keys,derive) in specs.items():
        count=0
        for batch in _pages(target,table,_columns(target,table),keys,batch_size):
            for row in batch:
                document=json.loads(row['data'])
                for field,value in derive(document).items():
                    if row[field]!=value:
                        raise MigrationError('Research projection differs: '+table+'/'+field)
                if table=='research_cycles':
                    try:
                        contract=state_store.load_cycle_data(target,row)
                    except RuntimeError as exc:
                        raise MigrationError('Incomplete cycle contract: '+row['id']) from exc
                    if _contract_value(contract)!=_contract_value(document):
                        raise MigrationError('Cycle contract differs: '+row['id'])
                    expected_counts={
                        'research_cycle_contracts':1,
                        'research_cycle_endpoints':len(document.get('endpoints') or {}),
                        'research_cycle_references':sum(document.get(kind+'_reference') is not None for kind in ('entry','removal')),
                        'research_cycle_peer_references':sum(len(document.get(kind+'_peer_refs') or []) for kind in ('entry','removal')),
                    }
                    for child,expected_count in expected_counts.items():
                        actual=int(target.execute('SELECT COUNT(*) FROM '+_ident(child)+' WHERE cycle_id=%s',(row['id'],)).fetchone()[0])
                        if actual!=expected_count:
                            raise MigrationError('Cycle contract child rows differ: '+child+'/'+row['id'])
                count+=1
        counts[table]=count
    return counts


def _validate_snapshot(source,target,batch_size,progress):
    fact_count=0;counts=Counter();digest=hashlib.sha256()
    for batch in _pages(source,'facts',_columns(source,'facts'),['id'],batch_size):
        metadata=[];values={}
        for row in batch:
            meta,payload=_metadata(row)
            metadata.append(meta);values[meta['id']]=payload
            counts[meta['dataset']]+=1;fact_count+=1
            _feed(digest,(meta['id'],meta['fingerprint'],store.dumps(payload)))
        _fact_batch_check(target,metadata,values)
        if progress:
            progress({'phase':'validate_facts','processed':fact_count,'through_id':batch[-1]['id']})
    target_count=int(target.execute('SELECT COUNT(*) FROM facts').fetchone()[0])
    if target_count!=fact_count:
        raise MigrationError('Target/source fact count differs')
    summaries={}
    for table in sorted(_tables(source)-{'facts','schema_versions','asset_stats'}):
        if table not in _tables(target):
            raise MigrationError('Target is missing source table '+table)
        columns=_common(source,target,table);keys=_primary(source,table)
        before=_table_digest(source,table,columns,keys,batch_size)
        after=_table_digest(target,table,columns,keys,batch_size)
        if before!=after:
            raise MigrationError('Source table content differs: '+table)
        summaries[table]=before
    before=hashlib.sha256();source_assets=0
    for rows in _asset_rows(source,batch_size):
        for row in rows:
            _feed(before,[row[key] for key in _ASSET_COLUMNS]);source_assets+=1
    after=_table_digest(target,'asset_stats',_ASSET_COLUMNS,['dataset'],batch_size)
    if after!={'count':source_assets,'sha256':before.hexdigest()}:
        raise MigrationError('Relational asset statistics differ')
    references={}
    for table,column in (('heads','fact_id'),('fact_origins','fact_id'),('evidence_refs','fact_id')):
        missing=int(target.execute('SELECT COUNT(*) FROM '+_ident(table)+' r LEFT JOIN facts f ON f.id=r.'+_ident(column)+' WHERE f.id IS NULL').fetchone()[0])
        references[table]=missing
        if missing:
            raise MigrationError('Dangling fact references in '+table)
    typed={}
    for dataset,table in record_store.DATASET_TABLES.items():
        missing=int(target.execute('SELECT COUNT(*) FROM facts f LEFT JOIN '+_ident(table)+
                                  " t ON t.fact_id=f.id WHERE f.dataset=%s AND t.fact_id IS NULL",(dataset,)).fetchone()[0])
        typed[dataset]=missing
        if missing:
            raise MigrationError('Missing typed rows for '+dataset)
    version=target.execute('SELECT MAX(version) FROM schema_versions').fetchone()[0]
    if version!=store.SCHEMA_VERSION:
        raise MigrationError('Target schema version is not current')
    projections=_validate_projection(target,batch_size)
    auto_increment=_validate_auto_increment(source,target)
    return {'facts':fact_count,'facts_by_dataset':dict(counts),'canonical_sha256':digest.hexdigest(),
            'tables':summaries,'asset_stats':after,'dangling_references':references,
            'missing_typed_records':typed,'research_projections':projections,
            'schema_version':version,'auto_increment':auto_increment,'full_value_fidelity':True}


def synchronize(source_name,target_name,*,batch_size=1000,progress=None):
    _validate_names(source_name,target_name)
    if not 1<=batch_size<=5000:
        raise MigrationError('Batch size must be between 1 and 5000')
    started=store.now()
    with store.db(admin=True,database=source_name,readonly=True,isolation='REPEATABLE READ',timeout=300) as source:
        # Establish the same snapshot before reading the other source tables.
        source.execute('SELECT id FROM facts ORDER BY id LIMIT 1').fetchone()
        with store.db(admin=True,database=target_name,timeout=300) as target:
            source_tables=_tables(source)
            if 'payload' not in _columns(source,'facts'):
                raise MigrationError('Source is not a legacy schema')
            unknown=source_tables-_tables(target)
            if unknown:
                raise MigrationError('Source tables have no target mapping: '+','.join(sorted(unknown)))
            facts=_copy_facts(source,target,batch_size,progress)
            ordinary=source_tables-{'facts','schema_versions','asset_stats'}
            copied,skipped=_copy_ordinary(source,target,ordinary,batch_size,progress)
            _copy_assets(source,target,batch_size)
            removed=_prune_missing_facts(target)
            projection=_project(target,batch_size,progress)
            auto_increment=_sync_auto_increment(source,target)
            validation=_validate_snapshot(source,target,batch_size,progress)
            target.execute('DROP TEMPORARY TABLE migration_seen_fact_ids')
    return {'operation':'sync','source':source_name,'target':target_name,'started':started,'finished':store.now(),
            'source_transaction':'READ ONLY REPEATABLE READ','production_cutover':False,
            'facts':facts,'copied_tables':{k:{'count':v['count'],'sha256':v['sha256']} for k,v in copied.items()},
            'skipped_unchanged_tables':skipped,
            'source_removed_facts_pruned_from_target':removed,'research_projection':projection,
            'auto_increment':auto_increment,
            'validation':validation,'status':'validated'}


def stage(source_name,target_name,*,batch_size=1000,progress=None):
    _validate_names(source_name,target_name)
    # Only this function creates schemas. Existing targets require explicit sync.
    create_isolated_database(target_name)
    result=synchronize(source_name,target_name,batch_size=batch_size,progress=progress)
    result['operation']='stage'
    return result


def validate(source_name,target_name,*,batch_size=1000,progress=None):
    _validate_names(source_name,target_name)
    if not 1<=batch_size<=5000:
        raise MigrationError('Batch size must be between 1 and 5000')
    with store.db(admin=True,database=source_name,readonly=True,isolation='REPEATABLE READ',timeout=300) as source:
        source.execute('SELECT id FROM facts ORDER BY id LIMIT 1').fetchone()
        with store.db(admin=True,database=target_name,readonly=True,isolation='REPEATABLE READ',timeout=300) as target:
            result=_validate_snapshot(source,target,batch_size,progress)
    return {'operation':'validate','source':source_name,'target':target_name,'finished':store.now(),
            'production_cutover':False,'validation':result,'status':'validated'}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('stage','sync','validate'))
    parser.add_argument('--source',default='stock_foundation')
    parser.add_argument('--target',required=True)
    parser.add_argument('--batch-size',type=int,default=1000)
    parser.add_argument('--receipt',type=Path)
    args=parser.parse_args(argv)
    progress=lambda value:print(store.dumps({'at':store.now(),**value}),flush=True)
    operation={'stage':stage,'sync':synchronize,'validate':validate}[args.operation]
    result=operation(args.source,args.target,batch_size=args.batch_size,progress=progress)
    if args.receipt:
        args.receipt.parent.mkdir(parents=True,exist_ok=True)
        args.receipt.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(store.dumps(result),flush=True)
    return result


if __name__=='__main__':
    main()
