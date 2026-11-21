"""Targeted acquisition through the existing source queue and its limits."""
from datetime import date, datetime, timedelta
import json
import re
import time

from fastapi import HTTPException
from foundation import assets, collection, database as store, record_store
from research import evidence, sessions


TYPES = {
    'announcements': ('cninfo_announcements', '近期公告索引', 21),
    'originals': ('cninfo_text', '已索引公告原文', 45),
    'actions': ('bs_actions', '公司行动与区间覆盖', 110),
    'financials': ('bs_profit', '财务对比资料', 30),
    'cashflow': ('bs_cashflow', '经营现金流', 30),
    'flow': ('sina_flow_history', '近期资金流', 10),
    'quote': ('tx_quotes', '当前参考行情', 0),
}

_FOCUS = re.compile(r'权益分派|除权除息|分红|送股|转增|权益变动|季度报告|半年度报告|年度报告')
_RISK_FOCUS = re.compile(r'交易异常波动|资产减值|延期履行承诺')
_PENDING = ('queued','running','waiting')


def financial_periods(day):
    point=date.fromisoformat(day)
    ended=[(year,q) for year in (point.year-1,point.year)
           for q,end in enumerate(('03-31','06-30','09-30','12-31'),1)
           if f'{year}-{end}'<day]
    year,quarter=max(ended)
    return [f'{year}Q{quarter}',f'{year-1}Q{quarter}']


def _original_ids(symbols, start, end):
    chosen = []
    with store.db(readonly=True) as conn:
        for symbol in symbols:
            rows = conn.execute(
                "SELECT f.id FROM heads h JOIN facts f ON h.fact_id=f.id JOIN announcements a ON a.fact_id=f.id "
                "WHERE h.dataset='announcements' AND h.symbol=%s AND h.day BETWEEN %s AND %s "
                "AND LOWER(a.url) LIKE 'https://static.cninfo.com.cn/%%.pdf' "
                "ORDER BY h.day DESC,f.id DESC LIMIT 80",(symbol,start,end)).fetchall()
            records = [(r['id'],r['payload']) for r in record_store.hydrate(conn,rows)]
            risk = [fid for fid,doc in records if evidence.RISK_ALERT.search(doc['title']) or _RISK_FOCUS.search(doc['title'])]
            focus = [fid for fid,doc in records if _FOCUS.search(doc['title']) and fid not in risk]
            pending = {fid for fid,doc in records if not doc.get('text')}
            risk_pending=[fid for fid in risk if fid in pending]
            focus_pending=[fid for fid in focus if fid in pending]
            balanced=risk_pending[:1]+focus_pending[:1]
            balanced+= [fid for fid in risk_pending+focus_pending if fid not in balanced]
            chosen += balanced[:2]
    return chosen


def acquire(symbols, day, session, publish_until, check=lambda:None):
    """Collect only shortlisted public records, then freeze whatever actually arrived."""
    now = datetime.fromisoformat(store.now())
    if not symbols or store.setting('automation_paused',True):
        return []
    symbols = list(dict.fromkeys(symbols))[:8]
    remaining = sessions.remaining_seconds(publish_until)
    budget = {'noon':180,'tail':180,'close':300}[session]
    if remaining is not None:
        # Preserve a model/signing window without imposing a fixed 500-second
        # subtraction that left the tail evidence queue only twenty seconds.
        budget = min(budget,int(remaining)-min(180,int(remaining*.6))-15)
    if budget < 8:
        return []
    expires = (now+timedelta(seconds=budget)).isoformat()
    end = now.date().isoformat()
    notice_start = (date.fromisoformat(day)-timedelta(days=45)).isoformat()
    # Expanded valid-bar history can predate 120 calendar days after a long
    # suspension. Match its one-year bound without adding per-symbol requests.
    action_start = (date.fromisoformat(day)-timedelta(days=365)).isoformat()
    items = []

    def submit(source, start, end, records=None, periods=None):
        try:
            kwargs={'trigger':'research_evidence','expires_at':expires,'records':records,'continue_after_expiry':True}
            if periods is not None:kwargs['financial_periods']=periods
            job = collection.submit({'source':source,'start':start,'end':end,'symbols':symbols},**kwargs)
        except HTTPException as exc:
            items.append({'source':source,'status':'blocked','detail':exc.detail})
        else:
            items.append({'source':source,'job_id':job['id']})

    def update():
        active = False
        for item in items:
            if 'job_id' in item:
                job = collection.get_job(item['job_id'])
                item.update({key:job[key] for key in ('status','done','total','received')})
                active |= job['status'] in _PENDING
        return active

    def wait(until):
        while update() and sessions.remaining_seconds(until)>0:
            check()
            time.sleep(min(2,max(0,sessions.remaining_seconds(until))))

    # Read already-indexed originals immediately; fresh indexes are checked
    # again after the first half of the wait, without resubmitting those IDs.
    original_ids=set(_original_ids(symbols,notice_start,end))
    if original_ids:submit('cninfo_text',notice_start,end,records=sorted(original_ids))
    submit('cninfo_announcements',notice_start,end)
    submit('bs_actions',action_start,day)
    financial_start=(date.fromisoformat(day)-timedelta(days=365)).isoformat()
    periods=financial_periods(day)
    submit('bs_profit',financial_start,day,periods=periods)
    submit('bs_cashflow',financial_start,day,periods=periods)
    wait((now+timedelta(seconds=budget//2)).isoformat())
    check()
    records = [fid for fid in _original_ids(symbols,notice_start,end) if fid not in original_ids]
    if records and sessions.remaining_seconds(expires)>0:
        submit('cninfo_text',notice_start,end,records=records)
    wait(expires)
    return items


def _warm_pending(job_id,state,expires,now):
    job=collection.get_job(job_id)
    if job['status'] in ('failed','interrupted') and now<expires:
        retries=state.setdefault('retry_counts',{})
        if retries.get(job_id,0)<3:
            retry_after=state.setdefault('retry_after',{})
            due=retry_after.setdefault(job_id,time.time()+900)
            if time.time()>=due:
                collection.control(job_id,'resume')
                retries[job_id]=retries.get(job_id,0)+1
                retry_after[job_id]=time.time()+900
            return True
    return job['status'] in _PENDING


def warm_recent():
    """Prepare recent shortlists before intraday signing, without model calls."""
    now=store.now();day=now[:10]
    if now[11:16]<'08:40' or now[11:16]>='11:00' or store.setting('automation_paused',True):return
    key='research_evidence_warm:'+day
    with store.db(readonly=True) as conn:
        if not sessions.config(conn)['enabled'] or sessions.trading_day(conn,day) is not True:return
        state=store.setting(key,{},conn=conn)
        if state.get('phase')=='completed':return
        symbols=state.get('symbols',[])
        if not symbols:
            rows=conn.execute("SELECT c.symbol FROM (SELECT id,created FROM research_runs WHERE report IS NOT NULL "
                              "ORDER BY created DESC,id DESC LIMIT 3) r JOIN research_candidates c ON c.run_id=r.id "
                              "ORDER BY r.created DESC,r.id DESC,c.sequence_no").fetchall()
            symbols=list(dict.fromkeys(row[0] for row in rows))[:24]
    if not symbols:return
    expires=sessions.local_time(day,'11:10')
    state.update(symbols=symbols,phase=state.get('phase','collecting'),jobs=state.get('jobs',{}))
    jobs=state['jobs']
    sources={'cninfo_announcements':45,'bs_actions':365,'bs_profit':365,'bs_cashflow':365}
    if state['phase']=='collecting':
        for source,days in sources.items():
            if source in jobs:continue
            start=(date.fromisoformat(day)-timedelta(days=days)).isoformat()
            kwargs={'trigger':'research_evidence','expires_at':expires,'continue_after_expiry':True}
            if source in ('bs_profit','bs_cashflow'):kwargs['financial_periods']=financial_periods(day)
            try:job=collection.submit({'source':source,'start':start,'end':day,'symbols':symbols},**kwargs)
            except HTTPException as exc:
                state['last_error']=str(exc.detail);continue
            jobs[source]=job['id']
        store.set_setting(key,state)
        if len(jobs)<len(sources):return
        waiting=any([_warm_pending(job_id,state,expires,now) for job_id in jobs.values()])
        store.set_setting(key,state)
        if waiting:return
        state['phase']='reading'
    if state['phase']=='reading':
        start=(date.fromisoformat(day)-timedelta(days=45)).isoformat()
        if 'originals' not in jobs:
            # Existing per-job original limits remain; split rather than silently
            # dropping filings when prewarming more than eight candidates.
            original_jobs=[]
            for offset in range(0,len(symbols),8):
                group=symbols[offset:offset+8];records=_original_ids(group,start,day)
                if records:
                    try:job=collection.submit({'source':'cninfo_text','start':start,'end':day,'symbols':group},
                                              trigger='research_evidence',expires_at=expires,records=records,
                                              continue_after_expiry=True)
                    except HTTPException as exc:
                        state['last_error']=str(exc.detail);store.set_setting(key,state);return
                    original_jobs.append(job['id'])
            jobs['originals']=original_jobs
        if any([_warm_pending(j,state,expires,now) for j in jobs['originals']]):
            store.set_setting(key,state);return
        receipts={source:collection.get_job(j)['status'] for source,j in jobs.items() if source!='originals'}
        original_states=[collection.get_job(j)['status'] for j in jobs['originals']]
        state.update(phase='completed' if all(s=='completed' for s in [*receipts.values(),*original_states]) else 'limited',
                     finished_at=store.now(),receipts={**receipts,'originals':original_states})
        store.set_setting(key,state)


def options():
    sources = {s['id']:s for s in assets.sources()}
    return [{'id':key,'source':source,'label':label,'enabled':bool(sources.get(source,{}).get('enabled')),
             'health':sources.get(source,{}).get('health','unknown')} for key,(source,label,_) in TYPES.items()]


def start(raw):
    if set(raw)-{'symbol','tasks','confirm'} or raw.get('confirm') is not True:
        raise HTTPException(422,'请确认这次个股补数的范围')
    symbol = assets.symbol(raw.get('symbol'))
    tasks = raw.get('tasks')
    if not isinstance(tasks,list) or not tasks or len(tasks)>len(TYPES) or any(not isinstance(x,str) or x not in TYPES for x in tasks):
        raise HTTPException(422,'请选择至少一项已支持的个股资料')
    today = store.today()
    result = []
    for key in dict.fromkeys(tasks):
        source, label, days = TYPES[key]
        start_day = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
        try:
            job = collection.submit({'source':source,'start':start_day,'end':today,'symbols':[symbol]})
        except HTTPException as exc:
            result.append({'id':key,'label':label,'state':'blocked','detail':str(exc.detail)})
        else:
            result.append({'id':key,'label':label,'state':job['status'],'job_id':job['id'],'total':job['total']})
    return {'symbol':symbol,'items':result}
