"""Relational scheduling and progress columns for durable collection requests.

``data`` remains the original request/work-list document.  SQL scheduling and
UI progress use the explicit columns, synchronized in the same transaction as
each document write.  Schema upgrades are invoked by the migration owner.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import math

from foundation import database as store


JOB_COLUMNS = {
    'retry_at':'DOUBLE NOT NULL DEFAULT 0',
    'expires_at':'DATETIME(6) NULL',
    'trigger':"VARCHAR(32) CHARACTER SET ascii NOT NULL DEFAULT ''",
    'last_yield':'DOUBLE NOT NULL DEFAULT 0',
    'done':'INT UNSIGNED NOT NULL DEFAULT 0',
    'total':'INT UNSIGNED NOT NULL DEFAULT 0',
    'added':'INT UNSIGNED NOT NULL DEFAULT 0',
    'unchanged':'INT UNSIGNED NOT NULL DEFAULT 0',
    'received':'INT UNSIGNED NOT NULL DEFAULT 0',
    'failed':'INT UNSIGNED NOT NULL DEFAULT 0',
    'no_data':'INT UNSIGNED NOT NULL DEFAULT 0',
    'reused':'INT UNSIGNED NOT NULL DEFAULT 0',
    'retries':'INT UNSIGNED NOT NULL DEFAULT 0',
    'deferred_retries':'INT UNSIGNED NOT NULL DEFAULT 0',
    'source':'VARCHAR(48) CHARACTER SET ascii NULL',
    'dataset':'VARCHAR(24) CHARACTER SET ascii NULL',
    'schedule_id':'VARCHAR(48) CHARACTER SET ascii NULL',
    'mode':"VARCHAR(12) CHARACTER SET ascii NOT NULL DEFAULT 'missing'",
    'name':"VARCHAR(200) NOT NULL DEFAULT ''",
    'message':'TEXT NULL',
    'updated':'VARCHAR(32) CHARACTER SET ascii NULL',
    'range_start':'DATE NULL',
    'range_end':'DATE NULL',
}
SCHEDULE_COLUMNS = {
    'last_job':'CHAR(16) CHARACTER SET ascii NULL',
    'last_slot':'VARCHAR(64) CHARACTER SET ascii NULL',
    'source':'VARCHAR(48) CHARACTER SET ascii NULL',
    'kind':'VARCHAR(24) CHARACTER SET ascii NULL',
    'enabled':'BOOLEAN NOT NULL DEFAULT 0',
    'trading_only':'BOOLEAN NOT NULL DEFAULT 0',
    'run_at':'TIME NULL',
    'interval_seconds':'INT UNSIGNED NOT NULL DEFAULT 0',
}
COUNTERS = ('done','total','added','unchanged','received','failed','no_data','reused','retries','deferred_retries')
JOB_SUMMARY_COLUMNS = ','.join('`'+key+'`' for key in ('id','identity','status','created',*JOB_COLUMNS))


def local_datetime(value):
    """Normalize an ISO deadline to a naive Shanghai wall-clock DATETIME."""
    if value in (None,''):
        return None
    stamp = value if isinstance(value,datetime) else datetime.fromisoformat(value.replace('Z','+00:00'))
    return stamp.astimezone(store.SHANGHAI).replace(tzinfo=None) if stamp.tzinfo else stamp


def expired(deadline, now=None):
    stamp = local_datetime(deadline)
    return stamp is not None and local_datetime(now or store.now())>=stamp


def _document(value):
    result = json.loads(value) if isinstance(value,(str,bytes)) else value
    if not isinstance(result,dict):
        raise ValueError('Collection state document must be an object')
    return result


def _counter(value, name):
    if type(value) is not int or not 0<=value<=2**32-1:
        raise ValueError('Collection counter is invalid: '+name)
    return value


def _clock(value, name):
    if type(value) not in (int,float) or not math.isfinite(value) or value<0:
        raise ValueError('Collection clock is invalid: '+name)
    return float(value)


def sync_job(conn, identifier, data=None):
    if data is None:
        row = conn.execute('SELECT data FROM collection_jobs WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
        if not row:
            raise KeyError('Unknown collection job: '+identifier)
        data = row[0]
    data = _document(data)
    values = {key:_counter(data.get(key,0),key) for key in COUNTERS}
    values.update(retry_at=_clock(data.get('retry_at',0),'retry_at'),
                  expires_at=local_datetime(data.get('expires_at')),
                  trigger=data.get('trigger',''),last_yield=_clock(data.get('last_yield',0),'last_yield'),
                  source=data.get('source'),dataset=data.get('dataset'),schedule_id=data.get('schedule_id'),
                  mode=data.get('mode','missing'),name=data.get('name',''),message=data.get('message'),
                  updated=data.get('updated') or data.get('created'),
                  range_start=data.get('start'),range_end=data.get('end'))
    if 'status' in data:
        values['status'] = data['status']
    keys = tuple(values)
    return conn.execute('UPDATE collection_jobs SET '+','.join('`'+key+'`=%s' for key in keys)+' WHERE id=%s',
                        (*[values[key] for key in keys],identifier)).rowcount


def sync_schedule(conn, identifier, data=None):
    if data is None:
        row = conn.execute('SELECT data FROM schedules WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
        if not row:
            raise KeyError('Unknown collection schedule: '+identifier)
        data = row[0]
    data = _document(data)
    for key in ('enabled','trading_only'):
        if type(data.get(key,False)) is not bool:
            raise ValueError('Schedule switch must be boolean: '+key)
    values = {'last_job':data.get('last_job'),'last_slot':data.get('last_slot'),
              'source':data.get('source'),'kind':data.get('kind'),
              'enabled':data.get('enabled',False),'trading_only':data.get('trading_only',False),
              'run_at':data.get('at'),'interval_seconds':_counter(data.get('interval',0),'interval')}
    keys = tuple(values)
    return conn.execute('UPDATE schedules SET '+','.join('`'+key+'`=%s' for key in keys)+' WHERE id=%s',
                        (*[values[key] for key in keys],identifier)).rowcount


def job_view(row, *, include_items=True):
    row = dict(row)
    result = dict(_document(row['data'])) if 'data' in row else {}
    for key in ('id','identity','status','created',*COUNTERS,'retry_at','last_yield','trigger','source',
                'dataset','schedule_id','mode','name','message','updated'):
        if key in row:
            result[key] = row[key]
    for column,key in (('range_start','start'),('range_end','end')):
        if row.get(column) is not None:
            result[key] = row[column].isoformat() if hasattr(row[column],'isoformat') else row[column]
    if row.get('expires_at') is not None:
        stamp = local_datetime(row['expires_at']).replace(tzinfo=store.SHANGHAI)
        result['expires_at'] = stamp.isoformat(timespec='microseconds' if stamp.microsecond else 'seconds')
    elif result.get('expires_at') is not None:
        result.pop('expires_at',None)
    if not include_items:
        result.pop('items',None)
    return result


def schedule_view(row):
    row = dict(row)
    result = dict(_document(row['data']))
    result['id'] = row['id']
    for key in ('last_job','last_slot','source','kind'):
        if key in row and (row[key] is not None or key in result):
            result[key] = row[key]
    for key in ('enabled','trading_only'):
        if key in row:
            result[key] = bool(row[key])
    if 'interval_seconds' in row:
        result['interval'] = int(row['interval_seconds'])
    if row.get('run_at') is not None:
        value = row['run_at']
        if isinstance(value,timedelta):
            seconds = int(value.total_seconds())
            result['at'] = f'{seconds//3600:02d}:{seconds%3600//60:02d}'
        else:
            result['at'] = str(value)[:5]
    return result


def apply_schema(conn):
    """Add missing columns/indexes only; the owner then syncs bounded ID batches."""
    added = []
    for table,definitions in (('collection_jobs',JOB_COLUMNS),('schedules',SCHEDULE_COLUMNS)):
        existing = {row[0] for row in conn.execute('SHOW COLUMNS FROM `'+table+'`')}
        missing = [(name,kind) for name,kind in definitions.items() if name not in existing]
        if missing:
            conn.execute('ALTER TABLE `'+table+'` '+','.join('ADD COLUMN `'+name+'` '+kind for name,kind in missing))
            added.extend(table+'.'+name for name,_ in missing)
    indexes = {row[2] for row in conn.execute('SHOW INDEX FROM collection_jobs')}
    for name,columns in (('job_due','status,retry_at'),('job_expiry','status,expires_at')):
        if name not in indexes:
            conn.execute('ALTER TABLE collection_jobs ADD KEY `'+name+'` ('+columns+')')
            added.append('collection_jobs.'+name)
    return added
