"""Isolate BaoStock's process-global socket and bound it with a parent timeout."""
from __future__ import annotations

import contextlib
import io
import json
import socket
import sys
import time


_SDK_SIGNALS = {"timed out": "sdk_timeout_reported", "sdk_peer_closed": "sdk_peer_closed",
                "接收数据异常，请稍后再试。": "sdk_receive_error_reported"}


def _sdk_signals(output):
    return sorted({_SDK_SIGNALS[line] for line in output.getvalue().splitlines() if line in _SDK_SIGNALS})


class _SdkSocketProxy:
    def __init__(self, connection):
        self.connection = connection

    def recv(self, *args, **kwargs):
        data = self.connection.recv(*args, **kwargs)
        if data == b"":
            # BaoStock 0.9.4 otherwise loops forever on an incomplete frame
            # after peer EOF. Its existing handler converts this to 10002007.
            raise ConnectionError("sdk_peer_closed")
        return data

    def __getattr__(self, name):
        return getattr(self.connection, name)


@contextlib.contextmanager
def _sdk_eof_guard():
    from baostock.common import context
    from baostock.util import socketutil
    original_connect = socketutil.SocketUtil.connect
    had_socket = hasattr(context, "default_socket")
    original_socket = getattr(context, "default_socket", None)
    connections = []

    def connect(instance, *args, **kwargs):
        result = original_connect(instance, *args, **kwargs)
        connection = getattr(context, "default_socket", None)
        if connection is not None and not isinstance(connection, _SdkSocketProxy):
            proxy = _SdkSocketProxy(connection)
            connections.append(proxy)
            context.default_socket = proxy
        return result

    socketutil.SocketUtil.connect = connect
    try:
        yield
    finally:
        socketutil.SocketUtil.connect = original_connect
        if had_socket:
            context.default_socket = original_socket
        elif hasattr(context, "default_socket"):
            del context.default_socket
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.close()


class _SDKError(RuntimeError):
    def __init__(self, phase, code):
        self.phase = phase
        self.code = code if isinstance(code, str) and code.isascii() and code.isdigit() and len(code) <= 10 else "未提供"
        label = {"login": "登录失败", "query": "查询未完成", "pagination": "分页未完成"}[phase]
        self.safe_message = f"BaoStock {label} ({self.code})"
        super().__init__(self.safe_message)


@contextlib.contextmanager
def _stage(diagnostics, phase):
    output = io.StringIO()
    started = time.monotonic()
    status = "ok"
    try:
        with contextlib.redirect_stdout(output):
            yield
        # ResultData.next() can swallow transport failure, return False and
        # leave error_code='0'. Do not publish the rows from earlier pages.
        if phase in ("login", "query", "pagination") and _sdk_signals(output):
            raise _SDKError(phase, "10002007")
    except Exception:
        status = "failed"
        raise
    finally:
        # SDK output stays in memory. Only exact, known whole lines become
        # diagnostic enums; exception/request/response text is never returned.
        signals = _sdk_signals(output)
        diagnostics.append({"phase": phase, "status": status,
                            "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
                            "sdk_signals": signals})


def query(operation, params, *, diagnostics=None):
    import baostock as bs
    from baostock.security.sectorinfo import query_stock_concept, query_stock_area
    diagnostics = [] if diagnostics is None else diagnostics
    socket.setdefaulttimeout(20)
    with _stage(diagnostics, "login"):
        login = bs.login()
        if login.error_code != "0":
            raise _SDKError("login", login.error_code)
    primary_error = None
    try:
        code, start, end = params.get("symbol", ""), params["start"], params["end"]
        methods = {
            "calendar": lambda: bs.query_trade_dates(start_date=start, end_date=end),
            "basics": lambda: bs.query_stock_basic(code=code),
            "universe": lambda: bs.query_all_stock(day=end),
            "industry": lambda: bs.query_stock_industry(code=code, date=end),
            "concept": lambda: query_stock_concept(code=code, date=end),
            "area": lambda: query_stock_area(code=code, date=end),
            "daily_all": lambda: bs.query_daily_history_k_AStock(date=end),
            "daily": lambda: bs.query_history_k_data_plus(code, "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,peTTM,pbMRQ,isST", start_date=start, end_date=end, frequency="d", adjustflag="3"),
            "minutes": lambda: bs.query_history_k_data_plus(code, "date,time,code,open,high,low,close,volume,amount,adjustflag", start_date=start, end_date=end, frequency=str(params.get("period", 5)), adjustflag="3"),
            "actions": lambda: bs.query_adjust_factor(code, start_date=start, end_date=end),
        }
        for key, fn in {"profit": bs.query_profit_data, "balance": bs.query_balance_data, "cashflow": bs.query_cash_flow_data, "growth": bs.query_growth_data}.items():
            methods[key] = lambda fn=fn: fn(code, year=int(params.get("year", end[:4])), quarter=int(params.get("quarter", 2)))
        with _stage(diagnostics, "query"):
            result = methods[operation]()
            if result.error_code != "0":
                raise _SDKError("query", result.error_code)
        rows = []
        with _stage(diagnostics, "pagination"):
            while result.next():
                rows.append(dict(zip(result.fields, result.get_row_data())))
            if result.error_code != "0":
                raise _SDKError("pagination", result.error_code)
        return rows
    except Exception as exc:
        primary_error = exc
        raise
    finally:
        try:
            with _stage(diagnostics, "logout"):
                bs.logout()
        except Exception:
            if primary_error is None:
                raise


def execute(request):
    diagnostics = []
    try:
        with contextlib.redirect_stdout(io.StringIO()), _sdk_eof_guard():
            rows = query(request["operation"], request["params"], diagnostics=diagnostics)
        return {"rows": rows}
    except Exception as exc:
        failed = next((entry["phase"] for entry in diagnostics if entry["status"] == "failed"), None)
        return {"error": exc.safe_message if isinstance(exc, _SDKError) else "BaoStock 本地调用未完成",
                "kind": "sdk_error" if isinstance(exc, _SDKError) else "worker_error",
                "diagnostics": {"failed_phase": failed, "stages": diagnostics}}


if __name__ == "__main__":
    try:
        request = json.loads(sys.stdin.read())
        response = execute(request)
    except Exception:
        response = {"error": "BaoStock 请求无法解析", "kind": "worker_error"}
    sys.stdout.write(json.dumps(response, ensure_ascii=True, allow_nan=False))
