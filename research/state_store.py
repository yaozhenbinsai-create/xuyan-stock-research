"""Relational query state for research documents and immutable decisions.

Frozen inputs and signed report originals remain documents.  Predicates,
aggregates, pool membership and outcome evaluation use the explicit columns
and decision rows maintained here in the same transaction as each write.
"""
from __future__ import annotations

from datetime import datetime
import copy
import json

from foundation import database as store


TABLES = ('research_decisions','research_cycle_contracts','research_cycle_endpoints',
          'research_cycle_references','research_cycle_peer_references')
COLUMNS = {
    'research_runs': {
        'session':"VARCHAR(16) CHARACTER SET ascii NOT NULL DEFAULT 'close'",
        'mode':'VARCHAR(32) CHARACTER SET ascii NULL',
        'published_at':'DATETIME(6) NULL', 'withdrawn_at':'DATETIME(6) NULL',
        'study_horizon':'VARCHAR(8) CHARACTER SET ascii NULL',
        'target_version':'VARCHAR(64) CHARACTER SET ascii NULL',
        'has_forecast_target':'BOOLEAN NOT NULL DEFAULT 0',
        'has_bundle':'BOOLEAN NOT NULL DEFAULT 0', 'market_coverage':'DOUBLE NULL',
        'market_analysis':'MEDIUMTEXT NULL',
        'decision_count':'INT UNSIGNED NOT NULL DEFAULT 0',
        'intent':'TEXT NULL', 'title':'VARCHAR(512) NULL',
        'has_validated_report':'BOOLEAN NOT NULL DEFAULT 0',
        'stop_requested':'BOOLEAN NOT NULL DEFAULT 0',
        'detail_compacted_at':'VARCHAR(32) CHARACTER SET ascii NULL',
        'rotation_retry_not_before':'DOUBLE NULL',
    },
    'research_cycles': {
        'action':'VARCHAR(16) CHARACTER SET ascii NULL',
        'session':"VARCHAR(16) CHARACTER SET ascii NOT NULL DEFAULT 'close'",
        'rise_type':'VARCHAR(16) CHARACTER SET ascii NULL',
        'rise_policy_version':'VARCHAR(64) CHARACTER SET ascii NULL',
        'method_version':'VARCHAR(64) CHARACTER SET ascii NULL',
        'thesis_key':"VARCHAR(64) CHARACTER SET ascii NOT NULL DEFAULT 'structure'",
        'research_horizon':"VARCHAR(8) CHARACTER SET ascii NOT NULL DEFAULT 'D3'",
        'first_removed_at':'DATETIME(6) NULL', 'end_day':'DATE NULL',
        'pool_review_day':'DATE NULL', 'pool_review_fact_id':'BIGINT UNSIGNED NULL',
        'pool_review_outcome':'VARCHAR(32) CHARACTER SET ascii NULL',
        'pool_review_reason':'TEXT NULL',
    },
    'research_outcomes': {
        'direction':'VARCHAR(8) CHARACTER SET ascii NULL',
        'return_value':'DOUBLE NULL', 'excess_return':'DOUBLE NULL',
    },
    'research_controls': {
        'method_version':'VARCHAR(64) CHARACTER SET ascii NULL',
        'return_value':'DOUBLE NULL', 'target_version':'VARCHAR(64) CHARACTER SET ascii NULL',
        'reference_basis':'VARCHAR(32) CHARACTER SET ascii NULL',
    },
    'research_events': {'to_state':'VARCHAR(24) CHARACTER SET ascii NULL'},
    'research_notices': {
        'action':'VARCHAR(24) CHARACTER SET ascii NULL',
        'symbol':'VARCHAR(12) CHARACTER SET ascii NULL',
    },
    'research_calls': {
        'attempt_no':'INT UNSIGNED NOT NULL DEFAULT 0',
        'recoverable':'BOOLEAN NOT NULL DEFAULT 0',
        'usage_state':'VARCHAR(16) CHARACTER SET ascii NULL',
        **{name:'BIGINT UNSIGNED NULL' for name in ('input_tokens','output_tokens','total_tokens',
                                                   'cached_input_tokens','reasoning_output_tokens')},
    },
}

DECISION_DDL = '''CREATE TABLE IF NOT EXISTS research_decisions(
 run_id CHAR(16) CHARACTER SET ascii NOT NULL,
 sequence_no SMALLINT UNSIGNED NOT NULL,
 symbol VARCHAR(12) CHARACTER SET ascii NOT NULL,
 name VARCHAR(512) NOT NULL,
 action VARCHAR(16) CHARACTER SET ascii NOT NULL,
 cycle_id CHAR(16) CHARACTER SET ascii NULL,
 review_event BOOLEAN NOT NULL,
 rise_type VARCHAR(16) CHARACTER SET ascii NULL,
 rise_name VARCHAR(64) NULL,
 stars TINYINT UNSIGNED NULL,
 PRIMARY KEY(run_id,sequence_no),
 KEY decision_symbol(symbol,run_id), KEY decision_cycle(cycle_id),
 KEY decision_rise(rise_type,run_id),
 CONSTRAINT decision_run FOREIGN KEY(run_id) REFERENCES research_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB'''

CONTRACT_DDL = (
'''CREATE TABLE IF NOT EXISTS research_cycle_contracts(
 cycle_id CHAR(16) CHARACTER SET ascii PRIMARY KEY,
 present_fields INT UNSIGNED NOT NULL, null_fields INT UNSIGNED NOT NULL,
 entry_fields SMALLINT UNSIGNED NOT NULL, session_fields SMALLINT UNSIGNED NOT NULL,
 structure_fields SMALLINT UNSIGNED NOT NULL, rise_fields SMALLINT UNSIGNED NOT NULL,
 last_review_fields SMALLINT UNSIGNED NOT NULL,
 entry_low DOUBLE NULL, entry_high DOUBLE NULL, invalidate_below DOUBLE NULL, entry_condition TEXT NULL,
 invalidation TEXT NULL, published_at DATETIME(6) NULL, adjustment_verified BOOLEAN NULL,
 reference_from DATETIME(6) NULL, action_valid_until DATETIME(6) NULL,
 structure_below DOUBLE NULL, rise_trigger_above DOUBLE NULL, rise_invalidation_price DOUBLE NULL,
 last_review_at DATETIME(6) NULL, evidence_through DATETIME(6) NULL,
 endpoint_count SMALLINT UNSIGNED NOT NULL, entry_peer_count SMALLINT UNSIGNED NOT NULL,
 removal_peer_count SMALLINT UNSIGNED NOT NULL,
 CONSTRAINT contract_cycle FOREIGN KEY(cycle_id) REFERENCES research_cycles(id) ON DELETE CASCADE
) ENGINE=InnoDB''',
'''CREATE TABLE IF NOT EXISTS research_cycle_endpoints(
 cycle_id CHAR(16) CHARACTER SET ascii NOT NULL, horizon VARCHAR(8) CHARACTER SET ascii NOT NULL,
 sequence_no SMALLINT UNSIGNED NOT NULL, endpoint DATE NULL,
 PRIMARY KEY(cycle_id,horizon), KEY endpoint_day(endpoint,cycle_id),
 CONSTRAINT endpoint_cycle FOREIGN KEY(cycle_id) REFERENCES research_cycles(id) ON DELETE CASCADE
) ENGINE=InnoDB''',
'''CREATE TABLE IF NOT EXISTS research_cycle_references(
 cycle_id CHAR(16) CHARACTER SET ascii NOT NULL, reference_kind VARCHAR(8) CHARACTER SET ascii NOT NULL,
 present_fields SMALLINT UNSIGNED NOT NULL,
 fact_id BIGINT UNSIGNED NULL, quoted_at DATETIME(6) NULL, price DOUBLE NULL,
 confirmed_at DATETIME(6) NULL, price_basis VARCHAR(64) CHARACTER SET ascii NULL,
 symbol VARCHAR(12) CHARACTER SET ascii NULL, conditions_confirmed BOOLEAN NULL,
 adjustment_verified BOOLEAN NULL,
 PRIMARY KEY(cycle_id,reference_kind),
 CONSTRAINT reference_cycle FOREIGN KEY(cycle_id) REFERENCES research_cycles(id) ON DELETE CASCADE,
 CONSTRAINT reference_fact FOREIGN KEY(fact_id) REFERENCES facts(id)
) ENGINE=InnoDB''',
'''CREATE TABLE IF NOT EXISTS research_cycle_peer_references(
 cycle_id CHAR(16) CHARACTER SET ascii NOT NULL, reference_kind VARCHAR(8) CHARACTER SET ascii NOT NULL,
 sequence_no SMALLINT UNSIGNED NOT NULL, present_fields SMALLINT UNSIGNED NOT NULL,
 symbol VARCHAR(12) CHARACTER SET ascii NULL, fact_id BIGINT UNSIGNED NULL,
 quoted_at DATETIME(6) NULL, price DOUBLE NULL,
 PRIMARY KEY(cycle_id,reference_kind,sequence_no),
 CONSTRAINT peer_reference_cycle FOREIGN KEY(cycle_id) REFERENCES research_cycles(id) ON DELETE CASCADE,
 CONSTRAINT peer_reference_fact FOREIGN KEY(fact_id) REFERENCES facts(id)
) ENGINE=InnoDB''',
)

CONTRACT_FIELDS = ('entry','endpoints','entry_reference','removal_reference','entry_peer_refs','removal_peer_refs',
                   'session_policy','structure_guard','rise','published_at','adjustment_verified','last_review','invalidation')
CONTRACT_BITS = {name:1<<i for i,name in enumerate(CONTRACT_FIELDS)}
ENTRY_FIELDS = ('low','high','invalidate_below','condition')
SESSION_FIELDS = ('reference_from','action_valid_until')
REFERENCE_FIELDS = ('fact_id','at','price','confirmed_at','kind','symbol','conditions_confirmed','adjustment_verified')
PEER_FIELDS = ('symbol','fact_id','at','price')


def schema_sql():
    return '\n\n'.join([
        *('ALTER TABLE `'+table+'` '+', '.join('ADD COLUMN `'+name+'` '+kind for name,kind in columns.items())+';'
          for table,columns in COLUMNS.items()), DECISION_DDL+';', *(ddl+';' for ddl in CONTRACT_DDL),
    ])+'\n'


def apply_schema(conn):
    """Apply the fixed reviewed field definitions once, including old schemas."""
    for table,columns in COLUMNS.items():
        present={row[0] for row in conn.execute('SHOW COLUMNS FROM `'+table+'`')}
        missing={name:kind for name,kind in columns.items() if name not in present}
        if missing:
            conn.execute('ALTER TABLE `'+table+'` '+', '.join('ADD COLUMN `'+name+'` '+kind for name,kind in missing.items()))
    conn.execute(DECISION_DDL)
    for ddl in CONTRACT_DDL:
        conn.execute(ddl)


def sql_time(value):
    if value is None or value=='':
        return None
    result=datetime.fromisoformat(value) if isinstance(value,str) else value
    return result.astimezone(store.SHANGHAI).replace(tzinfo=None) if result.tzinfo else result


def iso_time(value):
    if value is None:
        return None
    value=datetime.fromisoformat(value) if isinstance(value,str) else value
    value=value.astimezone(store.SHANGHAI) if value.tzinfo else value.replace(tzinfo=store.SHANGHAI)
    return value.isoformat()


def _object(raw):
    return json.loads(raw) if isinstance(raw,(str,bytes,bytearray)) else raw


def _read(conn,table,where,params,data=None):
    row=conn.execute('SELECT data FROM `'+table+'` WHERE '+where,params).fetchone()
    if row is None:
        raise ValueError('Cannot synchronize a missing '+table+' record')
    stored=_object(row[0])
    if data is not None and store.dumps(stored)!=store.dumps(data):
        raise ValueError('Research document changed before relational state synchronization')
    return stored


def _update(conn,table,values,where,params):
    conn.execute('UPDATE `'+table+'` SET '+','.join('`'+name+'`=%s' for name in values)+' WHERE '+where,
                 (*values.values(),*params))


def sync_run(conn,run_id,data=None,report=None):
    row=conn.execute('SELECT data,report FROM research_runs WHERE id=%s',(run_id,)).fetchone()
    if row is None:
        raise ValueError('Cannot synchronize a missing research run')
    stored_data,stored_report=_object(row[0]),_object(row[1])
    if (data is not None and store.dumps(stored_data)!=store.dumps(data) or
            report is not None and store.dumps(stored_report)!=store.dumps(report)):
        raise ValueError('Research document changed before relational state synchronization')
    data,report=stored_data,stored_report or {}
    bundle=data.get('bundle') or {}
    target=report.get('forecast_target')
    decisions=report.get('decisions') or []
    _update(conn,'research_runs',{
        'session':data.get('session') or report.get('session') or 'close',
        'mode':report.get('mode'), 'published_at':sql_time(report.get('published_at')),
        'withdrawn_at':sql_time(data.get('withdrawn_at')),
        'study_horizon':report.get('study_horizon'),
        'target_version':target.get('version') if target is not None else None,
        'has_forecast_target':target is not None,
        'has_bundle':bundle.get('market') is not None,
        'market_coverage':(bundle.get('market') or {}).get('coverage'),
        'market_analysis':report.get('market_analysis'),'decision_count':len(decisions),
        'intent':data.get('intent'),'title':report.get('title'),
        'has_validated_report':data.get('validated_report') is not None,
        'stop_requested':bool(data.get('stop_requested')),
        'detail_compacted_at':data.get('detail_compacted_at'),
        'rotation_retry_not_before':(data.get('rotation') or {}).get('retry_not_before'),
    },'id=%s',(run_id,))
    conn.execute('DELETE FROM research_decisions WHERE run_id=%s',(run_id,))
    if decisions:
        conn.executemany('INSERT INTO research_decisions(run_id,sequence_no,symbol,name,action,cycle_id,review_event,rise_type,rise_name,stars) '
                         'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',[
            (run_id,index,decision['symbol'],decision.get('name') or decision['symbol'],decision['action'],decision.get('cycle_id'),
             bool(decision.get('review_event')),(decision.get('rise') or {}).get('type'),
             (decision.get('rise') or {}).get('name'),(decision.get('rating') or {}).get('stars'))
            for index,decision in enumerate(decisions)])
    from research import coverage
    coverage.persist(conn,run_id,data)


def sync_cycle(conn,cycle_id,data=None):
    data=_read(conn,'research_cycles','id=%s',(cycle_id,),data)
    pool=data.get('pool_review') or {}
    ends=[day for day in (data.get('endpoints') or {}).values() if day]
    _update(conn,'research_cycles',{
        'action':data.get('action'),'session':(data.get('session_policy') or {}).get('session') or 'close',
        'rise_type':(data.get('rise') or {}).get('type'),'rise_policy_version':data.get('rise_policy_version'),
        'method_version':data.get('method_version'),'thesis_key':data.get('thesis_key') or 'structure',
        'research_horizon':data.get('research_horizon') or 'D3',
        'first_removed_at':sql_time(data.get('first_removed_at')),'end_day':max(ends) if ends else None,
        'pool_review_day':pool.get('day'),'pool_review_fact_id':pool.get('fact_id'),
        'pool_review_outcome':pool.get('outcome'),'pool_review_reason':pool.get('reason'),
    },'id=%s',(cycle_id,))
    _sync_contract(conn,cycle_id,data)


def sync_outcome(conn,cycle_id,horizon,kind,data=None):
    values=(cycle_id,horizon,kind)
    data=_read(conn,'research_outcomes','cycle_id=%s AND horizon=%s AND kind=%s',values,data)
    _update(conn,'research_outcomes',{'direction':data.get('direction'),'return_value':data.get('return'),
                                    'excess_return':data.get('excess_return')},
            'cycle_id=%s AND horizon=%s AND kind=%s',values)


def sync_control(conn,control_id,data=None):
    data=_read(conn,'research_controls','id=%s',(control_id,),data)
    _update(conn,'research_controls',{'method_version':data.get('method'),'return_value':data.get('return_'),
                                    'target_version':data.get('target_version'),'reference_basis':data.get('reference_basis')},
            'id=%s',(control_id,))


def sync_event(conn,event_id,data=None):
    data=_read(conn,'research_events','id=%s',(event_id,),data)
    _update(conn,'research_events',{'to_state':data.get('to')},'id=%s',(event_id,))


def sync_notice(conn,notice_id,data=None):
    data=_read(conn,'research_notices','id=%s',(notice_id,),data)
    _update(conn,'research_notices',{'action':data.get('action'),'symbol':data.get('symbol')},'id=%s',(notice_id,))


def sync_call(conn,call_id,data=None):
    data=_read(conn,'research_calls','id=%s',(call_id,),data)
    measured=data.get('usage_normalized') or {}
    _update(conn,'research_calls',{
        'attempt_no':data.get('attempt') or 0,'recoverable':bool(data.get('recoverable')),
        'usage_state':data.get('usage_state'),
        'input_tokens':measured.get('input'),'output_tokens':measured.get('output'),
        'total_tokens':measured.get('total'),'cached_input_tokens':measured.get('cached_input'),
        'reasoning_output_tokens':measured.get('reasoning_output'),
    },'id=%s',(call_id,))


def _mask(value,fields):
    return sum(1<<index for index,key in enumerate(fields) if key in (value or {}))


def _upsert(conn,table,values):
    names=','.join('`'+name+'`' for name in values)
    conn.execute('INSERT INTO `'+table+'`('+names+') VALUES('+','.join(['%s']*len(values))+') '
                 'ON DUPLICATE KEY UPDATE '+','.join('`'+name+'`=VALUES(`'+name+'`)' for name in values),tuple(values.values()))


def _sync_contract(conn,cycle_id,data):
    entry=data.get('entry') or {}
    policy=data.get('session_policy') or {}
    guard=data.get('structure_guard') or {}
    rise=data.get('rise') or {}
    last=data.get('last_review') or {}
    endpoints=data.get('endpoints') or {}
    present=sum(CONTRACT_BITS[key] for key in CONTRACT_FIELDS if key in data)
    nulls=sum(CONTRACT_BITS[key] for key in CONTRACT_FIELDS if key in data and data[key] is None)
    values={
        'cycle_id':cycle_id,'present_fields':present,'null_fields':nulls,
        'entry_fields':_mask(entry,ENTRY_FIELDS),'session_fields':_mask(policy,SESSION_FIELDS),
        'structure_fields':_mask(guard,('below',)),'rise_fields':_mask(rise,('trigger_above','invalidation_price')),
        'last_review_fields':_mask(last,('at','evidence_through')),
        'entry_low':entry.get('low'),'entry_high':entry.get('high'),'invalidate_below':entry.get('invalidate_below'),
        'entry_condition':entry.get('condition'),'invalidation':data.get('invalidation'),
        'published_at':sql_time(data.get('published_at')),'adjustment_verified':data.get('adjustment_verified'),
        'reference_from':sql_time(policy.get('reference_from')),'action_valid_until':sql_time(policy.get('action_valid_until')),
        'structure_below':guard.get('below'),'rise_trigger_above':rise.get('trigger_above'),
        'rise_invalidation_price':rise.get('invalidation_price'),
        'last_review_at':sql_time(last.get('at')),'evidence_through':sql_time(last.get('evidence_through')),
        'endpoint_count':len(endpoints),'entry_peer_count':len(data.get('entry_peer_refs') or []),
        'removal_peer_count':len(data.get('removal_peer_refs') or []),
    }
    prior=conn.execute('SELECT * FROM research_cycle_contracts WHERE cycle_id=%s',(cycle_id,)).fetchone()
    if prior:
        # A later review may append evidence and references, not move the original
        # entry prices, participation window or forecast endpoint.
        immutable=('entry_fields','session_fields','structure_fields','rise_fields','entry_low','entry_high',
                   'invalidate_below','entry_condition','invalidation','published_at','adjustment_verified',
                   'reference_from','action_valid_until','structure_below','rise_trigger_above','rise_invalidation_price')
        fixed_bits=sum(CONTRACT_BITS[key] for key in ('entry','endpoints','session_policy','structure_guard','rise',
                                                     'published_at','adjustment_verified','invalidation'))
        if any(prior[key]!=values[key] for key in immutable) or any(
                (prior[field] & fixed_bits)!=(values[field] & fixed_bits) for field in ('present_fields','null_fields')):
            raise ValueError('原参与条件和窗口已冻结，须建立新的研究周期，不能修改原合同')
        old_ends={row['horizon']:row['endpoint'] for row in conn.execute(
            'SELECT horizon,endpoint FROM research_cycle_endpoints WHERE cycle_id=%s',(cycle_id,))}
        if old_ends!=endpoints:
            raise ValueError('原评价终点已冻结，不能后移、删除或补写终点')
    for kind in ('entry','removal'):
        old=conn.execute('SELECT * FROM research_cycle_references WHERE cycle_id=%s AND reference_kind=%s',
                         (cycle_id,kind)).fetchone()
        if old and _reference_value(old,REFERENCE_FIELDS):
            new=data.get(kind+'_reference')
            previous=_reference_value(old,REFERENCE_FIELDS)
            if new is None or set(previous)!=set(new) or any(
                    (sql_time(previous[key])!=sql_time(new[key]) if key in ('at','confirmed_at') else previous[key]!=new[key])
                    for key in previous):
                raise ValueError('首个合格参考报价已冻结，不能清除或重新登记')
    _upsert(conn,'research_cycle_contracts',values)
    conn.execute('DELETE FROM research_cycle_endpoints WHERE cycle_id=%s',(cycle_id,))
    if endpoints:
        conn.executemany('INSERT INTO research_cycle_endpoints(cycle_id,horizon,sequence_no,endpoint) VALUES(%s,%s,%s,%s)',
                         [(cycle_id,horizon,index,endpoint) for index,(horizon,endpoint) in enumerate(endpoints.items())])
    conn.execute('DELETE FROM research_cycle_references WHERE cycle_id=%s',(cycle_id,))
    conn.execute('DELETE FROM research_cycle_peer_references WHERE cycle_id=%s',(cycle_id,))
    for kind in ('entry','removal'):
        reference=data.get(kind+'_reference')
        if reference is not None:
            _upsert(conn,'research_cycle_references',{
                'cycle_id':cycle_id,'reference_kind':kind,'present_fields':_mask(reference,REFERENCE_FIELDS),
                'fact_id':reference.get('fact_id'),'quoted_at':sql_time(reference.get('at')),'price':reference.get('price'),
                'confirmed_at':sql_time(reference.get('confirmed_at')),'price_basis':reference.get('kind'),
                'symbol':reference.get('symbol'),'conditions_confirmed':reference.get('conditions_confirmed'),
                'adjustment_verified':reference.get('adjustment_verified'),
            })
        peers=data.get(kind+'_peer_refs') or []
        if peers:
            conn.executemany('INSERT INTO research_cycle_peer_references(cycle_id,reference_kind,sequence_no,present_fields,symbol,fact_id,quoted_at,price) '
                             'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',[
                (cycle_id,kind,index,_mask(peer,PEER_FIELDS),peer.get('symbol'),peer.get('fact_id'),
                 sql_time(peer.get('at')),peer.get('price')) for index,peer in enumerate(peers)])


def _fixed_object(original,fields,mask,values):
    result={key:copy.deepcopy(value) for key,value in (original or {}).items() if key not in fields}
    result.update({key:values[index] for index,key in enumerate(fields) if mask & 1<<index})
    return result


def _reference_value(row,fields):
    values={'fact_id':row['fact_id'],'at':iso_time(row['quoted_at']),'price':row['price']}
    if 'confirmed_at' in fields:
        values.update(confirmed_at=iso_time(row['confirmed_at']),kind=row['price_basis'],symbol=row['symbol'],
                      conditions_confirmed=bool(row['conditions_confirmed']) if row['conditions_confirmed'] is not None else None,
                      adjustment_verified=bool(row['adjustment_verified']) if row['adjustment_verified'] is not None else None)
    else:
        values['symbol']=row['symbol']
    return {key:values[key] for index,key in enumerate(fields) if row['present_fields'] & 1<<index}


def overlay_cycles(conn,rows):
    """Read fixed cycle contracts in bounded batches from their business tables."""
    rows=[dict(row) for row in rows]
    if not rows:
        return []
    identifiers=tuple(row['id'] for row in rows)
    placeholders=','.join(['%s']*len(identifiers))
    contracts={row['cycle_id']:dict(row) for row in conn.execute(
        'SELECT * FROM research_cycle_contracts WHERE cycle_id IN ('+placeholders+')',identifiers)}
    endpoints={identifier:{} for identifier in identifiers}
    for row in conn.execute('SELECT cycle_id,horizon,endpoint FROM research_cycle_endpoints '
                            'WHERE cycle_id IN ('+placeholders+') ORDER BY cycle_id,sequence_no',identifiers):
        endpoints[row['cycle_id']][row['horizon']]=row['endpoint']
    references={(row['cycle_id'],row['reference_kind']):_reference_value(row,REFERENCE_FIELDS)
                for row in conn.execute('SELECT * FROM research_cycle_references WHERE cycle_id IN ('+placeholders+')',identifiers)}
    peers={(identifier,kind):[] for identifier in identifiers for kind in ('entry','removal')}
    for row in conn.execute('SELECT * FROM research_cycle_peer_references WHERE cycle_id IN ('+placeholders+') '
                            'ORDER BY cycle_id,reference_kind,sequence_no',identifiers):
        peers[row['cycle_id'],row['reference_kind']].append(_reference_value(row,PEER_FIELDS))
    result=[]
    for row in rows:
        identifier=row['id']
        if identifier not in contracts:
            raise RuntimeError('周期关系式合同缺失，尚不能按旧文档继续执行：'+identifier)
        contract=contracts[identifier]
        data=copy.deepcopy(_object(row['data']))
        fixed={
            'entry':_fixed_object(data.get('entry'),ENTRY_FIELDS,contract['entry_fields'],
                                  (contract['entry_low'],contract['entry_high'],contract['invalidate_below'],contract['entry_condition'])),
            'session_policy':_fixed_object(data.get('session_policy'),SESSION_FIELDS,contract['session_fields'],
                                           (iso_time(contract['reference_from']),iso_time(contract['action_valid_until']))),
            'structure_guard':_fixed_object(data.get('structure_guard'),('below',),contract['structure_fields'],(contract['structure_below'],)),
            'rise':_fixed_object(data.get('rise'),('trigger_above','invalidation_price'),contract['rise_fields'],
                                 (contract['rise_trigger_above'],contract['rise_invalidation_price'])),
            'last_review':_fixed_object(data.get('last_review'),('at','evidence_through'),contract['last_review_fields'],
                                        (iso_time(contract['last_review_at']),iso_time(contract['evidence_through']))),
            'endpoints':endpoints[identifier],'published_at':iso_time(contract['published_at']),
            'adjustment_verified':bool(contract['adjustment_verified']) if contract['adjustment_verified'] is not None else None,
            'invalidation':contract['invalidation'],
        }
        if len(endpoints[identifier])!=contract['endpoint_count']:
            raise RuntimeError('周期关系式终点记录不完整：'+identifier)
        for kind in ('entry','removal'):
            key=kind+'_reference'
            required=bool(contract['present_fields'] & CONTRACT_BITS[key] and not contract['null_fields'] & CONTRACT_BITS[key])
            if required and (identifier,kind) not in references:
                raise RuntimeError('周期关系式参考报价记录不完整：'+identifier)
            fixed[key]=references.get((identifier,kind))
            fixed[kind+'_peer_refs']=peers[identifier,kind]
            if len(peers[identifier,kind])!=contract[kind+'_peer_count']:
                raise RuntimeError('周期关系式同行参考报价记录不完整：'+identifier)
        for key,value in fixed.items():
            if not contract['present_fields'] & CONTRACT_BITS[key]:
                data.pop(key,None)
            else:
                data[key]=None if contract['null_fields'] & CONTRACT_BITS[key] else value
        if 'first_removed_at' in data or row.get('first_removed_at') is not None:
            data['first_removed_at']=iso_time(row['first_removed_at'])
        result.append({**row,'data':data})
    return result


def overlay_cycle(conn,row):
    return overlay_cycles(conn,[row])[0]


def load_cycle_data(conn,cycle):
    if isinstance(cycle,str):
        cycle=conn.execute('SELECT * FROM research_cycles WHERE id=%s',(cycle,)).fetchone()
        if cycle is None:
            raise ValueError('Research cycle does not exist')
    return overlay_cycle(conn,cycle)['data']
