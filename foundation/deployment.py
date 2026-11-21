"""Local deployment identity; business schedules remain in the shared database."""
from pathlib import Path
from functools import lru_cache
import json
import os

ROOT = Path(__file__).resolve().parents[1]

@lru_cache(maxsize=1)
def settings():
    path = ROOT / 'deployment.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}

def workers_enabled():
    return os.environ.get('STOCK_DISABLE_WORKER') != '1' and settings().get('workers_enabled', False)

def mysql_root():
    return ROOT / 'data' / 'mysql'

def mysql_executable():
    matches = list((ROOT / 'tools' / 'mysql').glob('mysql-8.4.*-winx64/bin/mysqld.exe'))
    if len(matches) != 1 or not matches[0].is_file():
        raise RuntimeError('请将一份官方 MySQL 8.4 Windows ZIP 解压至 tools/mysql/。')
    return matches[0]

def metadata_path():
    return mysql_root() / 'instance.json'

def credentials_path(*, admin=False):
    return ROOT / 'data' / ('mysql-admin.enc' if admin else 'mysql.enc')

def backups_path():
    database = os.environ.get('STOCK_MYSQL_DATABASE', '')
    isolated_data = os.environ.get('STOCK_DATA_ROOT')
    if database.startswith(('stock_test_', 'stock_restore_')) and isolated_data:
        return Path(isolated_data).resolve() / 'backups'
    from foundation import database as store
    return store.data_root() / 'backups'

def server_path():
    return mysql_root() / 'server'
