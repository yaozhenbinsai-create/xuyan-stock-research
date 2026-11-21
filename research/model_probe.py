"""User-initiated connection diagnostics, independent of the research queue."""
from __future__ import annotations

import hashlib
import json
import os
from threading import Lock
import time

from fastapi import HTTPException
import httpx
from pydantic import ValidationError

from foundation import configuration, database as store
from research import codex_native, model_transport, protocols


_active_test = Lock()
_fields = set(configuration.ModelSave.model_fields) - {"clear_api_key"}


def _resolve(raw, *, catalog=False):
    model_id = raw.get("model_id")
    if model_id is not None and (type(model_id) is not int or model_id < 1):
        raise HTTPException(422, "模型编号无效")
    row = configuration.model_row(model_id) if model_id is not None else None
    saved = configuration.item(row, "models") if row else {}
    if set(raw) - _fields - {"model_id"}:
        raise HTTPException(422, "连接设置包含不支持的字段")
    values = {k: saved[k] for k in _fields if k in saved}
    values.update({k: v for k, v in raw.items() if k in _fields})
    values["name"] = values.get("name") or "连接测试"
    if catalog:
        values["model_identifier"] = values.get("model_identifier") or "catalog"
    try:
        model = configuration.ModelSave.model_validate(values).model_dump(exclude={"clear_api_key"})
    except ValidationError:
        raise HTTPException(422, "请核对接入协议、接口地址和模型标识") from None
    supplied = model.pop("api_key")
    protocol = model["protocol"]
    key = ""
    if protocol != "codex":
        if supplied:
            key = supplied
        else:
            same_endpoint = not row or (protocol == saved["protocol"] and
                model["base_url"].rstrip("/") == saved["base_url"].rstrip("/"))
            if not same_endpoint:
                raise HTTPException(422, "接口地址或协议已修改；请填写用于新地址的 API Key 后再测试")
            key = configuration.decrypt_model_key(row) if row and row["secret"] else ""
            key = key or os.environ.get(model["api_key_env"], "")
        if not key:
            raise HTTPException(422, "请先填写 API Key，或配置可用的密钥环境变量")
        if not key.isascii() or any(ord(ch) < 33 or ord(ch) == 127 for ch in key):
            raise HTTPException(422, "API Key 含有无效字符，请检查是否粘贴了空格或换行")
    # Only an unchanged connection is attributed to the saved model.
    matches = row and not supplied and all(model[k] == saved.get(k) for k in model)
    return {**model, "id": model_id if matches else 0}, key


def catalog(raw):
    model, key = _resolve(raw, catalog=True)
    protocol = model["protocol"]
    timeout = min(model["timeout_seconds"] or 20, 20)
    started = time.monotonic()
    try:
        if protocol == "codex":
            result = codex_native.list_models(timeout)
        else:
            base = protocols.base_url(model['base_url'],protocol)
            if protocol == "anthropic":
                headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
            elif protocol == 'gemini':
                headers = {'x-goog-api-key':key}
            else:
                headers = {"Authorization": "Bearer " + key}
            entries=[];params={};more=False
            with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                for page in range(10):
                    with client.stream('GET',base+'/models',headers=headers,params=params) as response:
                        if response.status_code != 200:
                            raise HTTPException(502,f'读取模型目录失败（HTTP {response.status_code}）。可手填模型标识后测试连接。')
                        content=bytearray()
                        for chunk in response.iter_bytes():
                            content.extend(chunk)
                            if len(content)>1_000_000 or time.monotonic()-started>timeout:
                                raise HTTPException(502,'模型目录响应过大或超时，请手填模型标识')
                    result=json.loads(content)
                    field='models' if protocol=='gemini' else 'data'
                    if not isinstance(result,dict) or not isinstance(result.get(field),list):
                        raise HTTPException(502,'服务商未返回可用的模型目录，请手填模型标识')
                    entries.extend(result[field])
                    token=result.get('nextPageToken') if protocol=='gemini' else result.get('last_id') if protocol=='anthropic' and result.get('has_more') else None
                    more=bool(token or result.get('has_more'))
                    if not token or len(entries)>=500:break
                    params={'pageToken':token} if protocol=='gemini' else {'after_id':token}
            result={'data':entries,'has_more':more}
    except codex_native.NativeFailure as exc:
        raise HTTPException(502, str(exc)) from None
    except (httpx.HTTPError, OSError, ValueError):
        raise HTTPException(502, "未能读取模型目录，请核对地址和网络，也可以手填模型标识") from None
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise HTTPException(502, "服务商未返回可用的模型目录，请手填模型标识")
    ids = {}
    for entry in result["data"]:
        if not isinstance(entry, dict) or entry.get("hidden"):
            continue
        identifier = (entry.get('name') if protocol=='gemini' else entry.get("model") if protocol == "codex" else None) or entry.get("id")
        if isinstance(identifier, str) and 0 < len(identifier) <= 160 and (not key or key not in identifier):
            ids[identifier] = None
    return {"models": list(ids)[:250], "more": len(ids) > 250 or bool(result.get("has_more") or result.get("nextCursor"))}


def test_connection(raw):
    model, key = _resolve(raw)
    if not _active_test.acquire(blocking=False):
        raise HTTPException(409, "已有连接测试正在进行，请等待结果")
    try:
        started = time.monotonic()
        frozen = {
            "model": model,
            "config_hash": hashlib.sha256(store.dumps(model).encode()).hexdigest(),
            "budget": {"max_completion_tokens": min(model["max_output_tokens"] or 256, 256),
                       "timeout_seconds": min(model["timeout_seconds"] or 45, 45)},
            "attempt_context": {"origin": "connection_test", "model_identifier":
                                "" if key and key in model["model_identifier"] else model["model_identifier"]},
        }
        try:
            _, call_id = model_transport.complete(
                None, frozen, '这是连接测试。仅返回 JSON 对象 {"ok":true}。',
                {"purpose": "connection_test"}, probe_key=key)
        except model_transport.ModelFailure as exc:
            if exc.unknown:
                message = "连接中断、超时或响应未结束，服务商最终状态未知"
            elif exc.category in {"http_401", "http_403"}:
                message = f"鉴权失败或没有模型权限（HTTP {exc.category[5:]}），请核对 API Key 和模型标识"
            elif exc.category == "incomplete":
                message = "服务商已响应，但测试输出未完成"
            else:
                message = str(exc).replace("，未签发报告", "")
            return {"ok": False, "unknown": exc.unknown, "message": message,
                    "call_id": getattr(exc, "call_id", None), "elapsed_ms": round((time.monotonic() - started) * 1000)}
        return {"ok": True, "message": "连接成功，模型已返回结果", "call_id": call_id,
                "elapsed_ms": round((time.monotonic() - started) * 1000)}
    finally:
        _active_test.release()
