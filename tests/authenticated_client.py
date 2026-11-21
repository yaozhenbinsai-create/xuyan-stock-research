"""Use the real login endpoint against the test's isolated data directory."""
import os
import secrets
from contextlib import contextmanager
from fastapi.testclient import TestClient
from foundation import admin_access

_test_password = secrets.token_urlsafe(30)


@contextmanager
def admin_client(app, **kwargs):
    if not os.environ.get("STOCK_MYSQL_DATABASE", "").startswith("stock_test_") or not os.environ.get("STOCK_DATA_ROOT"):
        raise RuntimeError("测试登录需要独立的临时目录和测试数据库。")
    admin_access.initialize(_test_password)
    kwargs.setdefault("base_url", "http://127.0.0.1")
    with TestClient(app, **kwargs) as client:
        response = client.post("/login", data={"username": "ADMIN", "password": _test_password}, follow_redirects=False)
        assert response.status_code == 303
        yield client
