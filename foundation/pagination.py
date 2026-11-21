"""Signed, expiring keyset cursors, bound to filters and dataset generation."""
import base64
import hashlib
import hmac
import json
import time
from fastapi import HTTPException
from foundation import database as store, security_secrets


import secrets
import threading

_LOCK = threading.Lock()


def _key():
    # First use creates the signing key through the same encrypted store; an existing key is never replaced.
    path = store.ROOT / "data" / "cursor-key.enc"
    with _LOCK:
        if not security_secrets.load_json(path).get("key"):
            security_secrets.save_json(path, {"key": secrets.token_hex(32)})
        return _stored_key()


def _stored_key():
    value = security_secrets.load_json(store.ROOT / "data" / "cursor-key.enc")
    if not value.get("key"):
        raise RuntimeError("分页签名密钥尚未初始化")
    return bytes.fromhex(value["key"])


def filter_hash(dataset, start, end, stock, limit):
    return hashlib.sha256(store.dumps([dataset, start, end, stock, limit]).encode()).hexdigest()


def encode(value):
    raw = store.dumps({**value, "exp": int(time.time()) + 900}).encode()
    return base64.urlsafe_b64encode(raw + hmac.digest(_key(), raw, "sha256")).decode().rstrip("=")


def decode(value, expected):
    try:
        if len(value) > 4096:
            raise ValueError()
        data = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        raw, signature = data[:-32], data[-32:]
        if not hmac.compare_digest(signature, hmac.digest(_key(), raw, "sha256")):
            raise ValueError()
        result = json.loads(raw)
        if result["filter"] != expected or result["exp"] < time.time():
            raise ValueError()
        return result
    except (KeyError, ValueError, TypeError):
        raise HTTPException(422, "分页已过期或与筛选条件不符，请刷新列表") from None
