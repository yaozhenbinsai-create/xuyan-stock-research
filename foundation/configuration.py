"""Configuration management only. This module has no AI network transport."""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from foundation import database as store, security_secrets


class Named(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True


class ModelSave(Named):
    provider: str = Field(default="OpenAI Compatible", max_length=100)
    model_identifier: str = Field(min_length=1, max_length=160)
    base_url: str = Field(default="", max_length=500)
    api_key_env: str = Field(default="", max_length=100, pattern=r"^([A-Za-z_][A-Za-z0-9_]*)?$")
    capability: str = Field(default="文本", pattern="^文本$")
    protocol: str = Field(default="openai", pattern="^(openai|responses|anthropic|codex|gemini|xai)$")
    model_family: str = Field(default="custom", pattern="^(custom|gpt|gemini|grok|claude)$")
    connection_type: str = Field(default="api", pattern="^(api|relay|self_hosted|account)$")
    recoverable_responses: bool = False
    max_output_tokens: int | None = Field(default=None, gt=0, le=2147483647)
    timeout_seconds: int | None = Field(default=None, ge=10, le=600)
    reasoning_effort: str | None = Field(default=None, pattern="^(none|minimal|low|medium|high|xhigh|max)$")
    api_key: str | None = Field(default=None, max_length=10000)
    clear_api_key: bool = False

    @model_validator(mode="after")
    def validate_address(self):
        if self.protocol == "codex":
            self.base_url = "codex://chatgpt"
            self.api_key_env = ""
            self.api_key = None
            self.connection_type = 'account'
            self.recoverable_responses = False
        else:
            p = urlsplit(self.base_url)
            if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.query or p.fragment:
                raise ValueError("接口须为 HTTP/HTTPS 地址，不能包含密钥、账号密码或查询参数")
            if self.connection_type == 'account':
                raise ValueError('账号接入仅适用于本机官方账号协议')
            if self.recoverable_responses and self.protocol != 'responses':
                raise ValueError('原请求查询仅适用于支持后台响应的 Responses 通道')
            if self.api_key and (not self.api_key.isascii() or any(ord(ch)<33 or ord(ch)==127 for ch in self.api_key)):
                raise ValueError('API Key 含有空格、换行或无效字符')
        return self


class PlanSave(Named):
    plan_type: str = Field(default="文本分析", pattern="^文本分析$")
    model_ids: list[int] = Field(default_factory=list, min_length=1, max_length=32)
    description: str = Field(default="", max_length=4000)
    request_timeout_seconds: int | None = Field(default=None, ge=10, le=600)


class AgentSave(Named):
    role: str = Field(default="", max_length=200)
    responsibilities: str = Field(default="", max_length=8000)
    plan_ids: list[int] = Field(default_factory=list, max_length=32)


SCHEMAS = {"models": ModelSave, "plans": PlanSave, "agents": AgentSave}


def item(row, kind):
    v = json.loads(row["data"])
    v["id"] = row["id"]
    if kind == "models":
        v.setdefault('model_family','custom')
        v.setdefault('connection_type','account' if v.get('protocol')=='codex' else 'api')
        v.setdefault('recoverable_responses',False)
        v["has_api_key"] = bool(row["secret"])
        v["configured"] = bool(row["secret"] or v.get("protocol") == "codex" or os.environ.get(v.get("api_key_env", "")))
        v.pop("api_key", None)
        v['key_source'] = 'account' if v.get('protocol') == 'codex' else 'encrypted' if row['secret'] else 'environment' if os.environ.get(v.get('api_key_env', '')) else 'missing'
    return v


def list_items(kind):
    if kind not in SCHEMAS:
        raise HTTPException(404, "配置类型不存在")
    with store.db(readonly=True) as c:
        return [item(r, kind) for r in c.execute('SELECT * FROM ' + kind + " ORDER BY id")]


def model_row(model_id):
    with store.db(readonly=True) as c:
        row = c.execute("SELECT * FROM models WHERE id=%s", (model_id,)).fetchone()
    if not row:
        raise HTTPException(404, "模型连接不存在，请刷新后重试")
    return row


def decrypt_model_key(row):
    if not row["secret"]:
        return ""
    path = store.database_path()
    try:
        return security_secrets._backend(path).decrypt(row["secret"], security_secrets._context(path)).decode()
    except (security_secrets.SecretStorageError, UnicodeError):
        raise HTTPException(409, "无法解密已保存的 API Key，请核对 Windows 账户或重新填写密钥") from None


def visible_model_key(model_id):
    row = model_row(model_id)
    if item(row, "models")["protocol"] == "codex":
        return ""
    return decrypt_model_key(row)


def save(kind, raw, item_id=None):
    if kind not in SCHEMAS:
        raise HTTPException(404, "配置类型不存在")
    try:
        v = SCHEMAS[kind].model_validate(raw).model_dump()
    except ValidationError as exc:
        raise HTTPException(422, "；".join(e["msg"] for e in exc.errors(include_input=False, include_context=False))) from None
    try:
        with store.db() as c:
            old = c.execute('SELECT * FROM ' + kind + " WHERE id=%s FOR UPDATE", (item_id,)).fetchone() if item_id else None
            if item_id and old is None:
                raise HTTPException(404, "配置不存在")
            old_value = json.loads(old["data"]) if old else {}
            v.update(created_at=old_value.get("created_at", store.now()), updated_at=store.now())
            if kind == "models":
                key, clear = v.pop("api_key"), v.pop("clear_api_key")
                destination_changed = old and (v['protocol'] != old_value.get('protocol') or
                    v['base_url'].rstrip('/') != old_value.get('base_url','').rstrip('/'))
                inherited_environment = bool(old_value.get('api_key_env') and
                    v['api_key_env'] == old_value.get('api_key_env'))
                if (destination_changed and not key and v['protocol'] != 'codex'
                        and ((old['secret'] and not clear) or inherited_environment)):
                    raise HTTPException(422, '接口地址或协议已改变，请填写新连接的密钥，避免将旧密钥发送到新地址')
                secret = old["secret"] if old else None
                if clear or v["protocol"] == "codex":
                    secret = None
                elif key:
                    secret = security_secrets._backend(store.database_path()).encrypt(key.encode(), security_secrets._context(store.database_path()))
                values = (v["name"], store.dumps(v), secret)
                if old:
                    c.execute('UPDATE models SET name=%s,data=%s,secret=%s WHERE id=%s', (*values, item_id))
                else:
                    item_id = c.execute('INSERT INTO models(name,data,secret) VALUES(%s,%s,%s)', values).lastrowid
            else:
                field, target = ("model_ids", "models") if kind == "plans" else ("plan_ids", "plans")
                ids = v[field]
                if len(ids) != len(set(ids)) or any(type(i) is not int or i < 1 for i in ids):
                    raise HTTPException(422, "关联项目不能重复，编号须为正整数")
                for i in ids:
                    if not c.execute('SELECT 1 FROM ' + target + " WHERE id=%s", (i,)).fetchone():
                        raise HTTPException(422, "关联配置不存在，请刷新后重试")
                if old:
                    c.execute('UPDATE ' + kind + " SET name=%s,data=%s WHERE id=%s", (v["name"], store.dumps(v), item_id))
                else:
                    item_id = c.execute('INSERT INTO ' + kind + "(name,data) VALUES(%s,%s)", (v["name"], store.dumps(v))).lastrowid
            store.audit("保存 AI 配置", {"kind": kind, "id": item_id, "name": v["name"]}, conn=c)
            return item(c.execute('SELECT * FROM ' + kind + " WHERE id=%s", (item_id,)).fetchone(), kind)
    except store.IntegrityError:
        raise HTTPException(409, "名称已存在，请使用其他名称") from None
