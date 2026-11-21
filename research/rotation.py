"""Saved-order continuous rotation with durable cooldowns and unknown-outcome barriers."""
from __future__ import annotations
import json,time
from foundation import database as store
from research import model_transport, preferences, sessions, usage, state_store

SIGNING_RESERVE_SECONDS = 15
STAGE_MAX_LENGTH = 40

class RetryLater(Exception):pass
class Deadline(Exception):pass
class Stopped(Exception):
    def __init__(self,shutdown=False):self.shutdown=shutdown

def check(run_id,stopping=lambda:False):
    if stopping():raise Stopped(shutdown=True)
    with store.db(readonly=True) as c:
        r=c.execute('SELECT status,data FROM research_runs WHERE id=%s',(run_id,)).fetchone()
        if not r or r['status']=='stopped':raise Stopped()
        data = json.loads(r['data'])
        if data.get('stop_requested'):raise Stopped()
        if data.get('trigger')=='automatic' and data['session'] not in sessions.config(c)['enabled']:
            raise Stopped()
        preferences.context(data, c)

def save(run_id,state,stage,*,status='running'):
    # research_runs.stage is a short display label (VARCHAR(40)). Full model
    # names and attempt metadata remain unchanged in the durable rotation state.
    if len(stage)>STAGE_MAX_LENGTH:stage=stage[:STAGE_MAX_LENGTH-1]+'…'
    with store.db() as c:
        row=c.execute('SELECT data FROM research_runs WHERE id=%s FOR UPDATE',(run_id,)).fetchone()
        value=json.loads(row[0])
        if value.get('stop_requested') and status!='unknown':raise Stopped()
        value['rotation']=state
        c.execute('UPDATE research_runs SET data=%s,status=%s,stage=%s,updated=%s WHERE id=%s',(store.dumps(value),status,stage,store.now(),run_id))
        state_store.sync_run(c,run_id)

def pause(run_id,seconds,stopping):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        check(run_id,stopping);time.sleep(max(0,min(.5,end-time.monotonic())))

def execute(run_id,plan,system,payload,validate,*,stopping=lambda:False,wait=pause):
    with store.db(readonly=True) as c:row=c.execute('SELECT data FROM research_runs WHERE id=%s',(run_id,)).fetchone()
    state=json.loads(row[0]).get('rotation') or {'cycle':1,'position':0,'attempt':0,'excluded':[],'retry_not_before':None}
    chain=plan.get('chain') or [{'model':plan['model'],'config_hash':plan['config_hash']}]
    plan={**plan,'chain':chain};first=state['cycle'];visits=plan.get('rotation_policy',{}).get('cycles_per_visit',3)
    deadline = plan.get('publish_until')
    maximum = plan.get('budget', {}).get('max_requests')
    max_tokens = plan.get('budget', {}).get('stop_after_reported_tokens')
    while state['cycle']<first+visits:
        for pos in range(state['position'],len(chain)):
            check(run_id,stopping)
            selected=model_transport.select_model(plan,pos);model=selected['model']
            if model['id'] in state['excluded']:continue
            with store.db(readonly=True) as c:
                enabled = {row['id'] for row in c.execute('SELECT id,data FROM models') if json.loads(row['data']).get('enabled')}
            if model['id'] not in enabled:continue
            if maximum is not None and state['attempt']>=maximum:
                raise model_transport.ModelFailure('session_budget','已达到本轮调用次数上限')
            if max_tokens is not None and state['attempt']:
                totals = usage.summary(run_id)
                if not totals['actual_total_known'] or (totals['reported']['total'] or 0)>=max_tokens:
                    raise model_transport.ModelFailure('token_budget','已到用量停发线，或已有请求用量未知，未派发后续调用')
            remaining = sessions.remaining_seconds(deadline)
            if remaining is not None:
                available = remaining - SIGNING_RESERVE_SECONDS
                configured = selected['budget']['timeout_seconds']
                # A local timeout leaves the remote request unknown and forbids
                # fallback. Reserving hypothetical backup slots would truncate
                # this request for time that the queue then cannot legally use.
                # Known failures still rotate using their actual remaining time.
                timeout = min(configured, available)
                if timeout < 1:
                    raise Deadline('剩余时间不足以调用并完成签发，已结束本轮')
                selected['budget'].update(configured_timeout_seconds=configured, timeout_seconds=timeout)
            state.update(position=pos,attempt=state['attempt']+1,current_model_id=model['id'],current_model_name=model['name'],retry_not_before=None)
            selected['attempt_context']={'cycle':state['cycle'],'position':pos+1,'attempt':state['attempt']}
            save(run_id,state,f"第{state['cycle']}轮 · {model['name']} · 第{state['attempt']}次尝试")
            try:
                text,call_id=model_transport.complete(run_id,selected,system,payload)
                check(run_id,stopping)
                try:report=validate(text)
                except ValueError as exc:
                    with store.db() as c:
                        row=c.execute('SELECT data FROM research_calls WHERE id=%s FOR UPDATE',(call_id,)).fetchone()
                        if row:
                            value=json.loads(row[0]);value['validation_error']=str(exc)[:2000]
                            c.execute("UPDATE research_calls SET status='invalid',data=%s WHERE id=%s",(store.dumps(value),call_id))
                            state_store.sync_call(c,call_id)
                    raise model_transport.ModelFailure('invalid_output','已返回内容未通过签发契约，继续其他通道') from None
                state.update(position=pos,successful_call_id=call_id)
                save(run_id,state,'模型完成，证据契约通过')
                return report,call_id
            except model_transport.ModelFailure as exc:
                state.update(last_error=str(exc),last_category=exc.category,position=pos+1)
                if exc.unknown:
                    state['blocked_unknown']=True
                    save(run_id,state,'远端终态未知，停止后续通道派发',status='unknown')
                    raise
                check(run_id,stopping)
                if not exc.retryable:state['excluded'].append(model['id'])
                save(run_id,state,f"{model['name']}未完成，按保存顺序切换")
        check(run_id,stopping)
        with store.db(readonly=True) as c:enabled={r['id'] for r in c.execute('SELECT id,data FROM models') if json.loads(r['data']).get('enabled')}
        if not any(m['model']['id'] in enabled and m['model']['id'] not in state['excluded'] for m in chain):
            raise model_transport.ModelFailure('no_eligible_models','保存方案中的通道均已停用或存在需处理的配置／内容错误；已有尝试与用量保留')
        state.update(cycle=state['cycle']+1,position=0)
        if state['cycle']>=first+visits:
            delay=min(300,30*((state['cycle']-1+2)//3));state['retry_not_before']=time.time()+delay
            remaining = sessions.remaining_seconds(deadline)
            if remaining is not None and remaining <= delay + SIGNING_RESERVE_SECONDS:
                raise Deadline('下一轮等待时间超过原签发窗口')
            save(run_id,state,f"已完成{state['cycle']-1}轮，{delay}秒后按原顺序继续",status='waiting')
            raise RetryLater()
        save(run_id,state,'本轮通道均暂未完成，等待下一轮')
        wait(run_id,min(60,5*(state['cycle']-1)),stopping)
