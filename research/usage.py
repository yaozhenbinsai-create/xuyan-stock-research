"""Provider usage is measurement, not a configured token cap or a byte estimate."""
from __future__ import annotations
import json
from foundation import database as store

def _n(value):
    return value if type(value) is int and value>=0 else None

def normalize(raw, protocol=None):
    if not isinstance(raw,dict):return {'input':None,'output':None,'total':None,'cached_input':None,'reasoning_output':None}
    def first(*keys):
        return next((_n(raw[k]) for k in keys if _n(raw.get(k)) is not None),None)
    inp=first('input_tokens','prompt_tokens','inputTokens','promptTokenCount')
    out=first('output_tokens','completion_tokens','outputTokens','candidatesTokenCount')
    if protocol=='gemini' and out is not None:
        out += first('thoughtsTokenCount') or 0
    total=first('total_tokens','totalTokens','totalTokenCount')
    if protocol=='anthropic' and inp is not None:
        inp += (first('cache_read_input_tokens') or 0) + (first('cache_creation_input_tokens') or 0)
    detail_in=raw.get('prompt_tokens_details') or raw.get('input_tokens_details') or {}
    detail_out=raw.get('completion_tokens_details') or raw.get('output_tokens_details') or {}
    cached=first('cachedInputTokens','cache_read_input_tokens','cachedContentTokenCount')
    if cached is None:cached=_n(detail_in.get('cached_tokens'))
    reasoning=first('reasoningOutputTokens','thoughtsTokenCount')
    if reasoning is None:reasoning=_n(detail_out.get('reasoning_tokens'))
    # Native xAI Chat reports visible completion and reasoning separately. Some
    # relays already return their sum; an explicit matching total identifies that
    # normalized form and prevents adding the same reasoning a second time.
    if (protocol=='xai' and _n(raw.get('output_tokens')) is None
            and _n(raw.get('completion_tokens')) is not None and out is not None and reasoning is not None
            and not (total is not None and inp is not None and total==inp+out)):
        out+=reasoning
    if total is None and inp is not None and out is not None:total=inp+out
    return {'input':inp,'output':out,'total':total,'cached_input':cached,'reasoning_output':reasoning}

def fields(raw,*,complete,protocol=None):
    value=normalize(raw,protocol)
    measured=any(x is not None for x in value.values())
    return {'usage':raw,'usage_normalized':value,'usage_state':'complete' if complete and value['total'] is not None else 'partial' if measured else 'unknown'}

def summary(run_id=None):
    where=' WHERE run_id=%s' if run_id else '';params=(run_id,) if run_id else ()
    with store.db(readonly=True) as c:
        grouped=c.execute("SELECT model_id,count(*) attempts,SUM(status='completed') completed,"+
          "SUM(CASE WHEN usage_state='complete' THEN 1 ELSE 0 END) usage_complete,"
          "SUM(CASE WHEN usage_state='partial' THEN 1 ELSE 0 END) usage_partial,"+
          ','.join('SUM('+key+'_tokens) `'+key+'`' for key in ('input','output','total','cached_input','reasoning_output'))+
          ' FROM research_calls'+where+' GROUP BY model_id',params).fetchall()
        names={r['id']:r['name'] for r in c.execute('SELECT id,name FROM models ORDER BY id')}
    total=sum(int(r['attempts']) for r in grouped)
    items=[]
    for r in grouped:
        d=dict(r)
        for k in ('attempts','completed','usage_complete','usage_partial','input','output','total','cached_input','reasoning_output'):
            if d[k] is not None:d[k]=int(d[k])
        d['model_name']=names.get(d['model_id'],str(d['model_id']) if d['model_id'] else '未保存配置的连接测试');d['usage_unknown']=d['attempts']-d['usage_complete']-d['usage_partial'];items.append(d)
    seen={d['model_id'] for d in items}
    for mid,name in names.items():
        if mid not in seen:items.append({'model_id':mid,'model_name':name,'attempts':0,'completed':0,'usage_complete':0,'usage_partial':0,'usage_unknown':0,**{k:0 for k in ('input','output','total','cached_input','reasoning_output')}})
    items.sort(key=lambda x:x['model_id'])
    def subtotal(key):
        known=[r[key] for r in items if r['attempts'] and r[key] is not None]
        return sum(known) if known else 0 if total==0 else None
    complete=sum(r['usage_complete'] for r in items);partial=sum(r['usage_partial'] for r in items)
    return {'attempts':total,'usage_complete':complete,'usage_partial':partial,'usage_unknown':total-complete-partial,
      'reported':{k:subtotal(k) for k in ('input','output','total','cached_input','reasoning_output')},'by_model':items,
      'actual_total_known':total==complete,'cost':None,
      'basis':'只汇总供应商实测回执；未知不计0。缓存是输入子集，推理是输出子集，不额外加到总量。上限和字节数不是TOKEN实耗。'}

def calls(limit=25,before=None,run_id=None):
    where=[];params=[]
    if run_id:where.append('c.run_id=%s');params.append(run_id)
    if before:
        with store.db(readonly=True) as c:r=c.execute('SELECT started,id FROM research_calls WHERE id=%s',(before,)).fetchone()
        if not r:
            from fastapi import HTTPException
            raise HTTPException(409,'调用游标失效，请刷新')
        where.append('(c.started,c.id)<(%s,%s)');params.extend([r['started'],r['id']])
    sql=' WHERE '+' AND '.join(where) if where else ''
    with store.db(readonly=True) as c:
        rows=c.execute('SELECT c.*,m.name model_name FROM research_calls c LEFT JOIN models m ON m.id=c.model_id'+sql+' ORDER BY c.started DESC,c.id DESC LIMIT %s',(*params,limit+1)).fetchall()
    result=[{**dict(r),'data':json.loads(r['data'])} for r in rows[:limit]]
    return {'items':result,'next':result[-1]['id'] if len(rows)>limit else None}
