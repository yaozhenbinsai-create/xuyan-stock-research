from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import threading
import time

import pymysql
from foundation import deployment, security_secrets

_CONVERSIONS = pymysql.converters.conversions.copy()
_CONVERSIONS[pymysql.constants.FIELD_TYPE.DATE] = lambda value: value.decode() if isinstance(value, bytes) else value

ROOT = Path(__file__).resolve().parent.parent
SHANGHAI = timezone(timedelta(hours=8))
IntegrityError = pymysql.IntegrityError
DatabaseError = pymysql.DatabaseError
SCHEMA_VERSION = 2
_pool_slots=threading.BoundedSemaphore(6)
_pool_lock=threading.Lock()
_pool_idle=[]


def _borrow(options):
    if not _pool_slots.acquire(timeout=10):
        raise RuntimeError('数据库正忙，请稍后重试')
    key=tuple(sorted(options.items()))
    raw=None
    try:
        with _pool_lock:
            for i,(saved,candidate,used) in enumerate(_pool_idle):
                if saved==key:
                    raw=_pool_idle.pop(i)[1];break
            if raw is None and _pool_idle:
                _pool_idle.pop(0)[1].close()
        if raw:
            try:
                raw.ping(reconnect=False)
            except pymysql.Error:
                raw.close();raw=None
        if raw is None:
            for attempt in range(6):
                try:
                    raw=pymysql.connect(**options);break
                except pymysql.OperationalError as exc:
                    # Retry authentication admission only. Never replay an
                    # uncertain INSERT, UPDATE, commit or provider request.
                    if exc.args[0]!=1040 or attempt==5:raise
                    time.sleep(.1*(attempt+1))
        return key,raw
    except BaseException:
        _pool_slots.release();raise


def _release(key,raw,broken):
    try:
        if broken or not raw.open:
            raw.close()
        else:
            with _pool_lock:_pool_idle.append((key,raw,time.monotonic()))
    finally:_pool_slots.release()


def close_pool():
    with _pool_lock:
        for _,raw,_ in _pool_idle:raw.close()
        _pool_idle.clear()


def now():
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def today():
    return datetime.now(SHANGHAI).date().isoformat()


def data_root():
    database = os.environ.get("STOCK_MYSQL_DATABASE", "")
    if database.startswith(("stock_test_", "stock_restore_")) and os.environ.get("STOCK_DATA_ROOT"):
        return Path(os.environ["STOCK_DATA_ROOT"]).resolve()
    return ROOT / "data"


def database_path():
    """Legacy DPAPI entropy identity only. No runtime SQLite file is opened."""
    return data_root() / "stock.db"


def dumps(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)


def config_path(*, admin=False):
    return deployment.credentials_path(admin=admin)


def connection_options(*, admin=False, database=None):
    v = security_secrets.load_json(config_path(admin=admin))
    if not v or v.get("host") != "127.0.0.1":
        raise RuntimeError("项目 MySQL 加密连接配置缺失或不是本机地址")
    target = database or os.environ.get("STOCK_MYSQL_DATABASE") or v["database"]
    if not re.fullmatch(r"stock_(?:foundation|test_[a-z0-9_]+|restore_[a-z0-9_]+)", target):
        raise RuntimeError("数据库名称不在项目专用范围")
    # DATE is stored as a native database date; the existing API contract uses ISO dates.
    return dict(host=v["host"], port=int(v["port"]), user=v["user"], password=v["password"], database=target,
                charset="utf8mb4", autocommit=False, connect_timeout=5, read_timeout=300 if admin else 30,
                write_timeout=300 if admin else 30, conv=_CONVERSIONS)


class Row:
    def __init__(self, names, values):
        self._names, self._values = names, values
        self._map = dict(zip(names, values))

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else self._map[key]

    def keys(self):
        return self._names

    def __iter__(self):
        return iter(self._values)


class Result:
    def __init__(self, cursor):
        self.cursor = cursor
        self.names = [f[0] for f in cursor.description] if cursor.description else []
        self.lastrowid, self.rowcount = cursor.lastrowid, cursor.rowcount

    def fetchone(self):
        value = self.cursor.fetchone()
        return Row(self.names, value) if value is not None else None

    def fetchall(self):
        return [Row(self.names, value) for value in self.cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=None):
        cursor = self.raw.cursor()
        cursor.execute(sql, params)
        return Result(cursor)

    def executemany(self, sql, rows):
        cursor = self.raw.cursor()
        cursor.executemany(sql, rows)
        return Result(cursor)


@contextmanager
def db(*, readonly=False, admin=False, database=None, isolation="READ COMMITTED", timeout=None):
    options=connection_options(admin=admin,database=database)
    if timeout is not None:
        if type(timeout) is not int or not 1<=timeout<=600:
            raise ValueError('数据库操作时限无效')
        options.update(read_timeout=timeout,write_timeout=timeout)
    key,raw=_borrow(options)
    c = Connection(raw)
    broken=False
    try:
        c.execute("SET time_zone='+08:00'")
        if isolation not in ("READ COMMITTED", "REPEATABLE READ"):
            raise ValueError("不支持的事务隔离级别")
        c.execute("SET SESSION TRANSACTION ISOLATION LEVEL " + isolation)
        meta_path = deployment.metadata_path()
        if meta_path.exists():
            expected = json.loads(meta_path.read_text(encoding="utf-8")).get("server_uuid")
            if expected and c.execute("SELECT @@server_uuid").fetchone()[0] != expected:
                raise RuntimeError("MySQL 实例身份不符，未读写")
        if readonly:
            c.execute("SET TRANSACTION READ ONLY")
        raw.begin()
        yield c
        raw.commit()
    except BaseException:
        try:
            raw.rollback()
        except pymysql.Error:
            broken=True
        raise
    finally:
        _release(key,raw,broken)


def initialize():
    data_root().mkdir(parents=True, exist_ok=True)
    with db(readonly=True) as c:
        if c.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] != SCHEMA_VERSION:
            raise RuntimeError("MySQL 结构版本不匹配，请先完成数据库迁移")


def apply_schema(database=None):
    with db(admin=True, database=database) as c:
        for path in (ROOT / 'foundation' / 'schema.sql', ROOT / 'foundation' / 'records_schema.sql', ROOT / 'research' / 'schema.sql'):
            for statement in path.read_text(encoding='utf-8').split(';'):
                if statement.strip():
                    c.execute(statement)
        from foundation import queue_state
        from research import state_store, coverage
        queue_state.apply_schema(c)
        state_store.apply_schema(c)
        coverage.apply_schema(c)
        c.execute("INSERT INTO schema_versions VALUES(%s,%s) ON DUPLICATE KEY UPDATE applied_at=applied_at", (SCHEMA_VERSION, now()))


def description():
    v = connection_options()
    return {"engine": "mysql", "host": v["host"], "port": v["port"], "name": v["database"], "schema_version": SCHEMA_VERSION}


def setting(key, default=None, *, conn=None):
    if conn is None:
        with db(readonly=True) as c:
            return setting(key, default, conn=c)
    row = conn.execute("SELECT data FROM settings WHERE `key`=%s", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_setting(key, value, *, conn=None):
    if conn is None:
        with db() as c:
            return set_setting(key, value, conn=c)
    conn.execute("INSERT INTO settings VALUES(%s,%s) ON DUPLICATE KEY UPDATE data=VALUES(data)", (key, dumps(value)))


def audit(action, value, *, conn=None):
    if conn is None:
        with db() as c:
            return audit(action, value, conn=c)
    encoded = dumps(value)
    if len(encoded.encode()) > 16384:
        raise ValueError("审计只保存简要事件，不能写入完整响应或大对象")
    conn.execute("INSERT INTO audit(created,action,data) VALUES(%s,%s,%s)", (now(), action, encoded))
