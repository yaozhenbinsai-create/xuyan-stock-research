"""Transactional local delivery and an explicitly configured HTTPS handoff."""
from __future__ import annotations

import hashlib
import json
import os
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from foundation import database as store, deployment
from research import state_store


def _target():
    if not deployment.settings().get("enable_webhook_environment", False):
        return None, None
    raw = os.environ.get('STOCK_RESEARCH_WEBHOOK', '')
    if os.environ.get('STOCK_RESEARCH_WEBHOOK_ENABLED') != '1' or not raw:
        return None, None
    try:
        url = urlsplit(raw)
        if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
            return None, None
    except ValueError:
        return None, None
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def register(conn, run_id, kind, fields):
    _, fingerprint = _target()
    key = hashlib.sha256(store.dumps([run_id,kind,fields]).encode()).hexdigest()
    result = conn.execute('INSERT IGNORE INTO research_notices(notice_key,run_id,kind,created,channel_state,target_hash,data) '
                          'VALUES(%s,%s,%s,%s,%s,%s,%s)',
                          (key,run_id,kind,store.now(),'pending' if fingerprint else 'not_configured',fingerprint,store.dumps(fields)))
    identifier = conn.execute('SELECT id FROM research_notices WHERE notice_key=%s',(key,)).fetchone()[0]
    state_store.sync_notice(conn,identifier)
    return identifier, result.rowcount == 1


def _item(row):
    return {k:row[k] for k in ('id','run_id','kind','created','read_at','channel_state')} | {'data':json.loads(row['data'])}


def page(limit, before=None):
    with store.db(readonly=True) as conn:
        rows = conn.execute('SELECT * FROM research_notices'+(' WHERE id<%s' if before else '')+
                            ' ORDER BY id DESC LIMIT %s', (before,limit+1) if before else (limit+1,)).fetchall()
        unread = conn.execute('SELECT count(*) FROM research_notices WHERE read_at IS NULL').fetchone()[0]
    return {'items':[_item(row) for row in rows[:limit]],'unread':unread,
            'next':rows[limit-1]['id'] if len(rows)>limit else None,'external_configured':bool(_target()[0])}


def corrections(conn, run_id):
    rows = conn.execute("SELECT id,created,data FROM research_notices WHERE run_id=%s AND kind='correction' ORDER BY id DESC LIMIT 50",
                        (run_id,)).fetchall()
    return [{'id':row['id'],'at':row['created'],**{k:v for k,v in json.loads(row['data']).items() if k!='attempts'}} for row in rows]


def mark_read(identifier):
    with store.db() as conn:
        row = conn.execute('SELECT id FROM research_notices WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
        if not row:
            raise HTTPException(404,'站内通知不存在')
        conn.execute('UPDATE research_notices SET read_at=COALESCE(read_at,%s) WHERE id=%s',(store.now(),identifier))
    return {'id':identifier,'read':True}


def send(identifier, confirmed, acknowledge_unknown=False):
    if confirmed is not True:
        raise HTTPException(422,'请明确确认将这条通知发送到已配置的渠道')
    destination, fingerprint = _target()
    if not destination:
        raise HTTPException(409,'外部渠道尚未配置')
    with store.db() as conn:
        row = conn.execute('SELECT * FROM research_notices WHERE id=%s FOR UPDATE',(identifier,)).fetchone()
        if not row:
            raise HTTPException(404,'通知不存在')
        if row['target_hash'] != fingerprint:
            raise HTTPException(409,'该通知未登记到当前外部渠道')
        if row['channel_state']=='accepted':
            return _item(row)
        if row['channel_state']=='sending' or row['channel_state']=='unknown' and acknowledge_unknown is not True:
            raise HTTPException(409,'上次外部发送仍在处理或结果未知，重投前请核查')
        data = json.loads(row['data'])
        attempts = data.setdefault('attempts',[])
        if len(attempts)>=8:
            raise HTTPException(409,'该通知已达到8次投递上限')
        attempts.append({'started':store.now(),'status':'sending'})
        conn.execute("UPDATE research_notices SET channel_state='sending',data=%s WHERE id=%s",
                     (store.dumps(data),identifier))
        state_store.sync_notice(conn,identifier)
    # Share a stable identity and compact summary only; no prices, prompts or provider credentials.
    payload = {'event_id':row['notice_key'],'report_id':row['run_id'],'kind':row['kind'],'published_at':row['created']}
    for key in ('title','market_day','session','symbol','action'):
        if key in data:
            payload[key]=data[key]
    try:
        with httpx.Client(timeout=8,follow_redirects=False,trust_env=False) as client:
            result = client.post(destination,json=payload,headers={'Idempotency-Key':row['notice_key']})
        status = 'accepted' if 200 <= result.status_code < 300 else 'failed'
        attempts[-1]['http_status']=result.status_code
    except httpx.RequestError:
        status = 'unknown'
    attempts[-1].update(status=status,finished=store.now())
    with store.db() as conn:
        conn.execute('UPDATE research_notices SET channel_state=%s,data=%s WHERE id=%s AND channel_state=\'sending\'',
                     (status,store.dumps(data),identifier))
        state_store.sync_notice(conn,identifier)
        updated = conn.execute('SELECT * FROM research_notices WHERE id=%s',(identifier,)).fetchone()
    return _item(updated)


def recover(conn):
    # A process death after dispatch cannot establish the remote outcome.
    conn.execute("UPDATE research_notices SET channel_state='unknown' WHERE channel_state='sending'")
