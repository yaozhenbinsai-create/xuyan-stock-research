"""One-way recovery of historical v1 backup documents into isolated v2 tables.

The original DDL is used only in a uniquely named recovery schema.  The running
application never reads this schema and never falls back to payload queries.
"""
from __future__ import annotations

from pathlib import Path
import re
import uuid

from foundation import database as store


LEGACY_SCHEMA = Path(__file__).with_name('migrations')/'schema_v1.sql'
LEGACY_TABLES = tuple(re.findall(r'CREATE TABLE IF NOT EXISTS\s+`?([a-z][a-z0-9_]*)',
                                LEGACY_SCHEMA.read_text(encoding='utf-8'),re.I))


class LegacyBackupRestoreError(RuntimeError):
    def __init__(self, phase, legacy, target):
        self.phase=phase
        self.legacy_database=legacy
        self.target_database=target
        super().__init__(f'旧备份恢复未完成（{phase}）；请保留并核对临时旧库 {legacy} 与目标 {target}，未删除恢复证据。')


def create_legacy_schema(database):
    """Only a fresh project recovery/test schema may receive the archived DDL."""
    if not re.fullmatch(r'stock_(?:restore_|test_)[a-z0-9_]+',database):
        raise ValueError('旧结构仅可创建在独立恢复或测试库中')
    store.connection_options(database=database)
    with store.db(admin=True,database='stock_foundation') as conn:
        conn.execute('CREATE DATABASE `'+database+'` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin')
        conn.execute("GRANT SELECT,INSERT,UPDATE,DELETE ON `"+database+"`.* TO 'stock_app'@'127.0.0.1'")
    with store.db(admin=True,database=database) as conn:
        for statement in LEGACY_SCHEMA.read_text(encoding='utf-8').split(';'):
            if statement.strip():conn.execute(statement)


def restore_legacy(path, database, inspection):
    from foundation import backups
    from tools.mysql_instance import drop_isolated_database
    from tools import relational_migration
    legacy='stock_restore_legacy_backup_'+uuid.uuid4().hex[:16]
    phase='create_legacy_schema'
    try:
        with store.db(admin=True,database='stock_foundation',readonly=True) as conn:
            if conn.execute('SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s',(database,)).fetchone():
                raise ValueError('请求的恢复目标已存在，未覆盖')
        create_legacy_schema(legacy)
        phase='restore_and_verify_v1'
        original=backups.restore_raw(path,legacy,inspection,LEGACY_TABLES)
        phase='migrate_to_typed_schema'
        migration=relational_migration.stage(legacy,database)
        if migration.get('status')!='validated' or not migration.get('validation',{}).get('full_value_fidelity'):
            raise RuntimeError('旧备份的关系式迁移尚未完整验证')
        phase='cleanup_verified_legacy_schema'
        drop_isolated_database(legacy)
    except BaseException as exc:
        raise LegacyBackupRestoreError(phase,legacy,database) from exc
    return {'database':database,'integrity':'legacy_backup_verified_and_typed_migration_validated',
            'source_schema_version':1,'schema_version':store.SCHEMA_VERSION,
            'counts':original['counts'],'sha256':original['sha256'],
            'legacy_restore':original,'migration':migration,
            'temporary_legacy_database':legacy,'temporary_legacy_removed':True,
            'auto_increment':original['auto_increment']}
