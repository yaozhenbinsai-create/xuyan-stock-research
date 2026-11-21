"""Editable, explicitly confirmed user preferences for future research."""
import json
import re

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Literal

from foundation import assets, database as store


KEY = 'research_preferences'
BOARDS = ('主板', '创业板', '科创板', '北交所')


class Profile(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)
    note: str = Field(default='', max_length=600)
    horizon: Literal['D1', 'D3'] = 'D3'
    risk: Literal['conservative', 'balanced', 'opportunistic'] = 'balanced'
    boards: list[str] = Field(default_factory=list, max_length=4)
    excluded_symbols: list[str] = Field(default_factory=list, max_length=30)
    candidate_limit: int = Field(default=8, ge=1, le=8)
    max_industry_recommendations: int | None = Field(default=None, ge=1, le=8)


def current(conn=None):
    return store.setting(KEY, {'revision': 0, 'confirmed_at': None}, conn=conn)


def save(raw, *, conn=None):
    if conn is None:
        with store.db() as conn:
            return save(raw,conn=conn)
    value = dict(raw)
    confirm = value.pop('confirm', None)
    revoke = value.pop('revoke', None)
    if confirm is not True:
        raise HTTPException(422, '请明确确认保存或撤销研究偏好')
    # Lock the settings row so concurrent edits create distinct revisions.
    conn.execute('INSERT IGNORE INTO settings VALUES(%s,%s)', (KEY, store.dumps({'revision': 0, 'confirmed_at': None})))
    previous = conn.execute('SELECT data FROM settings WHERE `key`=%s FOR UPDATE', (KEY,)).fetchone()
    revision = json.loads(previous[0])['revision'] + 1
    if revoke is True:
        result = {'revision': revision, 'confirmed_at': None}
    else:
        try:
            result = Profile.model_validate(value).model_dump()
        except ValidationError:
            raise HTTPException(422, '研究偏好格式无效') from None
        if set(result['boards']) - set(BOARDS):
            raise HTTPException(422, '板块范围无效')
        result['boards'] = list(dict.fromkeys(result['boards']))
        result['excluded_symbols'] = list(dict.fromkeys(assets.symbol(s) for s in result['excluded_symbols']))
        result.update(revision=revision, confirmed_at=store.now())
    store.set_setting(KEY, result, conn=conn)
    store.audit('research_preference', {'revision': revision, 'active': bool(result['confirmed_at'])}, conn=conn)
    return result


def context(run, conn=None):
    revision = run.get('preference_revision')
    if not revision:
        return {}
    profile = current(conn)
    if not profile.get('confirmed_at') or profile['revision'] != revision:
        raise HTTPException(409, '本轮使用的长期偏好已修改或撤销，请按现行偏好重新发起')
    return profile


def interpret(intent):
    """Recognize only explicit constraints, leaving other language to the model."""
    result={}
    board=re.search(r'(?:只看|仅看|只研究|仅研究)\s*(沪深主板|主板|创业板|科创板|北交所)(?:[，。；、\s]|$)',intent)
    limit=re.search(r'(?:候选)?最多\s*([1-8])\s*只',intent)
    horizon=re.search(r'(?:期限|周期|只看)\s*[:：]?\s*(D1|D3)(?![\w])',intent,re.I)
    if board:result['boards']=['主板' if board[1]=='沪深主板' else board[1]]
    if limit:result['candidate_limit']=int(limit[1])
    if horizon:result['horizon']=horizon[1].upper()
    return result
