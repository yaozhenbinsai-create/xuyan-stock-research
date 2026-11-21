"""Atomic promotion/rollback of the verified relational schema on this instance.

The application must be stopped and its active work drained.  Tables are moved
in one InnoDB RENAME statement; source data is retained in a named rollback
schema.  No credentials, model calls, or filesystem deletion is involved.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import socket

from foundation import database as store

PRODUCTION = 'stock_foundation'
RELATIONAL_PREFIX = 'stock_restore_relational_'
LEGACY_PREFIX = 'stock_restore_legacy_'
_IDENT = re.compile(r'^[a-z][a-z0-9_]*$')


def _q(value):
    if not _IDENT.fullmatch(value):
        raise ValueError('Invalid project SQL identifier')
    return '`'+value+'`'


def _offline():
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(('127.0.0.1',8001))==0:
            raise RuntimeError('Stop the verified project service before database cutover')


def _tables(conn,schema):
    rows=conn.execute('SELECT TABLE_NAME,TABLE_TYPE,ENGINE FROM information_schema.tables WHERE TABLE_SCHEMA=%s',
                      (schema,)).fetchall()
    if any(row[1]!='BASE TABLE' or row[2]!='InnoDB' for row in rows):
        raise RuntimeError('Only ordinary InnoDB tables can participate in this cutover')
    if conn.execute('SELECT 1 FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=%s LIMIT 1',(schema,)).fetchone():
        raise RuntimeError('Cross-schema table triggers require a dedicated migration')
    return sorted(row[0] for row in rows)


def _references(conn,schema):
    return [dict(row) for row in conn.execute(
        'SELECT TABLE_NAME table_name,CONSTRAINT_NAME constraint_name,REFERENCED_TABLE_SCHEMA referenced_schema,'
        'REFERENCED_TABLE_NAME referenced_table FROM information_schema.KEY_COLUMN_USAGE '
        'WHERE TABLE_SCHEMA=%s AND REFERENCED_TABLE_SCHEMA IS NOT NULL',(schema,))]


def _drained(conn,schema):
    for table,label in (('collection_jobs','collection batch'),('research_runs','research attempt')):
        if conn.execute('SELECT 1 FROM '+_q(schema)+'.'+_q(table)+" WHERE status='running' LIMIT 1").fetchone():
            raise RuntimeError('An active '+label+' has not drained')


def _save(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def _intent(receipt,operation,target,archive,old,new):
    value={'at':store.now(),'operation':operation,'production':PRODUCTION,'target':target,
           'rollback':archive,'old_tables':old,'new_tables':new,'status':'prepared',
           'ddl_may_have_applied':False}
    _save(str(receipt)+'.intent.json',value)
    # Check the final destination before executing irreversible DDL.
    _save(receipt,value)
    return value


def _promote(target,archive,proof,receipt,validation_receipt=None):
    if not target.startswith(RELATIONAL_PREFIX) or not archive.startswith(LEGACY_PREFIX):
        raise ValueError('Use the verified relational target and a separate project rollback schema')
    for name in (target,archive):
        _q(name);store.connection_options(database=name)
    if proof.get('status')!='validated' or proof.get('source')!=PRODUCTION or proof.get('target')!=target:
        raise RuntimeError('The complete source/target validation receipt does not match this promotion')
    expected=proof.get('validation',{})
    if expected.get('full_value_fidelity') is not True or expected.get('schema_version')!=store.SCHEMA_VERSION:
        raise RuntimeError('Complete value fidelity and current schema version are required')
    _offline()
    with store.db(admin=True,database=PRODUCTION,timeout=120) as conn:
        _drained(conn,PRODUCTION)
        old=_tables(conn,PRODUCTION);new=_tables(conn,target)
        if not old or not new:
            raise RuntimeError('Both production and replacement must contain the full project schema')
        if not set(old)<=set(new):
            raise RuntimeError('The replacement is missing an existing table')
        if conn.execute('SELECT MAX(version) FROM '+_q(target)+'.schema_versions').fetchone()[0]!=store.SCHEMA_VERSION:
            raise RuntimeError('Replacement schema version differs from the deployed source')
        for table,count in (('facts',expected['facts']),('heads',expected['tables']['heads']['count'])):
            if conn.execute('SELECT COUNT(*) FROM '+_q(PRODUCTION)+'.'+_q(table)).fetchone()[0]!=count:
                raise RuntimeError('Production changed after the last full validation: '+table)
        if conn.execute('SELECT SCHEMA_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s',(archive,)).fetchone():
            raise RuntimeError('Rollback schema already exists; no existing archive is overwritten')
        conn.execute('CREATE DATABASE '+_q(archive)+' CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
        if _tables(conn,archive):
            raise RuntimeError('Rollback destination must be empty')
        intent=_intent(receipt,'promote',target,archive,old,new)
        moves=[(_q(PRODUCTION)+'.'+_q(name),_q(archive)+'.'+_q(name)) for name in old]
        moves += [(_q(target)+'.'+_q(name),_q(PRODUCTION)+'.'+_q(name)) for name in new]
        try:
            intent['ddl_may_have_applied']=True
            _save(str(receipt)+'.intent.json',intent)
            conn.execute('RENAME TABLE '+','.join(before+' TO '+after for before,after in moves))
            refs=_references(conn,PRODUCTION)
            if any(row['referenced_schema']!=PRODUCTION for row in refs):
                raise RuntimeError('Post-cutover references require rollback; source archive is preserved')
            result={'at':store.now(),'operation':'promote','production':PRODUCTION,'target':target,'rollback':archive,
                'old_table_count':len(old),'new_table_count':len(new),'foreign_keys':refs,
                'schema_version':store.SCHEMA_VERSION,'validation_receipt':str(validation_receipt),
                'old_tables':old,'new_tables':new,'full_value_fidelity':True,
                'status':'promoted','source_archive_preserved':True}
            _save(receipt,result)
        except Exception:
            raise RuntimeError('Cutover DDL may already have applied. Inspect the table manifests and preserved intent before retrying or rolling back.') from None
    store.close_pool()
    return result


def rollback(target,archive,promotion_receipt,receipt):
    if not target.startswith(RELATIONAL_PREFIX) or not archive.startswith(LEGACY_PREFIX):
        raise ValueError('Rollback names are outside this project migration')
    for name in (target,archive):_q(name);store.connection_options(database=name)
    proof=json.loads(Path(promotion_receipt).read_text(encoding='utf-8-sig'))
    if any(proof.get(k)!=v for k,v in {'status':'promoted','production':PRODUCTION,'target':target,'rollback':archive}.items()):
        raise RuntimeError('Rollback requires the receipt of this exact promotion')
    _offline()
    with store.db(admin=True,database=PRODUCTION,timeout=120) as conn:
        _drained(conn,PRODUCTION)
        _drained(conn,archive)
        current=_tables(conn,PRODUCTION);old=_tables(conn,archive)
        if not current or not old or _tables(conn,target):
            raise RuntimeError('Rollback requires intact old tables and an empty original target schema')
        if current!=proof.get('new_tables') or old!=proof.get('old_tables'):
            raise RuntimeError('Rollback archive or production table manifest does not match the promotion')
        intent=_intent(receipt,'rollback',target,archive,old,current)
        moves=[(_q(PRODUCTION)+'.'+_q(name),_q(target)+'.'+_q(name)) for name in current]
        moves += [(_q(archive)+'.'+_q(name),_q(PRODUCTION)+'.'+_q(name)) for name in old]
        try:
            intent['ddl_may_have_applied']=True
            _save(str(receipt)+'.intent.json',intent)
            conn.execute('RENAME TABLE '+','.join(before+' TO '+after for before,after in moves))
            if any(row['referenced_schema']!=PRODUCTION for row in _references(conn,PRODUCTION)):
                raise RuntimeError('Rollback references do not point to the restored production schema')
            result={'at':store.now(),'operation':'rollback','production':PRODUCTION,'status':'restored',
                    'source_code_restore_required':True,'new_tables_preserved':target}
            _save(receipt,result)
        except Exception:
            raise RuntimeError('Rollback DDL may already have applied. Inspect the preserved table manifests before further operations.') from None
    store.close_pool()
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('sync-promote','promote','rollback'))
    parser.add_argument('--target',required=True);parser.add_argument('--archive',required=True)
    parser.add_argument('--validation-receipt',type=Path);parser.add_argument('--receipt',required=True,type=Path)
    parser.add_argument('--promotion-receipt',type=Path)
    parser.add_argument('--batch-size',type=int,default=5000)
    args=parser.parse_args()
    if args.operation=='rollback':
        if args.promotion_receipt is None:
            parser.error('Rollback requires its exact promotion receipt')
        result=rollback(args.target,args.archive,args.promotion_receipt,args.receipt)
    else:
        # Validate in the same offline invocation as RENAME; an old receipt is
        # never accepted as evidence that a source or target stayed unchanged.
        _offline()
        with store.db(admin=True,database=PRODUCTION) as conn:_drained(conn,PRODUCTION)
        from tools import relational_migration
        operation=relational_migration.synchronize if args.operation=='sync-promote' else relational_migration.validate
        progress=lambda value:print(store.dumps({'at':store.now(),**value}),flush=True)
        proof=operation(PRODUCTION,args.target,batch_size=args.batch_size,progress=progress)
        validation_path=args.validation_receipt or Path(str(args.receipt)+'.validation.json')
        _save(validation_path,proof)
        result=_promote(args.target,args.archive,proof,args.receipt,validation_path)
    print(store.dumps(result))


if __name__=='__main__':main()
