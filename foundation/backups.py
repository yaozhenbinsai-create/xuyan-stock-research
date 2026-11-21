"""Streaming logical MySQL backups with integrity hashes and finite rotation."""
from __future__ import annotations
import base64
from datetime import date, datetime, timedelta
from decimal import Decimal
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import uuid
import pymysql
from pymysql.cursors import SSCursor
from fastapi import HTTPException
from foundation import database as store, deployment, storage_policy, record_store
from research import state_store

BASE_TABLES = ("schema_versions", "facts", "heads", "fact_origins", "sources", "schedules", "settings", "models", "plans", "agents",
          "collection_jobs", "collection_events", "collection_units", "audit", "asset_stats", "dataset_usage", "collection_holds", "classification_scopes", "evidence_refs",
          "research_runs", "research_calls", "research_cycles", "research_events", "research_outcomes")
TABLES = (BASE_TABLES + ("research_controls", "portfolio_fills", "research_notices", "research_screening")
          + record_store.TABLES + state_store.TABLES
          + ("research_scope_sets","research_scope_members","research_candidates"))
_lock = threading.Lock()


def pack(value):
    if isinstance(value, bytes):
        return {"$binary": base64.b64encode(value).decode()}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$decimal": str(value)}
    if isinstance(value, timedelta):
        return {"$time_microseconds": (value.days*86400+value.seconds)*1000000+value.microseconds}
    return value


def unpack(value):
    if isinstance(value, dict) and len(value)==1:
        if '$binary' in value:return base64.b64decode(value['$binary'])
        if '$datetime' in value:return datetime.fromisoformat(value['$datetime'])
        if '$date' in value:return date.fromisoformat(value['$date'])
        if '$decimal' in value:return Decimal(value['$decimal'])
        if '$time_microseconds' in value:return timedelta(microseconds=value['$time_microseconds'])
    return value


def rotate(directory):
    policy = storage_policy.POLICY
    root = directory.resolve()
    candidates = sorted((p for p in directory.glob("stock-mysql-*.jsonl.gz") if p.is_file() and not p.is_symlink()), key=lambda p: p.name, reverse=True)
    total, keep = 0, 0
    removed = []
    for path in candidates:
        size = path.stat().st_size
        if keep < policy["backups_max_count"] and total + size <= policy["backups_max_bytes"]:
            total += size; keep += 1
        else:
            if path.resolve().parent != root:
                raise RuntimeError("备份清理路径越界")
            path.unlink(); removed.append(path.name)
    return removed


def create_backup():
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, "备份正在执行，请稍后查看结果")
    directory = deployment.backups_path()
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / ("stock-mysql-" + datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6] + ".jsonl.gz")
    partial = final.with_name(final.name + ".part")
    raw = None
    try:
        raw = pymysql.connect(**store.connection_options())
        with raw.cursor() as c:
            c.execute('SET SESSION information_schema_stats_expiry=0')
            c.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            c.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
        counts, digest = {}, hashlib.sha256()
        with gzip.open(partial, "wb", compresslevel=3) as stream:
            def emit(value):
                line = (store.dumps(value) + "\n").encode()
                stream.write(line); digest.update(line)
            emit({"format": "stock-mysql-jsonl-v1", "schema_version": store.SCHEMA_VERSION, "created": store.now()})
            for table in TABLES:
                with raw.cursor() as c:
                    c.execute("SHOW COLUMNS FROM `" + table + "`")
                    columns = [r[0] for r in c.fetchall() if "GENERATED" not in r[5]]
                    c.execute("SHOW INDEX FROM `" + table + "` WHERE Key_name='PRIMARY'")
                    order = [r[4] for r in sorted(c.fetchall(),key=lambda row:row[3])]
                    c.execute('SELECT AUTO_INCREMENT FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s',(table,))
                    auto_increment = c.fetchone()[0]
                emit({"table": table, "columns": columns, "auto_increment": auto_increment})
                count = 0
                with raw.cursor(SSCursor) as c:
                    c.execute("SELECT " + ",".join("`"+k+"`" for k in columns) + " FROM `" + table + "` ORDER BY " + ",".join("`"+k+"`" for k in order))
                    while batch := c.fetchmany(1000):
                        for row in batch:
                            emit({"row": [pack(v) for v in row]})
                        count += len(batch)
                        if partial.stat().st_size > storage_policy.POLICY["backups_max_bytes"] // 2:
                            raise HTTPException(409, "单份备份达到容量上限，已保留上一份可用备份")
                counts[table] = count
            stream.write((store.dumps({"footer": {"counts": counts, "sha256": digest.hexdigest()}})+"\n").encode())
        raw.rollback()
        footer = verify_file(partial)
        os.replace(partial, final)
        removed = rotate(directory)
        receipt = {"file": final.name, "integrity": "sha256_verified", "bytes": final.stat().st_size,
                   "counts": footer["counts"], "sha256": footer["sha256"], "rotated": removed}
        store.audit("完成 MySQL 备份", {k: v for k, v in receipt.items() if k != "counts"})
        return receipt
    finally:
        if raw:
            raw.close()
        partial.unlink(missing_ok=True)
        _lock.release()


def inspect_backup(path):
    """Verify the original byte footer and retain its schema/column contract."""
    digest, footer, counts, current = hashlib.sha256(), None, {}, None
    header, tables = None, {}
    with gzip.open(path, "rb") as stream:
        for line in stream:
            value = json.loads(line)
            if not isinstance(value,dict):
                raise ValueError('备份行必须为对象')
            if "footer" in value:
                if footer is not None:
                    raise ValueError("备份包含多个尾部")
                footer = value["footer"]
            else:
                if footer:
                    raise ValueError("备份尾部之后存在额外内容")
                digest.update(line)
                if header is None:
                    if value.get('format')!='stock-mysql-jsonl-v1' or type(value.get('schema_version')) is not int:
                        raise ValueError('不支持的备份格式或缺少结构版本')
                    if value['schema_version'] not in (1,store.SCHEMA_VERSION):
                        raise ValueError('备份结构版本不受支持')
                    header=value
                elif "table" in value:
                    current = value["table"]
                    if header['schema_version']==1:
                        from foundation.backup_compat import LEGACY_TABLES
                        allowed_tables=LEGACY_TABLES
                    else:
                        allowed_tables=TABLES
                    if current not in allowed_tables or current in counts:
                        raise ValueError("备份表范围无效")
                    columns=value.get('columns')
                    if (not isinstance(columns,list) or not columns
                            or any(not isinstance(column,str) or not re.fullmatch(r'[a-z][a-z0-9_]*',column) for column in columns)
                            or len(set(columns))!=len(columns)):
                        raise ValueError('备份字段清单无效')
                    if 'auto_increment' in value and value['auto_increment'] is not None:
                        counter=value['auto_increment']
                        if type(counter) is not int or not 1<=counter<2**64:
                            raise ValueError('备份自增高位无效')
                    tables[current]=value
                    counts[current] = 0
                elif "row" in value:
                    if current is None or not isinstance(value['row'],list) or len(value['row'])!=len(tables[current]['columns']):
                        raise ValueError('备份记录与字段清单不符')
                    counts[current] += 1
                else:
                    raise ValueError('备份包含未知行类型')
    if not header or not footer or footer["sha256"] != digest.hexdigest() or counts != footer["counts"]:
        raise ValueError("备份校验失败")
    return {'header':header,'footer':footer,'tables':tables}


def verify_file(path):
    return inspect_backup(path)['footer']


def _restore_name(database):
    if not re.fullmatch(r'stock_(?:restore_|test_)[a-z0-9_]+',database):
        raise ValueError('仅可恢复至独立的测试或恢复数据库')
    store.connection_options(database=database)
    return database


def _restore_increments(database, inspection):
    """Apply recorded high-water marks, never invent missing historical state."""
    saved=inspection['tables']
    details={}
    with store.db(admin=True,database=database) as conn:
        conn.execute('SET SESSION information_schema_stats_expiry=0')
        rows=conn.execute('SELECT TABLE_NAME,AUTO_INCREMENT FROM information_schema.TABLES '
                          'WHERE TABLE_SCHEMA=%s AND AUTO_INCREMENT IS NOT NULL',(database,)).fetchall()
        for table,current in rows:
            recorded=saved.get(table,{}).get('auto_increment')
            desired=max(int(current),int(recorded)) if recorded is not None else int(current)
            if recorded is not None:
                conn.execute('ALTER TABLE `'+table+'` AUTO_INCREMENT='+str(desired))
            details[table]={'recorded':recorded,'restored':desired,
                            'basis':'recorded_high_water' if recorded is not None else 'max_saved_id_or_engine_default'}
        actual={row[0]:int(row[1]) for row in conn.execute('SELECT TABLE_NAME,AUTO_INCREMENT FROM information_schema.TABLES '
                'WHERE TABLE_SCHEMA=%s AND AUTO_INCREMENT IS NOT NULL',(database,))}
    if any(actual.get(table,0)<value['restored'] for table,value in details.items()):
        raise RuntimeError('恢复后的自增高位低于备份或现存记录')
    missing=[table for table,value in details.items() if value['recorded'] is None]
    return {'tables':details,'recorded_high_water_complete':not missing,
            'missing_headers':missing,
            'limitation':('旧备份未记录这些表的原始自增高位；只能从已保存行恢复，不能还原已删除行或跳号对应的原高位。' if missing else None)}


def restore_raw(path, database, inspection, expected_tables):
    """Restore exact saved columns into an already-created matching empty schema."""
    _restore_name(database)
    footer=inspection['footer']
    current, columns, batch = None, None, []
    expected_hashes, column_sets = {}, {}
    restored_stream=hashlib.sha256()
    read_footer=None
    with store.db(admin=True, database=database) as c:
        c.execute("DELETE FROM schema_versions")
    def flush():
        if not batch:
            return
        sql = "INSERT INTO `"+current+"` ("+",".join("`"+k+"`" for k in columns)+") VALUES("+",".join(["%s"]*len(columns))+")"
        with store.db(admin=True, database=database) as c:
            c.executemany(sql, batch)
        batch.clear()
    with gzip.open(path, "rb") as stream:
        for line in stream:
            value = json.loads(line)
            if read_footer is not None:
                raise ValueError('恢复流在尾部之后仍有内容')
            if 'footer' in value:
                read_footer=value['footer']
                continue
            restored_stream.update(line)
            if "table" in value:
                flush(); current, columns = value["table"], value["columns"]
                if value!=inspection['tables'].get(current):
                    raise ValueError('恢复读取期间表字段或自增高位发生变化')
                with store.db(readonly=True, database=database) as c:
                    allowed=[r[0] for r in c.execute('SHOW COLUMNS FROM `'+current+'`') if 'GENERATED' not in r[5]]
                # Online schema additions can leave the same named columns in a
                # different physical order. INSERT and verification both name
                # every saved column explicitly; no value depends on ordinal
                # position in the newly created table.
                if len(columns)!=len(allowed) or set(columns)!=set(allowed):
                    raise ValueError('备份字段与目标结构不符：'+current+'；未丢弃字段或依赖新默认值')
                column_sets[current]=columns
                expected_hashes[current]=hashlib.sha256()
            elif "row" in value:
                expected_hashes[current].update((store.dumps(value['row'])+'\n').encode())
                batch.append([unpack(v) for v in value["row"]])
                if len(batch) >= 10000:
                    flush()
            elif value!=inspection['header']:
                raise ValueError('恢复读取期间备份头发生变化')
        flush()
    if read_footer!=footer or restored_stream.hexdigest()!=footer['sha256']:
        raise ValueError('恢复读取的原始内容与已核验的备份尾部不一致')
    return _verify_restored_rows(database,inspection,expected_tables,expected_hashes,column_sets)


def _verify_restored_rows(database,inspection,expected_tables,expected_hashes,column_sets,progress=None):
    """Count and hash the same ordered stream, avoiding a blocking COUNT scan."""
    _restore_name(database)
    footer=inspection['footer']
    if not set(BASE_TABLES)<=set(footer['counts']):
        raise RuntimeError('备份缺少基础表，无法完成恢复核验')
    raw=pymysql.connect(**store.connection_options(admin=True,database=database))
    actual_hashes,actual_counts={},{}
    try:
        with raw.cursor() as c:
            c.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
            c.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
            c.execute("SELECT TABLE_NAME FROM information_schema.tables WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE'")
            if {r[0] for r in c.fetchall()}!=set(expected_tables):
                raise RuntimeError('恢复库表清单与预期结构不符')
        for table in expected_tables:
            with raw.cursor() as c:
                c.execute('SHOW COLUMNS FROM `'+table+'`')
                allowed=[r[0] for r in c.fetchall() if 'GENERATED' not in r[5]]
                columns=column_sets.get(table,allowed)
                if len(columns)!=len(allowed) or set(columns)!=set(allowed):
                    raise ValueError('备份字段与恢复库不符：'+table)
                c.execute('SHOW INDEX FROM `'+table+"` WHERE Key_name='PRIMARY'")
                order=[r[4] for r in sorted(c.fetchall(),key=lambda r:r[3])]
                if not order:raise ValueError('恢复核验缺少稳定主键：'+table)
            digest=hashlib.sha256();count=0
            with raw.cursor(SSCursor) as c:
                c.execute('SELECT '+','.join('`'+k+'`' for k in columns)+' FROM `'+table+'` ORDER BY '+','.join('`'+k+'`' for k in order))
                while rows:=c.fetchmany(1000):
                    count+=len(rows)
                    for row in rows:digest.update((store.dumps([pack(v) for v in row])+'\n').encode())
            actual_hashes[table]=digest.hexdigest();actual_counts[table]=count
            if count!=footer['counts'].get(table,0):
                raise RuntimeError('恢复行数与备份不符：'+table)
            expected=expected_hashes.get(table)
            expected=expected.hexdigest() if expected is not None else hashlib.sha256().hexdigest()
            if actual_hashes[table]!=expected:
                raise RuntimeError('独立恢复内容摘要不符：'+table)
            if progress:progress({'phase':'restored_table_verified','table':table,'count':count})
        raw.rollback()
    finally:
        raw.close()
    increments=_restore_increments(database,inspection)
    return {'database':database,'integrity':'restored_hashes_and_counts_verified','table_hashes':actual_hashes,
            'verified_counts':actual_counts,'auto_increment':increments,**footer}


def verify_restored(path,database,progress=None):
    """Resume verification of an already-written isolated copy, then finalize IDs.

    No data rows are replayed. The file footer, all named columns, every saved
    row, and the restored primary-key streams must still match exactly.
    """
    _restore_name(database)
    inspection=inspect_backup(path)
    expected_hashes,column_sets={},{}
    digest=hashlib.sha256();current=None;footer=None
    with gzip.open(path,'rb') as stream:
        for line in stream:
            value=json.loads(line)
            if footer is not None:raise ValueError('备份尾部之后存在内容')
            if 'footer' in value:
                footer=value['footer'];continue
            digest.update(line)
            if 'table' in value:
                current=value['table']
                if value!=inspection['tables'].get(current):raise ValueError('核验期间备份表头发生变化')
                column_sets[current]=value['columns'];expected_hashes[current]=hashlib.sha256()
            elif 'row' in value:
                expected_hashes[current].update((store.dumps(value['row'])+'\n').encode())
            elif value!=inspection['header']:
                raise ValueError('核验期间备份头发生变化')
    if footer!=inspection['footer'] or digest.hexdigest()!=footer['sha256']:
        raise ValueError('核验期间备份文件内容发生变化')
    if inspection['header']['schema_version']==1:
        from foundation.backup_compat import LEGACY_TABLES
        expected_tables=LEGACY_TABLES
    else:expected_tables=TABLES
    return _verify_restored_rows(database,inspection,expected_tables,expected_hashes,column_sets,progress)


def restore_isolated(path, database):
    """Restore v2 directly, or verify v1 exactly before isolated typed migration."""
    from tools.mysql_instance import create_isolated_database
    _restore_name(database)
    inspection=inspect_backup(path)
    if inspection['header']['schema_version']==1:
        from foundation.backup_compat import restore_legacy
        return restore_legacy(path,database,inspection)
    create_isolated_database(database)
    return restore_raw(path,database,inspection,TABLES)
