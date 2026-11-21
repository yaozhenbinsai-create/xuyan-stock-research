"""Feedback creates inspectable suggestions. Applying one is explicit and versioned."""
from hashlib import sha256
import json

from fastapi import HTTPException

from foundation import database as store
from research import preferences


KEY='research_feedback_proposals'
TOPICS={
    'risk':{'label':'希望更严格地检查风险','patch':{'risk':'conservative'},'focus':'先列风险与能推翻论点的证据。'},
    'cashflow':{'label':'希望更多核对现金流','patch':{},'focus':'优先对照经营现金流和利润兑现情况。'},
    'shorter':{'label':'更关注下一交易日','patch':{'horizon':'D1'}},
    'three_days':{'label':'更关注未来三个交易日','patch':{'horizon':'D3'}},
    'main_board':{'label':'优先主板','patch':{'boards':['主板']}},
    'fewer':{'label':'每次希望少看几只','patch':{'candidate_limit':3}},
}


def _locked(conn):
    conn.execute('INSERT IGNORE INTO settings VALUES(%s,%s)',(KEY,'[]'))
    return json.loads(conn.execute('SELECT data FROM settings WHERE `key`=%s FOR UPDATE',(KEY,)).fetchone()[0])


def status():
    with store.db(readonly=True) as conn:
        current=preferences.current(conn)
        items=store.setting(KEY,[],conn=conn)
    return {'topics':{k:v['label'] for k,v in TOPICS.items()},
            'items':[{**item,'stale':item['revision']!=current['revision']} for item in items[:20]]}


def propose(raw):
    if set(raw)-{'topic','run_id','note'}:
        raise HTTPException(422,'反馈包含未知字段')
    topic,run_id=raw.get('topic'),raw.get('run_id') or None
    note=raw.get('note','')
    if not isinstance(topic,str) or topic not in TOPICS or not isinstance(note,str) or len(note)>250:
        raise HTTPException(422,'请选择反馈方向，备注最多250字')
    if run_id is not None and (not isinstance(run_id,str) or len(run_id)!=16):
        raise HTTPException(422,'研究编号无效')
    spec=TOPICS[topic]
    with store.db() as conn:
        items=_locked(conn)
        if run_id and not conn.execute('SELECT 1 FROM research_runs WHERE id=%s',(run_id,)).fetchone():
            raise HTTPException(404,'关联研究不存在')
        profile=preferences.current(conn)
        identifier=sha256(store.dumps([run_id,topic,note.strip(),profile['revision']]).encode()).hexdigest()[:24]
        old=next((p for p in items if p['id']==identifier),None)
        if old:return old
        if len([p for p in items if p['state']=='pending'])>=50:
            raise HTTPException(409,'待确认偏好已达50项，请先处理已有建议')
        suggestion={**spec['patch'],**preferences.interpret(note)}
        if spec.get('focus') and spec['focus'] not in profile.get('note',''):
            suggestion['note']='\n'.join(t for t in (profile.get('note',''),spec['focus']) if t)[:600]
        proposal={'id':identifier,'topic':topic,'label':spec['label'],'source_run':run_id,'source_note':note.strip(),
                  'created':store.now(),'revision':profile['revision'],'state':'pending','patch':suggestion}
        store.set_setting(KEY,[proposal,*items[:99]],conn=conn)
        store.audit('research_feedback_proposed',{'id':identifier,'source_run':run_id,'topic':topic},conn=conn)
        return proposal


def decide(identifier, action, confirmed):
    if action not in ('accept','dismiss') or confirmed is not True:
        raise HTTPException(422,'请确认应用或略过这条偏好建议')
    with store.db() as conn:
        items=_locked(conn)
        item=next((p for p in items if p['id']==identifier),None)
        if not item or item['state']!='pending':
            raise HTTPException(409,'偏好建议不存在或已处理')
        if action=='accept':
            conn.execute('INSERT IGNORE INTO settings VALUES(%s,%s)',(preferences.KEY,store.dumps({'revision':0,'confirmed_at':None})))
            conn.execute('SELECT data FROM settings WHERE `key`=%s FOR UPDATE',(preferences.KEY,)).fetchone()
            profile=preferences.current(conn)
            if profile['revision']!=item['revision']:
                raise HTTPException(409,'偏好已更新或撤销，请从当前版本重新生成建议')
            value={k:v for k,v in profile.items() if k in preferences.Profile.model_fields}
            preferences.save({**value,**item['patch'],'confirm':True},conn=conn)
        item.update(state='accepted' if action=='accept' else 'dismissed',resolved_at=store.now())
        store.set_setting(KEY,items,conn=conn)
        store.audit('research_feedback_decision',{'id':identifier,'action':action},conn=conn)
    return status()
