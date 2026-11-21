"""Local ADMIN verifier and in-memory, expiring browser sessions."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

from foundation import database

COOKIE = "xuyan_session"
MAX_AGE = 8 * 60 * 60
_sessions: dict[str, float] = {}
_lock = threading.Lock()


def account_path() -> Path:
    return database.data_root() / "web-admin.json"


def derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=32768, r=8, p=1, maxmem=64 * 1024**2, dklen=32)


def initialize(password: str) -> bool:
    path = account_path()
    if path.exists():
        read_account()
        return False
    if not 12 <= len(password) <= 160:
        raise ValueError("管理员密码需要 12 到 160 个字符。")
    salt = secrets.token_bytes(16)
    data = {"username": "ADMIN", "salt": salt.hex(), "verifier": derive(password, salt).hex()}
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temp_path = Path(file.name)
        json.dump(data, file)
    try:
        try:
            os.link(temp_path, path)
        except FileExistsError:
            read_account()
            return False
    finally:
        temp_path.unlink(missing_ok=True)
    return True


def read_account() -> tuple[bytes, bytes]:
    try:
        value = json.loads(account_path().read_text(encoding="utf-8"))
        salt, verifier = bytes.fromhex(value["salt"]), bytes.fromhex(value["verifier"])
        if value["username"] != "ADMIN" or len(salt) != 16 or len(verifier) != 32:
            raise ValueError("invalid verifier")
        return salt, verifier
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError("请使用 tools/admin_account.py 初始化 ADMIN，或恢复本机账号文件。") from error


def authenticate(username: str, password: str) -> bool:
    if username != "ADMIN" or not 12 <= len(password) <= 160:
        return False
    salt, verifier = read_account()
    return hmac.compare_digest(derive(password, salt), verifier)


def issue() -> str:
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _lock:
        for item in [key for key, expires in _sessions.items() if expires <= now]:
            del _sessions[item]
        _sessions[token] = now + MAX_AGE
    return token


def valid(token: str | None) -> bool:
    if not token:
        return False
    with _lock:
        return _sessions.get(token, 0) > time.monotonic()


def revoke(token: str | None) -> None:
    with _lock:
        _sessions.pop(token, None)
