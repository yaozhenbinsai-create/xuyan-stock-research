"""One durable queue for manual and scheduled point-in-time research."""
from __future__ import annotations
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from fastapi import HTTPException
from foundation import assets, collection, database as store, storage_policy
from research import contracts, counterparty, delivery, discovery, evidence, learning, ledger, market, model_transport, multidim, originals, outlook, planning, preferences, preparation, reassessment, recaps, review, rises, rotation, screening, sessions, state_store, stock_library, tools as evidence_tools

log=logging.getLogger(__name__)
TERMINAL={'completed','completed_limited','failed','unknown','stopped','interrupted','data_limited','expired'}

def get_run(run_id, *, include_checkpoint=False):
    with store.db(readonly=True) as c:
        row=c.execute('SELECT * FROM research_runs WHERE id=%s',(run_id,)).fetchone()
        if not row:raise HTTPException(404,'研究任务不存在')
        calls=[{**dict(r),'data':json.loads(r['data'])} for r in c.execute('SELECT * FROM research_calls WHERE run_id=%s ORDER BY started,id LIMIT 10',(run_id,))]
        corrections=delivery.corrections(c,run_id) if row['report'] else []
    data = json.loads(row['data'])
    if not include_checkpoint:
        data.pop('validated_report', None)
        data.get('bundle',{}).pop('scope_pack',None)
    return {k:row[k] for k in ('id','status','stage','created','updated','market_day','kind')}|{'data':data,'report':json.loads(row['report']) if row['report'] else None,'calls':calls,'corrections':corrections}

def list_runs(limit=20,before=None, *, session=None, outcome=None, start=None, end=None):
    params=[];filters=[]
    session_filter="session=%s"
    if session:
        filters.append(session_filter);params.append(session)
    if outcome in ('published','unpublished'):
        filters.append("report IS NOT NULL" if outcome=='published' else "report IS NULL")
    if start:
        filters.append('market_day>=%s');params.append(assets.day(start))
    if end:
        filters.append('market_day<=%s');params.append(assets.day(end))
    if start and end and start>end:
        raise HTTPException(422,'市场日开始日期须早于或等于结束日期')
    count_where = 'WHERE '+' AND '.join(filters) if filters else ''
    count_params = tuple(params)
    if before:
        with store.db(readonly=True) as c:r=c.execute('SELECT created,id FROM research_runs WHERE id=%s',(before,)).fetchone()
        if not r:raise HTTPException(409,'研究档案游标失效，请刷新')
        filters.append('(created,id)<(%s,%s)');params.extend((r['created'],r['id']))
    where = 'WHERE '+' AND '.join(filters) if filters else ''
    with store.db(readonly=True) as c:
        rows=c.execute("SELECT id,status,stage,created,updated,market_day,kind,"
                       "session,intent,title,published_at,withdrawn_at FROM research_runs "
                       +where+' ORDER BY created DESC,id DESC LIMIT %s',(*params,limit+1)).fetchall()
        total=c.execute('SELECT count(*) FROM research_runs '+count_where,count_params).fetchone()[0]
    items=[{**dict(r),'published_at':state_store.iso_time(r['published_at']),
            'withdrawn_at':state_store.iso_time(r['withdrawn_at'])} for r in rows[:limit]]
    return {'items':items,'total':total,'next':rows[limit-1]['id'] if len(rows)>limit else None}

_UNRESOLVED = "(r.status='unknown' OR EXISTS (SELECT 1 FROM research_calls c WHERE c.run_id=r.id AND c.status='unknown'))"

def unresolved_count():
    with store.db(readonly=True) as c:
        return c.execute('SELECT count(*) FROM research_runs r WHERE '+_UNRESOLVED).fetchone()[0]


def reconcile(run_id):
    """Recover a saved response, validate its frozen evidence, and publish once."""
    run=get_run(run_id,include_checkpoint=True)
    if run['report']:return {'state':'published','run_id':run_id}
    if run['status']!='unknown':return {'state':run['status'],'run_id':run_id}
    if run['data'].get('stop_requested'):return {'state':'stopped','run_id':run_id}
    if run['data'].get('validated_report'):
        ledger.publish(run_id,run['data']['validated_report'])
        return {'state':'published','run_id':run_id}
    # The public detail is intentionally limited to the first ten calls. Recovery
    # must use the actual blocked attempt, including attempts beyond that page.
    # Attempts may share a second-resolution timestamp, so use their saved ordinal.
    with store.db(readonly=True) as conn:
        blocked_id=run['data'].get('unknown_call_id')
        if blocked_id:
            row=conn.execute('SELECT id,status,data FROM research_calls WHERE id=%s AND run_id=%s',
                             (blocked_id,run_id)).fetchone()
        else:
            row=conn.execute("SELECT id,status,data FROM research_calls WHERE run_id=%s ORDER BY "
                             "attempt_no DESC,"
                             "started DESC,id DESC LIMIT 1",(run_id,)).fetchone()
    if row:
        call={**dict(row),'data':json.loads(row['data'])}
        if not call['data'].get('recoverable'):
            return {'state':'unqueryable','run_id':run_id,
                    'message':'本轮最后请求没有可查询句柄；保留未知，不使用较早尝试代替'}
        result=model_transport.reconcile_call(call['id'])
        if result['state']=='completed' and result.get('text'):
            try:report=contracts.validate_report(result['text'],run['data']['bundle'])
            except ValueError:
                with store.db() as c:
                    c.execute("UPDATE research_calls SET status='invalid' WHERE id=%s",(call['id'],))
                _write(run_id,'查询取得原结果，但签发契约未通过',status='failed',
                       patch={'error_category':'recovered_invalid'})
                return {'state':'invalid','run_id':run_id}
            if not _write(run_id,'已查询取回原请求，等待签发',patch={'call_id':call['id'],'validated_report':report}):
                return {'state':'stopped','run_id':run_id}
            ledger.publish(run_id,report)
            return {'state':'published','run_id':run_id}
        if result['state']=='failed':
            _write(run_id,'原请求已核实失败，保留回执',status='failed',patch={'error_category':'reconciled_failure'})
        elif result['state'] in ('pending','unknown'):
            _write(run_id,'已查询原请求，尚未取得终态',patch={'reconcile_checked_at':store.now()})
        return {'state':result['state'],'run_id':run_id}
    return {'state':'unqueryable','run_id':run_id,'message':'原通道没有可查询句柄；保留未知记录，不重发旧请求'}

def submit(raw, *, automatic=None):
    if set(raw)-{'idempotency_key','intent','symbols','kind','market_day','cycle_ids','session','acknowledge_unknown','use_preferences',
                 'allow_evidence_round','max_requests','time_budget_seconds','stop_after_reported_tokens','rise_type'}:raise HTTPException(422,'研究请求包含未知字段')
    token=raw.get('idempotency_key')
    if not isinstance(token,str) or not 12<=len(token)<=80:raise HTTPException(422,'缺少有效请求编号')
    key=hashlib.sha256(token.encode()).hexdigest()
    with store.db(readonly=True) as c:
        old=c.execute('SELECT id FROM research_runs WHERE request_key=%s',(key,)).fetchone()
        if old:return get_run(old['id'])
        profile = preferences.current(c) if raw.get('use_preferences', True) is True else {}
        method = learning.active(c)
        shadows = learning.shadow_candidates(c)
    kind=raw.get('kind','discovery')
    if kind not in ('discovery','review'):raise HTTPException(422,'研究类型无效')
    research_plan = planning.plan(raw, profile)
    session, intent, symbols = (research_plan[key] for key in ('session','intent','symbols'))
    originals=[]
    if kind=='review':
        ids=raw.get('cycle_ids') or []
        if not isinstance(ids,list) or not 1<=len(ids)<=8:raise HTTPException(422,'每次复核1至8个周期')
        for cid in list(dict.fromkeys(ids)):
            cy=ledger.cycle_detail(cid)
            if cy['state'] not in reassessment.ACTIVE:
                raise HTTPException(409,'所选股票已退出跟踪，请刷新股票池后复核')
            originals.append({k:cy[k] for k in ('id','symbol','state','formal','data')})
        symbols=list(dict.fromkeys(c['symbol'] for c in originals))
    slot = sessions.choose(session, research_plan['market_day'])
    if not automatic and raw.get('acknowledge_unknown') is not True:
        with store.db(readonly=True) as conn:
            if conn.execute("SELECT 1 FROM research_runs r WHERE r.market_day=%s AND "
                            "r.session=%s AND "+
                            _UNRESOLVED+' LIMIT 1',(slot['market_day'],session)).fetchone():
                raise HTTPException(409,'同一交易日和时段已有终态未知请求，请先核对原请求或明确发起独立研究')
    slot['target']['primary']=research_plan['horizon']
    if automatic and (slot['retrospective'] or not slot['publish_until'] or session not in automatic['enabled']):
        raise HTTPException(409, '该时段无法按时签发自动研究')
    if automatic and kind == 'discovery':
        with store.db(readonly=True) as conn:
            acknowledged = automatic.get('acknowledged_through')
            unseen = ' AND r.updated>=%s' if acknowledged else ''
            params = (slot['market_day'], session, *((acknowledged,) if acknowledged else ()))
            if conn.execute("SELECT 1 FROM research_runs r WHERE r.market_day=%s AND r.kind='discovery' "
                            "AND r.session=%s AND "+
                            _UNRESOLVED+unseen+' LIMIT 1',params).fetchone():
                raise HTTPException(409,'这个交易日的同一时段已有终态未知的请求')
            originals = reassessment.active_cycles(conn, slot['market_data_cutoff'], store.now(), symbols, session=session,
                                                   limit=max(0, min(4, profile.get('candidate_limit', 8) - 1)))
        originals = [{k:v for k,v in item.items() if k!='flags'} for item in originals]
    # A non-trading day is not an intraday replay. The user can select a prior market day explicitly.
    try:plan=model_transport.freeze_plan()
    except model_transport.ModelFailure as exc:raise HTTPException(409,str(exc)) from None
    maximum = raw.get('max_requests')
    duration = raw.get('time_budget_seconds')
    tokens = raw.get('stop_after_reported_tokens')
    if maximum is not None and (type(maximum) is not int or not 1 <= maximum <= 32):
        raise HTTPException(422,'本轮调用上限须在1至32次')
    if duration is not None and (type(duration) is not int or not 30 <= duration <= 3600):
        raise HTTPException(422,'本轮时长上限须在30至3600秒')
    if tokens is not None and (type(tokens) is not int or not 1000 <= tokens <= 10_000_000):
        raise HTTPException(422,'用量停发线须在1千至1千万TOKEN')
    if research_plan['tool_names'] and maximum == 1:
        raise HTTPException(422,'补证需要允许至少两次模型请求')
    stamp=store.now();run_id=uuid.uuid4().hex[:16]
    plan['budget']['max_requests'] = maximum
    plan['budget']['stop_after_reported_tokens'] = tokens
    if not slot['retrospective']:
        plan['publish_until'] = slot['publish_until']
    if duration is not None:
        budget_end = (datetime.fromisoformat(stamp) + timedelta(seconds=duration)).isoformat()
        plan['publish_until'] = min(plan.get('publish_until') or budget_end, budget_end)
    research_plan.update(symbols=symbols, market_day=slot['market_day'], market_data_cutoff=slot['market_data_cutoff'],
                         target=slot['target'] if not research_plan.get('extended_horizon') else None,
                         publish_until=plan.get('publish_until'))
    data={'intent':intent,'symbols':symbols,'session':session,'session_policy':slot,'method_version':method['version'],'method':method,'plan':plan,
      'trigger':'automatic' if automatic else 'manual','preference_revision':profile.get('revision') if profile.get('confirmed_at') else None,
      'intent_constraints':research_plan['constraints'],'research_plan':research_plan,'rise_policy_version':rises.VERSION,
      'dimension_evidence_version':multidim.VERSION,
      'outlook_version':outlook.VERSION if not research_plan.get('extended_horizon') else None,
      'stock_library_version':stock_library.VERSION,
      'shadow_candidates':shadows,
      'original_cycles':originals,'stop_requested':False,'error':None,'request_fingerprint':ledger.digest(raw),
      'limits':{'candidates':8,'max_input_bytes':220000,'max_report_bytes':128000,'max_requests':plan['budget']['max_requests']},
      'memory_applied':[profile['revision']] if profile.get('confirmed_at') else []}
    try:
        with store.db() as c:
            c.execute('INSERT INTO research_runs(id,request_key,status,stage,created,updated,market_day,kind,data) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
              (run_id,key,'queued','等待机会发现',stamp,stamp,slot['market_day'],kind,store.dumps(data)))
            state_store.sync_run(c,run_id)
    except store.IntegrityError:
        with store.db(readonly=True) as c:
            old=c.execute('SELECT id FROM research_runs WHERE request_key=%s',(key,)).fetchone()
            if old:return get_run(old['id'])
        raise HTTPException(409,'已有一轮研究正在执行，请查看进度或停止后再开始') from None
    return get_run(run_id)

def stop(run_id):
    with store.db() as c:
        row=c.execute('SELECT * FROM research_runs WHERE id=%s FOR UPDATE',(run_id,)).fetchone()
        if not row:raise HTTPException(404,'任务不存在')
        if row['status'] in TERMINAL:return get_run(run_id)
        data=json.loads(row['data']);data['stop_requested']=True
        queued=row['status'] in ('queued','waiting')
        c.execute('UPDATE research_runs SET status=%s,stage=%s,updated=%s,data=%s WHERE id=%s',
          ('stopped' if queued else 'running','已停止' if queued else '停止已登记，等待已发请求终态；不会签发',store.now(),store.dumps(data),run_id))
        state_store.sync_run(c,run_id)
    return get_run(run_id)

def _write(run_id,stage,*,patch=None,status=None,screening_rows=None):
    with store.db() as c:
        row=c.execute('SELECT data,status FROM research_runs WHERE id=%s FOR UPDATE',(run_id,)).fetchone()
        if row['status'] in ('completed','completed_limited'):
            return False
        data=json.loads(row['data'])
        if patch:data.update(patch)
        if data.get('stop_requested') and status not in ('stopped','unknown'):
            c.execute("UPDATE research_runs SET status='stopped',stage='已停止，未签发报告',updated=%s WHERE id=%s",(store.now(),run_id))
            return False
        if screening_rows is not None and not data.get('screening_capture'):
            screening.save_rows(c,run_id,screening_rows)
            data['screening_capture']={'basis':'frozen','count':len(screening_rows),'captured_at':store.now()}
        c.execute('UPDATE research_runs SET status=%s,stage=%s,updated=%s,data=%s WHERE id=%s',(status or row['status'],stage,store.now(),store.dumps(data),run_id))
        state_store.sync_run(c,run_id)
        return True

def _claim(run_id):
    with store.db() as c:
        row = c.execute('SELECT status,data FROM research_runs WHERE id=%s FOR UPDATE', (run_id,)).fetchone()
        if not row:
            raise HTTPException(404,'研究任务不存在')
        if row['status'] not in ('queued','waiting'):
            return False
        data = json.loads(row['data'])
        if row['status'] == 'waiting' and (data.get('rotation',{}).get('retry_not_before') or 0) > time.time():
            return False
        c.execute("UPDATE research_runs SET status='running',stage='按冻结截点发现机会与计算证据',updated=%s WHERE id=%s",
                  (store.now(),run_id))
        return True

def process(run_id):
    if not _claim(run_id):return
    run=get_run(run_id,include_checkpoint=True)
    try:
        data = run['data']
        rotation.check(run_id)
        if data.get('validated_report'):
            ledger.publish(run_id, data['validated_report'])
            return
        remaining = sessions.remaining_seconds(data['plan'].get('publish_until'))
        if remaining is not None and remaining <= 0:
            raise rotation.Deadline('已越过原定签发时间，研究未派发后续请求')
        profile = {**preferences.context(data),**data.get('intent_constraints',{})}
        requested=set(data['symbols'])
        scope_profile = {**profile, 'candidate_limit':len(requested), 'boards':[], 'excluded_symbols':[]} if requested else profile
        bundle = data.get('bundle')
        if not bundle:
            review_symbols = tuple(c['symbol'] for c in data['original_cycles'])
            def snapshot(prepared_symbols=()):
                return discovery.snapshot(run['market_day'],data['symbols'],slot=data.get('session_policy'),profile=scope_profile,
                                          method=data.get('method'),review_symbols=review_symbols,prepared_symbols=prepared_symbols,
                                          rise_version=data.get('rise_policy_version'),rise_type=data.get('research_plan',{}).get('rise_type'),
                                          market_context=bool(requested and data.get('dimension_evidence_version')),
                                          outlook_version=data.get('outlook_version'))
            try:
                bundle = snapshot()
            except HTTPException as exc:
                _write(run_id,'截点资料尚未就绪',status='data_limited',patch={'error':exc.detail})
                return
            shortlist = [item['symbol'] for item in bundle['candidates']]
            acceptable = data.get('trigger')!='automatic' or (bundle['market'].get('coverage') or 0)>=data['session_policy']['min_coverage']
            if (shortlist and not requested-set(shortlist) and acceptable and bundle['mode']!='retrospective'
                    and os.environ.get('STOCK_DISABLE_WORKER')!='1'):
                prepared = data.get('evidence_preparation')
                if not data.get('evidence_preparation_started'):
                    if not _write(run_id,'补取候选公告和除权资料',patch={'evidence_preparation_started':store.now()}):
                        return
                    prepared = preparation.acquire(shortlist,run['market_day'],data['session'],data['plan'].get('publish_until'),
                                                   check=lambda:rotation.check(run_id,engine.stop_event.is_set))
                    if not _write(run_id,'冻结候选补证结果',patch={'evidence_preparation':prepared}):
                        return
                if prepared or data.get('evidence_preparation_started'):
                    bundle = snapshot(shortlist)
            if data['original_cycles']:
                targets = []
                with store.db(readonly=True) as conn:
                    for target in data['original_cycles']:
                        row = conn.execute('SELECT * FROM research_cycles WHERE id=%s', (target['id'],)).fetchone()
                        if row:
                            flags = reassessment.flags(conn,row,target['data'],knowledge=bundle['knowledge_cutoff'],
                                                       cutoff=bundle['market_data_cutoff']) if row['state'] in reassessment.ACTIVE else []
                            targets.append({'id':target['id'],'symbol':target['symbol'],'flags':flags})
                    for candidate in bundle['candidates']:
                        reassessment.attach(conn, candidate, [cy for cy in targets if cy['symbol'] == candidate['symbol']])
                bundle['review_targets'] = targets
            if data.get('research_plan',{}).get('extended_horizon'):
                bundle['extended_horizon'] = data['research_plan']['extended_horizon']
        if data.get('stock_library_version'):
            bundle['stock_library_version']=data['stock_library_version']
        screening_rows = bundle.pop('screening_rows',None)
        bundle.pop('rise_preview',None)
        compact=json.loads(store.dumps(bundle))
        for candidate in compact['candidates']:
            candidate.pop('bars',None)
            for document in candidate.get('documents', []):
                document.pop('excerpt',None)
        compact['input_storage']='保存事实编号与研究摘要，公告正文及行情按冻结编号读取'
        if data.get('trigger')=='automatic' and (bundle['market'].get('coverage') or 0) < data['session_policy']['min_coverage']:
            _write(run_id,'覆盖未达到自动研究要求',status='data_limited',screening_rows=screening_rows,
                   patch={'bundle':compact,'error':'保留本轮覆盖与缺口，未派发模型请求'})
            return
        if requested-set(c['symbol'] for c in bundle['candidates']):
            _write(run_id,'指定股票缺少同截点行情或有效历史',status='data_limited',screening_rows=screening_rows,
                   patch={'bundle':compact,'error':'部分指定证券资料不足，未静默缩小研究范围，也未发起模型请求'})
            return
        if not bundle['candidates']:
            _write(run_id,'本次未召回可研究线索',status='data_limited',screening_rows=screening_rows,
                   patch={'bundle':compact,'error':'机会发现无候选，不等于完整AI研究后的正式空选；未发起模型请求'})
            return
        with store.db() as c:c.execute('UPDATE research_runs SET market_day=%s WHERE id=%s',(bundle['market']['market_day'],run_id))
        patch = {'bundle':compact}
        if 'shadow_signals' not in data and bundle['mode'] != 'retrospective':
            patch['shadow_signals'] = learning.observe(data.get('shadow_candidates',[]), bundle)
        if not _write(run_id,'AI综合维度、组合关系与反证',patch=patch,screening_rows=screening_rows):return
        # The public market-data bundle contains no credentials or account holdings.
        model_bundle=json.loads(store.dumps(compact))
        model_bundle.pop('comparison_samples',None)
        model_bundle.pop('scope_pack',None)
        with store.db(readonly=True) as conn:
            for candidate in model_bundle['candidates']:
                ids = candidate.get('bar_ids')
                candidate['bars']=market.frozen_bars(candidate['symbol'],ids[-5:] if ids else candidate['evidence_ids'],5,conn)
                if len(candidate['bars'])<5 or ids and [bar['id'] for bar in candidate['bars']] != ids[-5:]:
                    raise HTTPException(409, '本轮冻结的行情已不可读，请重新核对证据')
                evidence.restore_excerpts(conn,candidate.get('documents',[]))
                candidate['originals']=originals.attach_quotes(conn,candidate.get('originals',{}))
                if any(anchor['quote'] is None for anchor in candidate['originals'].get('anchors',[])):
                    raise HTTPException(409,'本轮冻结的原文段落已不可读取，请重新核对证据')
                if data.get('dimension_evidence_version') == multidim.VERSION:
                    analysis = multidim.build(model_bundle['market'],candidate,pointed=bool(requested))
                    candidate['joint_evidence'] = [
                        {key:row[key] for key in ('dimensions','state','summary','facts')}
                        for row in analysis['combinations'] if row['state']!='missing'
                    ]
                if data.get('stock_library_version') == stock_library.VERSION:
                    candidate['counterparty_reference']=counterparty.reference(candidate)
        model_bundle['read_scope']='读取日线与截点快照摘要、最后5根柱及已列公告与财务节选；未经读取的原文须标明'
        model_input={'intent':data['intent'],'kind':run['kind'],'bundle':model_bundle,
                     'research_plan':data.get('research_plan',{}),'research_roles':data['plan'].get('research_roles',[])}
        flags_by_cycle = {t['id']:t['flags'] for t in model_bundle.pop('review_targets',[])}
        covered_symbols = {c['symbol'] for c in model_bundle['candidates']}
        model_input['original_cycles']=[{**cy,'flags':flags_by_cycle.get(cy['id'],[])}
                                       for cy in data['original_cycles'] if cy['symbol'] in covered_symbols]
        if profile:
            model_input['research_preferences']={k:profile[k] for k in ('note','horizon','risk','boards','excluded_symbols','candidate_limit','max_industry_recommendations') if k in profile}
        if data.get('intent_constraints'):
            model_input['current_intent_constraints']=data['intent_constraints']
        stopping=lambda:os.environ.get('STOCK_DISABLE_WORKER')!='1' and engine.stop_event.is_set()
        system_prompt = contracts.SYSTEM_PROMPT
        if data.get('outlook_version') == outlook.VERSION:
            system_prompt += contracts.OUTLOOK_PROMPT
        if data.get('research_plan',{}).get('target'):
            system_prompt += contracts.SESSION_TARGET_PROMPT
        if data.get('stock_library_version') == stock_library.VERSION:
            system_prompt += contracts.LIBRARY_PROMPT
        allowed_tools = data.get('research_plan',{}).get('tool_names',[])
        questions = data.get('tool_requests')
        validate = lambda text: contracts.validate_report(text,bundle)
        if allowed_tools and not questions:
            def validate_first(text):
                requests = evidence_tools.requested(text,bundle,allowed_tools)
                return {'tool_requests':requests} if requests else validate(text)
            report,call_id=rotation.execute(run_id,data['plan'],system_prompt+'\n'+evidence_tools.SYSTEM_ADDENDUM,
                                            model_input,validate_first,stopping=stopping)
            questions=report.get('tool_requests')
            if questions and not _write(run_id,'已登记AI补证问题',patch={'tool_requests':questions,'tool_request_call':call_id}):
                return
        elif not questions:
            report,call_id=rotation.execute(run_id,data['plan'],system_prompt,model_input,validate,stopping=stopping)
        if questions:
            rotation.check(run_id,stopping)
            with store.db(readonly=True) as conn:
                results=evidence_tools.collect(conn,questions,bundle)
            model_input['evidence_round']=results
            receipt=[{**question,'state':result['state'],'result_hash':ledger.digest(result['evidence'])}
                     for question,result in zip(questions,results)]
            if not _write(run_id,'补证已完成，等待最终报告',patch={'tool_receipt':receipt}):
                return
            report,call_id=rotation.execute(run_id,data['plan'],system_prompt,model_input,validate,stopping=stopping)
        if not _write(run_id,'校验证据、登记报告与跟踪',patch={'call_id':call_id,'validated_report':report}):return
        ledger.publish(run_id,report)
    except rotation.RetryLater:return
    except rotation.Deadline as exc:
        _write(run_id,'已错过原签发窗口',status='expired',patch={'error':str(exc)})
    except rotation.Stopped as exc:
        _write(run_id,'服务停止，保留当前检查点' if exc.shutdown else '已停止研究',status='interrupted' if exc.shutdown else 'stopped')
    except model_transport.ModelFailure as exc:
        _write(run_id,'模型未完成，未签发报告',status='unknown' if exc.unknown else 'failed',patch={
            'error':str(exc),'error_category':exc.category,
            **({'unknown_call_id':exc.call_id} if exc.unknown and getattr(exc,'call_id',None) else {})})
    except (ValueError,HTTPException) as exc:
        _write(run_id,'资料或签发契约未通过',status='failed',patch={'error':getattr(exc,'detail',str(exc))})
    except Exception:
        log.exception('research task failed: %s',run_id)
        _write(run_id,'内部处理未完成，未签发报告',status='failed',patch={'error':'内部处理未完成；真实失败已保留，请查看诊断，不自动重发模型请求'})

def recover_interrupted():
    with store.db() as c:
        stamp=store.now()
        delivery.recover(c)
        c.execute("UPDATE research_calls SET status='unknown',finished=%s WHERE status='sent'",(stamp,))
        c.execute("UPDATE research_runs r SET status='unknown',stage='服务重启中断，远端终态未知',updated=%s WHERE status='running' AND "+_UNRESOLVED,(stamp,))
        c.execute("UPDATE research_runs SET status='queued',stage='恢复已验证报告的签发检查点',updated=%s "
                  "WHERE status='running' AND has_validated_report=1 AND stop_requested=0",(stamp,))
        c.execute("UPDATE research_runs r SET status='queued',stage='恢复未派发的本地研究',updated=%s "
                  "WHERE status='running' AND NOT EXISTS (SELECT 1 FROM research_calls c WHERE c.run_id=r.id) "
                  "AND stop_requested=0",(stamp,))
        c.execute("UPDATE research_runs SET status='interrupted',stage='服务重启中断；不会自动重试',updated=%s WHERE status='running'",(stamp,))


def compact_unpublished():
    before=(datetime.fromisoformat(store.now())-timedelta(days=storage_policy.POLICY['failed_research_detail_days'])).isoformat()
    with store.db() as conn:
        rows=conn.execute("SELECT id,data FROM research_runs WHERE report IS NULL AND active_slot IS NULL AND updated<%s "
                          "AND has_bundle=1 AND detail_compacted_at IS NULL ORDER BY created,id LIMIT 20 FOR UPDATE",
                          (before,)).fetchall()
        for row in rows:
            data=json.loads(row['data'])
            data.pop('validated_report',None)
            bundle=data['bundle']
            data['bundle']={k:v for k,v in bundle.items() if k in ('market','input_hash','mode','method_version',
                            'knowledge_cutoff','market_data_cutoff','session_policy')}
            data['bundle']['candidates']=[{**{k:c[k] for k in ('symbol','name','paths','industry','metrics','dimensions')},
                                            'evidence_ids':c['evidence_ids'],'bar_ids':c.get('bar_ids',[])}
                                          for c in bundle.get('candidates',[])]
            data['detail_compacted_at']=store.now()
            if data.pop('screening_capture',None):
                conn.execute('DELETE FROM research_screening WHERE run_id=%s',(row['id'],))
            conn.execute('UPDATE research_runs SET data=%s WHERE id=%s',(store.dumps(data),row['id']))
            state_store.sync_run(conn,row['id'])


def schedule_tick():
    try:preparation.warm_recent()
    except Exception as exc:
        log.warning('候选预采集尚未完成：%s',type(exc).__name__)
    # Query only a saved original handle. This never posts another generation.
    now_for_recovery=store.now()
    last_recovery=store.setting('research_reconcile_checked_at','')
    if not last_recovery or (datetime.fromisoformat(now_for_recovery)-datetime.fromisoformat(last_recovery)).total_seconds()>=60:
        store.set_setting('research_reconcile_checked_at',now_for_recovery)
        with store.db(readonly=True) as conn:
            recovery=conn.execute("SELECT r.id FROM research_runs r JOIN research_calls c ON c.run_id=r.id "
                                  "WHERE r.status='unknown' AND (c.status IN ('unknown','completed') OR r.has_validated_report=1) AND c.recoverable=1 "
                                  "AND r.market_day>=%s ORDER BY r.updated LIMIT 1",
                                  ((datetime.fromisoformat(now_for_recovery)-timedelta(days=2)).date().isoformat(),)).fetchone()
        if recovery:
            try:reconcile(recovery[0])
            except (model_transport.ModelFailure,ValueError,HTTPException):
                log.info('原请求暂未恢复，保留原始回执')
    now = store.now()
    if symbols := review.afternoon_symbols(now):
        day=now[:10]
        try:
            collection.submit({'source':'tx_quotes','start':day,'end':day,'symbols':symbols},
                              trigger='afternoon_review',expires_at=sessions.local_time(day,'13:05'))
        except HTTPException as exc:
            log.info('afternoon evaluation quote unavailable: %s',exc.detail)
    for day, session, symbols in sessions.captures(now):
        try:
            cutoff=sessions.local_time(day,sessions.SESSIONS[session]['cutoff'])
            job = collection.submit({'source':'tx_quotes','start':day,'end':day,'symbols':symbols},
                                    trigger='research_snapshot',expires_at=cutoff)
        except HTTPException as exc:
            sessions.remember_slot(day, session, 'capture_unavailable', str(exc.detail))
        else:
            sessions.remember_slot(day, session, 'capturing', '已排入截点快照采集', job['id'])
    for slot, policy in sessions.due(now):
        day, session = slot['market_day'], slot['session']
        if now >= slot['launch_until']:
            sessions.remember_slot(day, session, 'missed', '已错过原启动窗口，留待回看')
            continue
        with store.db(readonly=True) as conn:
            busy = conn.execute('SELECT 1 FROM research_runs WHERE active_slot=1 LIMIT 1').fetchone()
            if busy:
                continue
            if session == 'close':
                ready = collection.market_daily_received(conn, day)
            else:
                ready = market.latest_quotes(conn, day, slot['market_data_cutoff'], now, slot['quote_age_seconds'], policy['symbols'])
            if not ready:
                continue
        try:
            run = submit({'idempotency_key':f'auto/{day}/{session}', 'session':session,'market_day':day,
                          'symbols':policy['symbols']}, automatic=policy)
        except HTTPException as exc:
            if exc.status_code == 409 and '已有一轮' in str(exc.detail):
                continue
            sessions.remember_slot(day, session, 'blocked', str(exc.detail))
        else:
            sessions.remember_slot(day, session, 'submitted', '已登记独立时段研究', run['id'])

class Engine:
    def __init__(self):self.stop_event=threading.Event();self.thread=None;self.clock=None
    def start(self):
        if self.thread and self.thread.is_alive():return
        recover_interrupted()
        self.stop_event.clear();self.thread=threading.Thread(target=self.loop,daemon=True,name='new-research-engine');self.thread.start()
        self.clock=threading.Thread(target=self.clock_loop,daemon=True,name='new-research-clock');self.clock.start()
    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=2)
        if self.clock:self.clock.join(timeout=2)
    def clock_loop(self):
        last_pool_check = 0
        while not self.stop_event.is_set():
            try:
                schedule_tick()
            except Exception:
                log.exception('research clock failed')
            if time.monotonic()-last_pool_check>=60:
                try:
                    ledger.maintain_pools()
                except Exception:
                    log.exception('daily pool review failed')
                try:
                    recaps.schedule_tick()
                except Exception:
                    log.exception('scheduled recap failed')
                last_pool_check = time.monotonic()
            self.stop_event.wait(15)
    def loop(self):
        last_outcome=0
        while not self.stop_event.is_set():
            try:
                with store.db(readonly=True) as c:row=c.execute("SELECT id FROM research_runs WHERE status='queued' OR (status='waiting' AND rotation_retry_not_before<=%s) ORDER BY created,id LIMIT 1",(time.time(),)).fetchone()
                if row:process(row['id'])
                if time.monotonic()-last_outcome>300:
                    # Advance the clock first so one failing step cannot retry every 2 seconds.
                    last_outcome=time.monotonic()
                    for step in (ledger.refresh_outcomes,learning.auto_publish,compact_unpublished):
                        try:step()
                        except Exception:log.exception('periodic maintenance step failed: %s',step.__name__)
            except Exception:log.exception('new research worker cycle failed')
            self.stop_event.wait(2)

engine=Engine()
