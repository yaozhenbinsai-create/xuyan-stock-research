"""Typed business records behind the immutable ``facts`` provenance catalogue.

The catalogue owns day/symbol/key and keeps its original fact IDs.  Business
values live in dedicated relational tables; no serialized payload is stored.
Presence masks distinguish an absent key from an explicitly unknown value.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import math
import re

from foundation import database as store, storage_policy


DATASET_TABLES = {
    'calendar': 'calendar_records', 'basics': 'security_records',
    'industry': 'classification_records', 'concept': 'classification_records',
    'area': 'classification_records', 'daily': 'daily_bars',
    'quotes': 'quote_snapshots', 'minutes': 'minute_bars',
    'financials': 'financial_reports', 'announcements': 'announcements',
    'policies': 'policy_documents', 'actions': 'corporate_actions',
    'fund_flows': 'fund_flow_records',
}

# Types govern both persistence and the generated, checked-in SQL schema.
SPECS = {
    'calendar_records': {'trading': 'bool'},
    'security_details': {'name': 'str', 'listed': 'date', 'delisted': 'date', 'status': 'str', 'board': 'str'},
    'classification_values': {'group': 'str', 'taxonomy': 'str', 'source_effective_date': 'date',
                              'historical_effective_unknown': 'bool', 'unclassified': 'bool'},
    'daily_bars': {**dict.fromkeys(('open','high','low','close','preclose','volume','amount','turnover','pe','pb'), 'number'),
                   'suspended':'bool', 'is_st':'bool', 'adjust':'str', 'volume_unit':'str'},
    'quote_snapshots': {**dict.fromkeys(('open','high','low','close','preclose','volume','amount','turnover','pe','pb'), 'number'),
                        'name':'str', 'source_time':'time', 'volume_unit':'str'},
    'minute_bars': {**dict.fromkeys(('open','high','low','close','volume','amount'), 'number'),
                    'adjust':'str', 'source_time':'time', 'period':'integer', 'closed':'bool', 'volume_unit':'str'},
    'financial_reports': {'report_type':'str', 'report_date':'date', 'published':'date'},
    'announcements': {'title':'text', 'published':'time', 'url':'text', 'text':'text', 'text_extracted_at':'time',
                      'text_sha256':'hash', 'text_truncated':'bool', 'document_sha256':'hash',
                      'text_pages':'integer', 'total_pages':'integer', 'text_scope':'text'},
    'policy_documents': {'title':'text', 'published':'time', 'url':'text', 'text':'text',
                         'document_sha256':'hash', 'effective_day':'date', 'text_scope':'text'},
    'corporate_actions': {'record_kind':'str', 'coverage_from':'date', 'coverage_through':'date', 'event_count':'integer'},
    'fund_flow_records': {**dict.fromkeys(('net_main','net_large','net_super','net_medium','net_small','ratio_main'), 'number'),
                          'unit':'str', 'basis':'str', 'source_time':'time', 'time_precision':'str'},
}
DIMENSIONS = {'basics': ('security_records','security_id','security_details'),
              **{k:('classification_records','classification_id','classification_values')
                 for k in ('industry','concept','area')}}
CHILD_TABLES = {'financials':'financial_values', 'actions':'action_values'}
FIELDS = {table: tuple(sorted(fields)) for table, fields in SPECS.items()}
FIELDS['calendar_records'] = tuple(sorted((*FIELDS['calendar_records'], 'markets')))
for _table in ('financial_reports','corporate_actions'):
    FIELDS[_table] = tuple(sorted((*FIELDS[_table], 'fields')))
BITS = {table:{field:1 << index for index,field in enumerate(fields)} for table,fields in FIELDS.items()}
TABLES = ('security_details','classification_values','calendar_records','calendar_markets',
          'security_records','classification_records','daily_bars','quote_snapshots','minute_bars',
          'financial_reports','financial_values','announcements','policy_documents',
          'corporate_actions','action_values','fund_flow_records')
SHANGHAI = timezone(timedelta(hours=8))
_SQL_NAME = re.compile(r'^[a-z][a-z0-9_]*$')


def typed_join(dataset, alias='r', detail_alias='v', fact_alias='f'):
    """SQL joins plus the alias containing the business columns, all fixed names."""
    if any(not _SQL_NAME.fullmatch(v) for v in (alias, detail_alias, fact_alias)):
        raise ValueError('SQL alias is invalid')
    table = DATASET_TABLES[dataset]
    sql = f' JOIN `{table}` {alias} ON {alias}.fact_id={fact_alias}.id '
    if dataset in DIMENSIONS:
        _, key, dimension = DIMENSIONS[dataset]
        sql += f' JOIN `{dimension}` {detail_alias} ON {detail_alias}.id={alias}.{key} '
        return sql, detail_alias
    return sql, alias


def _time_encode(value):
    if not isinstance(value, str):
        raise ValueError('A business timestamp must be a string or null')
    if not value:
        return None, 0, 0, ''
    offset = ''
    if value.isascii() and value.isdigit() and len(value) in (8,12,14,15,16,17,18,19,20):
        length = len(value)
        base = value[:14].ljust(14,'0')
        stamp = datetime.strptime(base,'%Y%m%d%H%M%S')
        digits = max(0,length-14)
        if digits:
            stamp = stamp.replace(microsecond=int(value[14:].ljust(6,'0')))
        return stamp, 2, length, offset
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return datetime.combine(date.fromisoformat(value),datetime.min.time()), 1, 0, offset
    match = re.fullmatch(r'\d{4}-\d{2}-\d{2}([T ])\d{2}:\d{2}(?::\d{2})?(?:\.(\d{1,6}))?(Z|[+-]\d{2}:?\d{2})?',value)
    if not match:
        raise ValueError('Business timestamp format is unsupported: '+value[:40])
    stamp = datetime.fromisoformat(value.replace('Z','+00:00'))
    offset = match[3] or ''
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(SHANGHAI).replace(tzinfo=None)
    seconds = bool(re.search(r':\d{2}:\d{2}',value))
    style = (3 if match[1]=='T' else 4) + (0 if seconds else 2)
    return stamp, style, len(match[2] or ''), offset


def _time_decode(value, style, precision, offset):
    if style == 0:
        return ''
    if isinstance(value,str):
        value = datetime.fromisoformat(value)
    if style == 1:
        return value.date().isoformat()
    if style == 2:
        return (value.strftime('%Y%m%d%H%M%S')+f'{value.microsecond:06d}')[:precision]
    if offset:
        cleaned = offset.replace(':','')
        minutes = 0 if offset=='Z' else (1 if cleaned[0]=='+' else -1)*(int(cleaned[1:3])*60+int(cleaned[3:5]))
        value = value.replace(tzinfo=SHANGHAI).astimezone(timezone(timedelta(minutes=minutes)))
    seconds = style in (3,4)
    text = value.strftime('%Y-%m-%d'+('T' if style in (3,5) else ' ')+'%H:%M'+(':%S' if seconds else ''))
    if precision:
        text += '.'+f'{value.microsecond:06d}'[:precision]
    return text+offset


def _encode(table, payload):
    result = {'present_fields':sum(BITS[table][key] for key in FIELDS[table] if key in payload),
              'integer_fields':0, 'empty_fields':0, 'negative_zero_fields':0}
    for field, kind in SPECS[table].items():
        value = payload.get(field)
        if value is None:
            result[field] = None
            continue
        bit = BITS[table][field]
        if kind == 'number':
            if type(value) not in (int,float) or not math.isfinite(value):
                raise ValueError('Non-numeric market field: '+field)
            result[field] = value
            if type(value) is int:
                result['integer_fields'] |= bit
                if abs(value)>2**53:
                    if len(str(abs(value)))>65:
                        raise ValueError('Integer exceeds the exact SQL range: '+field)
                    result[field+'_exact_integer'] = value
            elif value == 0 and math.copysign(1.,value)<0:
                result['negative_zero_fields'] |= bit
        elif kind == 'integer':
            if type(value) is not int or not -(2**63)<=value<2**63:
                raise ValueError('Expected integer field: '+field)
            result[field] = value
        elif kind == 'bool':
            if type(value) not in (bool,int) or value not in (0,1):
                raise ValueError('Expected boolean field: '+field)
            result[field] = int(value)
            if type(value) is int:
                result['integer_fields'] |= bit
        elif kind == 'date':
            if value == '':
                result[field] = None
                result['empty_fields'] |= bit
            else:
                result[field] = date.fromisoformat(value)
        elif kind == 'time':
            stamp, style, precision, offset = _time_encode(value)
            result.update({field:stamp, field+'_style':style, field+'_precision':precision, field+'_offset':offset})
        else:
            if not isinstance(value,str):
                raise ValueError('Expected text field: '+field)
            result[field] = value
    return result


def _decode(table, row):
    result = {}
    present = int(row['present_fields'])
    for field,kind in SPECS[table].items():
        bit = BITS[table][field]
        if not present & bit:
            continue
        value = row[field]
        if kind=='date' and int(row['empty_fields']) & bit:
            value = ''
        elif kind=='time' and row.get(field+'_style')==0:
            value = ''
        elif value is not None:
            if kind=='number':
                if int(row['integer_fields']) & bit:
                    value = int(row.get(field+'_exact_integer') if row.get(field+'_exact_integer') is not None else value)
                elif int(row['negative_zero_fields']) & bit:
                    value = -0.0
                else:
                    value = float(value)
            elif kind=='bool':
                value = int(value) if int(row['integer_fields']) & bit else bool(value)
            elif kind=='integer':
                value = int(value)
            elif kind=='date':
                value = value.isoformat() if isinstance(value,date) else str(value)
            elif kind=='time':
                value = _time_decode(value,row[field+'_style'],row[field+'_precision'],row[field+'_offset'])
        result[field] = value
    return result


def _insert_many(conn, table, rows):
    if not rows:
        return
    keys = tuple(sorted(set().union(*(row.keys() for row in rows))))
    columns = ','.join('`'+key+'`' for key in keys)
    # A fact already has an immutable content fingerprint; migration can safely
    # resume the same fact.  The catalogue caller is responsible for that key.
    updates = ','.join(f'`{key}`=VALUES(`{key}`)' for key in keys if key not in ('fact_id','position','field_name'))
    sql = f'INSERT INTO `{table}` ({columns}) VALUES ('+','.join(['%s']*len(keys))+')'
    if updates:
        sql += ' ON DUPLICATE KEY UPDATE '+updates
    conn.executemany(sql,[tuple(row.get(key) for key in keys) for row in rows])


def _scalar(field, value):
    row = {'field_name':field,'value_kind':'null','text_value':None,'number_value':None,
           'integer_value':None,'boolean_value':None,'numeric_value':None}
    if value is None:
        return row
    if isinstance(value,str):
        row.update(value_kind='text',text_value=value)
        try:
            decimal = Decimal(value)
            if decimal.is_finite() and decimal.adjusted()<35 and decimal.as_tuple().exponent>=-30:
                row['numeric_value'] = decimal
        except InvalidOperation:
            pass
    elif type(value) is bool:
        row.update(value_kind='boolean',boolean_value=int(value))
    elif type(value) is int and len(str(abs(value)))<=65:
        row.update(value_kind='integer',integer_value=value)
    elif type(value) is float and math.isfinite(value):
        row.update(value_kind='number',number_value=value)
        if value==0 and math.copysign(1.,value)<0:
            row['value_kind']='negative_zero'
    else:
        raise ValueError('Financial/action fields must contain scalar values: '+field)
    return row


def _scalar_decode(row):
    kind = row['value_kind']
    if kind == 'null':return None
    if kind == 'text':return row['text_value']
    if kind == 'boolean':return bool(row['boolean_value'])
    if kind == 'integer':return int(row['integer_value'])
    if kind == 'negative_zero':return -0.0
    return float(row['number_value'])


def save(conn, fact_id, dataset, payload):
    save_many(conn,[(fact_id,dataset,payload)])


def save_many(conn, items):
    """Persist (fact_id, dataset, payload) tuples in the caller's transaction."""
    grouped = defaultdict(list)
    dimensions = defaultdict(dict)
    children = defaultdict(list)
    for fact_id,dataset,payload in items:
        if dataset not in storage_policy.FIELDS or not isinstance(payload,dict):
            raise ValueError('Unsupported typed fact payload: '+str(dataset))
        table = DATASET_TABLES[dataset]
        business_table = DIMENSIONS[dataset][2] if dataset in DIMENSIONS else table
        mapped = set(FIELDS[business_table]) & storage_policy.FIELDS[dataset]
        unknown = set(payload)-mapped-{'day','symbol','key'}
        if unknown:
            raise ValueError('Unmapped typed fact fields: '+','.join(sorted(unknown)))
        if not {'day','symbol','key'}<=payload.keys():
            raise ValueError('Typed facts require their original day, symbol and key')
        # New acquisitions are normalized by assets before their fingerprint is
        # fixed.  Migration passes an already-saved historical shape, including
        # explicit null pe/pb.  Re-normalizing here would rewrite that history.
        value = dict(payload)
        if dataset in DIMENSIONS:
            table,foreign,dimension = DIMENSIONS[dataset]
            content = {key:value[key] for key in SPECS[dimension] if key in value}
            digest = hashlib.sha256(store.dumps(content).encode()).digest()
            dimensions[dimension][digest] = _encode(dimension,content)
            grouped[table].append({'fact_id':fact_id,foreign:digest})
            continue
        row = {'fact_id':fact_id,**_encode(table,value)}
        if dataset=='calendar':
            markets = value.get('markets')
            row['markets_null'] = markets is None
            if markets is not None:
                if not isinstance(markets,list) or any(not isinstance(m,str) for m in markets):
                    raise ValueError('Calendar markets must be a list of market names')
                children['calendar_markets'].extend({'fact_id':fact_id,'position':i,'market':market} for i,market in enumerate(markets))
        elif dataset in CHILD_TABLES:
            fields = value.get('fields')
            row['fields_null'] = fields is None
            if fields is not None:
                if not isinstance(fields,dict):
                    raise ValueError('Financial/action fields must be an object')
                children[CHILD_TABLES[dataset]].extend({'fact_id':fact_id,**_scalar(key,val)} for key,val in fields.items())
        grouped[table].append(row)
    for dimension,values in dimensions.items():
        _insert_many(conn,dimension,[{'content_hash':digest,**data} for digest,data in values.items()])
        mapping = {}
        hashes = list(values)
        for start in range(0,len(hashes),500):
            batch = hashes[start:start+500]
            for row in conn.execute(f'SELECT id,content_hash FROM `{dimension}` WHERE content_hash IN ('+','.join(['%s']*len(batch))+')',tuple(batch)):
                mapping[bytes(row['content_hash'])] = row['id']
        table,foreign = ('security_records','security_id') if dimension=='security_details' else ('classification_records','classification_id')
        for row in grouped[table]:
            row[foreign] = mapping[row[foreign]]
    for table,rows in grouped.items():
        _insert_many(conn,table,rows)
    for table,rows in children.items():
        _insert_many(conn,table,rows)


def load(conn, fact_id):
    values = load_many(conn,[fact_id])
    if fact_id not in values:
        raise KeyError('Unknown fact ID: '+str(fact_id))
    return values[fact_id]


def load_many(conn, ids):
    """Return normalized payloads keyed by ID, using bounded typed-table queries."""
    ids = list(dict.fromkeys(int(value) for value in ids))
    result = {}
    for start in range(0,len(ids),500):
        batch = ids[start:start+500]
        marks = ','.join(['%s']*len(batch))
        facts = [dict(row) for row in conn.execute('SELECT id,dataset,day,symbol,record_key FROM facts WHERE id IN ('+marks+')',tuple(batch))]
        groups = defaultdict(list)
        for fact in facts:
            groups[fact['dataset']].append(fact)
        for dataset,records in groups.items():
            table = DATASET_TABLES[dataset]
            record_ids = tuple(row['id'] for row in records)
            marks = ','.join(['%s']*len(record_ids))
            if dataset in DIMENSIONS:
                _,foreign,dimension = DIMENSIONS[dataset]
                sql = f'SELECT r.fact_id,v.* FROM `{table}` r JOIN `{dimension}` v ON v.id=r.{foreign} WHERE r.fact_id IN ({marks})'
                decode_table = dimension
            else:
                sql = f'SELECT * FROM `{table}` WHERE fact_id IN ({marks})'
                decode_table = table
            values = {row['fact_id']:dict(row) for row in conn.execute(sql,record_ids)}
            detail = defaultdict(dict)
            markets = defaultdict(list)
            if dataset in CHILD_TABLES:
                for row in conn.execute(f'SELECT * FROM `{CHILD_TABLES[dataset]}` WHERE fact_id IN ({marks}) ORDER BY fact_id,field_name',record_ids):
                    detail[row['fact_id']][row['field_name']] = _scalar_decode(row)
            elif dataset=='calendar':
                for row in conn.execute(f'SELECT fact_id,market FROM calendar_markets WHERE fact_id IN ({marks}) ORDER BY fact_id,position',record_ids):
                    markets[row['fact_id']].append(row['market'])
            for fact in records:
                fid = fact['id']
                if fid not in values:
                    raise RuntimeError(f'Typed {dataset} record is missing for fact {fid}')
                row = values[fid]
                payload = {'day':fact['day'].isoformat() if isinstance(fact['day'],date) else fact['day'],
                           'symbol':fact['symbol'],'key':fact['record_key'],**_decode(decode_table,row)}
                if dataset in CHILD_TABLES and int(row['present_fields']) & BITS[table]['fields']:
                    payload['fields'] = None if row['fields_null'] else detail[fid]
                elif dataset=='calendar' and int(row['present_fields']) & BITS[table]['markets']:
                    payload['markets'] = None if row['markets_null'] else markets[fid]
                result[fid] = payload
    return result


def hydrate(conn, rows):
    """Attach a payload dict to each metadata row without changing its order."""
    rows = [dict(row) for row in rows]
    ids = [row.get('id',row.get('fact_id')) for row in rows]
    if any(identifier is None for identifier in ids):
        raise ValueError('Hydration requires fact id metadata')
    values = load_many(conn,ids)
    return [{**row,'payload':values[identifier]} for row,identifier in zip(rows,ids)]


def schema_sql():
    """Deterministic source for records_schema.sql; no database access."""
    statements = []
    for table,fields in SPECS.items():
        dimension = table in ('security_details','classification_values')
        columns = ['id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT','content_hash BINARY(32) NOT NULL UNIQUE'] if dimension else ['fact_id BIGINT UNSIGNED PRIMARY KEY']
        columns += ['present_fields BIGINT UNSIGNED NOT NULL','integer_fields BIGINT UNSIGNED NOT NULL DEFAULT 0',
                    'empty_fields BIGINT UNSIGNED NOT NULL DEFAULT 0','negative_zero_fields BIGINT UNSIGNED NOT NULL DEFAULT 0']
        for field,kind in fields.items():
            sql_type = {'number':'DOUBLE','integer':'BIGINT','bool':'BOOLEAN','date':'DATE','time':'DATETIME(6)',
                        'str':'VARCHAR(512)','text':'MEDIUMTEXT','hash':'CHAR(64) CHARACTER SET ascii'}[kind]
            columns.append(f'`{field}` {sql_type} NULL')
            if kind=='number':
                columns.append(f'`{field}_exact_integer` DECIMAL(65,0) NULL')
            elif kind=='time':
                columns += [f'`{field}_style` TINYINT UNSIGNED NULL',f'`{field}_precision` TINYINT UNSIGNED NULL',
                            f'`{field}_offset` VARCHAR(6) CHARACTER SET ascii NULL']
        if table=='calendar_records':columns.append('markets_null BOOLEAN NOT NULL DEFAULT 0')
        if table in ('financial_reports','corporate_actions'):columns.append('fields_null BOOLEAN NOT NULL DEFAULT 0')
        if not dimension:
            columns.append(f'CONSTRAINT `{table}_fact` FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE')
        if table=='quote_snapshots':columns.append('KEY quote_time(source_time,fact_id)')
        if table=='minute_bars':columns.append('KEY minute_time(source_time,fact_id)')
        if table=='announcements':columns.append('KEY announcement_published(published,fact_id)')
        statements.append('CREATE TABLE IF NOT EXISTS `'+table+'` (\n '+',\n '.join(columns)+'\n) ENGINE=InnoDB;')
    for table,foreign,dimension in (DIMENSIONS['basics'],DIMENSIONS['industry']):
        statements.append(f'CREATE TABLE IF NOT EXISTS `{table}` (\n fact_id BIGINT UNSIGNED PRIMARY KEY,\n {foreign} BIGINT UNSIGNED NOT NULL,\n'
                          f' CONSTRAINT `{table}_fact` FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE,\n'
                          f' CONSTRAINT `{table}_detail` FOREIGN KEY({foreign}) REFERENCES `{dimension}`(id)\n) ENGINE=InnoDB;')
    statements.append('CREATE TABLE IF NOT EXISTS calendar_markets (\n fact_id BIGINT UNSIGNED NOT NULL,\n position SMALLINT UNSIGNED NOT NULL,\n market VARCHAR(32) NOT NULL,\n PRIMARY KEY(fact_id,position),\n CONSTRAINT calendar_market_record FOREIGN KEY(fact_id) REFERENCES calendar_records(fact_id) ON DELETE CASCADE\n) ENGINE=InnoDB;')
    for table,parent in (('financial_values','financial_reports'),('action_values','corporate_actions')):
        statements.append(f'CREATE TABLE IF NOT EXISTS `{table}` (\n fact_id BIGINT UNSIGNED NOT NULL,\n field_name VARCHAR(128) NOT NULL,\n'
                          " value_kind ENUM('null','text','number','integer','boolean','negative_zero') NOT NULL,\n"
                          ' text_value MEDIUMTEXT NULL,\n number_value DOUBLE NULL,\n integer_value DECIMAL(65,0) NULL,\n'
                          ' boolean_value BOOLEAN NULL,\n numeric_value DECIMAL(65,30) NULL,\n PRIMARY KEY(fact_id,field_name),\n'
                          f' CONSTRAINT `{table}_record` FOREIGN KEY(fact_id) REFERENCES `{parent}`(fact_id) ON DELETE CASCADE\n) ENGINE=InnoDB;')
    return '-- Typed business facts. Generated from foundation.record_store.schema_sql().\n'+'\n\n'.join(statements)+'\n'
