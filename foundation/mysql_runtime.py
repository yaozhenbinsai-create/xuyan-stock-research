"""Start only the project's loopback MySQL; no adoption of another instance."""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from foundation import deployment


def ensure_running():
    metadata = deployment.metadata_path()
    mysql_root = deployment.mysql_root()
    if not metadata.is_file():
        raise RuntimeError("项目 MySQL 尚未初始化，请先执行 tools/mysql_instance.py initialize")
    v = json.loads(metadata.read_text(encoding="utf-8"))
    if v.get("host") != "127.0.0.1" or v.get("owner") != "stock-foundation":
        raise RuntimeError("MySQL 实例归属不符")
    executable, config = Path(v["executable"]), Path(v["config"])
    if (executable.resolve() != deployment.mysql_executable().resolve()
            or config.resolve() != (mysql_root / "my.ini").resolve()):
        raise RuntimeError("MySQL 路径不属于已登记的专用实例")
    from foundation import database as store

    def verify_instance():
        config = store.connection_options()
        if config["port"] != v["port"]:
            raise RuntimeError("数据库凭据与已登记实例的端口不匹配")
        with store.db(readonly=True) as connection:
            if connection.execute("SELECT @@server_uuid").fetchone()[0] != v["server_uuid"]:
                raise RuntimeError("专用端口上的数据库与已登记实例不匹配")

    with socket.socket() as s:
        s.settimeout(1)
        if s.connect_ex(("127.0.0.1", v["port"])) == 0:
            verify_instance()
            return
    subprocess.Popen([str(executable), f"--defaults-file={config}", "--standalone"],
                     cwd=executable.parent, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    for _ in range(100):
        with socket.socket() as s:
            s.settimeout(.3)
            if s.connect_ex(("127.0.0.1", v["port"])) == 0:
                verify_instance()
                return
        time.sleep(.2)
    raise RuntimeError(f"项目 MySQL 未就绪，请检查 {mysql_root / 'server.err'}")
