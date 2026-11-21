"""Provision the stock project's independent, loopback-only MySQL 8.4 instance.

The official archive must already be verified and extracted under tools/mysql.
Passwords are generated locally and saved in current-user DPAPI envelopes.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from foundation import assets, database as store, deployment, security_secrets, mysql_runtime


def initialize(port=None):
    base = deployment.mysql_root()
    metadata = deployment.metadata_path()
    if metadata.exists():
        state = json.loads(metadata.read_text(encoding="utf-8"))
        if port is not None and port != state["port"]:
            raise ValueError("指定端口与已初始化实例不同，请使用原实例端口。")
        mysql_runtime.ensure_running()
        if state.pop("initializing", False):
            store.apply_schema()
            install_source_catalog()
            save_metadata(metadata, state)
        store.initialize()
        return {"state": "already_initialized", **store.description()}
    port = deployment.settings().get("mysql_port", 19317) if port is None else port
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("MySQL 端口须为 1 到 65535 的整数。")
    resume = (base / "server" / "mysql").exists()
    if resume and not (store.config_path().exists() and store.config_path(admin=True).exists()):
        raise RuntimeError("发现没有加密初始化检查点的实例；不能重建或覆盖")
    native_executable = deployment.mysql_executable()
    native_base = base
    if not str(base).isascii() or not str(native_executable).isascii():
        raise RuntimeError("本机 MySQL 选项文件需要 ASCII 路径。请将本项目放在仅含英文、数字与常用标点的目录。")
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError("专用端口已占用，未接管任何现有实例")
    base.mkdir(parents=True, exist_ok=True)
    (base / "tmp").mkdir(exist_ok=True)
    ini = base / "my.ini"
    ini.write_text(f'''[mysqld]
basedir="{native_executable.parents[1].as_posix()}"
datadir="{(native_base / 'server').as_posix()}"
port={port}
bind-address=127.0.0.1
mysqlx=0
skip-name-resolve=ON
local-infile=OFF
secure-file-priv=NULL
character-set-server=utf8mb4
collation-server=utf8mb4_bin
default-time-zone=+08:00
max-connections=32
max-allowed-packet=16M
innodb-buffer-pool-size=1024M
innodb-redo-log-capacity=256M
innodb-io-capacity=200
innodb-io-capacity-max=400
innodb-file-per-table=ON
innodb-undo-log-truncate=ON
innodb-max-undo-log-size=64M
innodb-purge-rseg-truncate-frequency=16
skip-log-bin
general-log=OFF
slow-query-log=OFF
log-error="{(native_base / 'server.err').as_posix()}"
log-error-verbosity=2
tmpdir="{(native_base / 'tmp').as_posix()}"
tmp-table-size=16M
max-heap-table-size=16M
temptable-max-ram=64M
temptable-max-mmap=0
''', encoding="utf-8")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    if not resume:
        initialized = subprocess.run([str(native_executable), f"--defaults-file={native_base / 'my.ini'}", "--initialize-insecure"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags, timeout=120)
        if initialized.returncode:
            raise RuntimeError("专用实例初始化失败，请查看 data/mysql/server.err")
    root_password = security_secrets.load_json(store.config_path(admin=True))["password"] if resume else secrets.token_hex(32)
    app_password = security_secrets.load_json(store.config_path())["password"] if resume else secrets.token_hex(32)
    init = base / ".bootstrap.sql"
    try:
        init.write_text(
            f"ALTER USER 'root'@'localhost' IDENTIFIED WITH caching_sha2_password BY '{root_password}';\n"
            f"CREATE USER IF NOT EXISTS 'root'@'127.0.0.1' IDENTIFIED WITH caching_sha2_password BY '{root_password}';\n"
            "GRANT ALL PRIVILEGES ON *.* TO 'root'@'127.0.0.1' WITH GRANT OPTION;\n"
            "CREATE DATABASE IF NOT EXISTS stock_foundation CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;\n"
            f"CREATE USER IF NOT EXISTS 'stock_app'@'127.0.0.1' IDENTIFIED WITH caching_sha2_password BY '{app_password}';\n"
            "GRANT SELECT,INSERT,UPDATE,DELETE ON stock_foundation.* TO 'stock_app'@'127.0.0.1';\n",
            encoding="utf-8")
        if os.name == "nt":
            # An ephemeral bootstrap file is needed before authenticated connections exist.
            import getpass
            subprocess.run(["icacls", str(init), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
        proc = subprocess.Popen([str(native_executable), f"--defaults-file={native_base / 'my.ini'}", "--standalone", f"--init-file={(native_base / init.name).as_posix()}"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=creationflags)
        common = {"host": "127.0.0.1", "port": port, "database": "stock_foundation"}
        # Save before the first connection so a failed later step remains recoverable.
        security_secrets.save_json(store.config_path(admin=True), common | {"user": "root", "password": root_password})
        security_secrets.save_json(store.config_path(), common | {"user": "stock_app", "password": app_password})
        for _ in range(120):
            try:
                with store.db(admin=True) as c:
                    server_uuid, version = c.execute("SELECT @@server_uuid,@@version").fetchone()
                break
            except Exception:
                if proc.poll() is not None:
                    raise RuntimeError("专用实例未能启动，请查看 server.err") from None
                time.sleep(.25)
        else:
            raise RuntimeError("专用实例连接超时；凭据已加密保存，请核查现场状态")
    finally:
        # Never retain a cleartext password, even after an initialization failure.
        init.unlink(missing_ok=True)
    state = common | {"owner": "stock-foundation", "executable": str(native_executable),
                      "config": str(native_base / "my.ini"), "server_uuid": server_uuid, "version": version}
    save_metadata(metadata, state | {"initializing": True})
    store.apply_schema()
    install_source_catalog()
    save_metadata(metadata, state)
    return {"state": "initialized", "version": version, **store.description()}


def save_metadata(path, state):
    temp = path.with_name(path.name + ".pending")
    temp.write_text(store.dumps(state), encoding="utf-8")
    temp.replace(path)


def install_source_catalog():
    """Register implemented adapter choices without carrying over a site's settings or observations."""
    choices = (
        ("bs_calendar", "baostock", "calendar", "BaoStock 交易日历"),
        ("bs_basics", "baostock", "basics", "BaoStock 证券资料"),
        ("bs_universe", "baostock", "universe", "BaoStock 当日证券名录"),
        ("bs_industry", "baostock", "industry", "BaoStock 行业分类"),
        ("bs_daily_all", "baostock", "daily_all", "BaoStock 全市场日线"),
        ("bs_daily", "baostock", "daily", "BaoStock 个股日线"),
        ("bs_minutes", "baostock", "minutes", "BaoStock 分钟行情"),
        ("bs_profit", "baostock", "profit", "BaoStock 盈利能力"),
        ("bs_balance", "baostock", "balance", "BaoStock 偿债能力"),
        ("bs_cashflow", "baostock", "cashflow", "BaoStock 现金流"),
        ("bs_growth", "baostock", "growth", "BaoStock 成长能力"),
        ("tx_daily", "tencent", "daily", "腾讯日线行情"),
        ("tx_quotes", "tencent", "quotes", "腾讯当前报价"),
        ("cninfo_announcements", "cninfo", "announcements", "巨潮公告索引"),
        ("cninfo_text", "cninfo", "announcement_text", "巨潮公告原文"),
        ("sina_flow_history", "sina", "flow_history", "新浪历史资金流"),
    )
    records = [
        {"id": sid, "provider": provider, "operation": operation, "name": name,
         "dataset": assets.SOURCE_DATASETS[sid], "coverage": assets.DATASETS[assets.SOURCE_DATASETS[sid]],
         "implemented": True, "min_interval": 3, "timeout": 30}
        for sid, provider, operation, name in choices
    ]
    records.extend((assets.ACTIONS_SOURCE, assets.FILING_FINANCIAL_SOURCE, assets.OFFICIAL_POLICY_SOURCE))
    with store.db() as conn:
        for record in records:
            value = record | {"enabled": False, "automatic": False, "health": "unverified",
                              "validation_origin": "本机尚未验证", "message": "等待管理员选择并启用"}
            conn.execute("INSERT IGNORE INTO sources(id,data) VALUES(%s,%s)",
                         (value["id"], store.dumps(value)))


def create_isolated_database(name):
    if not (name.startswith("stock_test_") or name.startswith("stock_restore_")):
        raise ValueError("仅允许独立的测试／恢复数据库")
    store.connection_options(database=name)  # Validate before identifier interpolation.
    with store.db(admin=True, database="stock_foundation") as c:
        c.execute(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
        c.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON `{name}`.* TO 'stock_app'@'127.0.0.1'")
    store.apply_schema(name)


def drop_isolated_database(name):
    if not (name.startswith("stock_test_") or name.startswith("stock_restore_")):
        raise ValueError("不能以测试清理方式删除正式库")
    store.connection_options(database=name)
    store.close_pool()
    with store.db(admin=True, database="stock_foundation") as c:
        c.execute(f"DROP DATABASE `{name}`")
        c.execute(f"REVOKE ALL PRIVILEGES ON `{name}`.* FROM 'stock_app'@'127.0.0.1'")


if __name__ == "__main__":
    os.environ.pop("STOCK_MYSQL_DATABASE", None)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["initialize", "start", "status"])
    parser.add_argument("--port", type=int, help="新建专用实例的本机端口")
    args = parser.parse_args()
    if args.action == "initialize":
        result = initialize(args.port)
    else:
        if args.port is not None:
            parser.error("--port 仅适用于 initialize")
        if args.action == "start":
            mysql_runtime.ensure_running()
        with store.db(readonly=True) as c:
            result = {**store.description(), "version": c.execute("SELECT @@version").fetchone()[0]}
    print(json.dumps(result, ensure_ascii=False))
