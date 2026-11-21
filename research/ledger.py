"""Immutable original decisions, append-only reviews, and separate outcome denominators."""
from __future__ import annotations
from datetime import datetime
import hashlib
import json
import logging
import uuid
from fastapi import HTTPException
from foundation import assets, database as store, record_store
from research import delivery, evidence, market, methods, pool_market, preferences, reassessment, review, sessions, stock_library, state_store

log = logging.getLogger(__name__)

def digest(v):return hashlib.sha256(store.dumps(v).encode()).hexdigest()

def endpoints(day,conn,extended=None):
    rows=conn.execute("SELECT h.day,c.trading FROM heads h JOIN calendar_records c ON c.fact_id=h.fact_id WHERE h.dataset='calendar' AND h.day>%s ORDER BY h.day LIMIT 60",(day,)).fetchall()
    days=[r[0] for r in rows if r[1]]
    if extended in ('D5','D10','D20'):
        length=int(extended[1:])
        return {extended:days[length-1] if len(days)>=length else None}
    return {'D1':days[0] if len(days)>0 else None,'D3':days[2] if len(days)>2 else None}

def event(c,cycle_id,run_id,kind,data,event_identity):
    written=c.execute('INSERT IGNORE INTO research_events(event_key,cycle_id,run_id,created,kind,data) VALUES(%s,%s,%s,%s,%s,%s)',
      (digest(event_identity),cycle_id,run_id,store.now(),kind,store.dumps(data)))
    if written.rowcount:
        state_store.sync_event(c,written.lastrowid,data)


def _outcome(conn, cycle_id, horizon, kind, result, now):
    payload = store.dumps(result)
    current = conn.execute('SELECT status,data FROM research_outcomes WHERE cycle_id=%s AND horizon=%s AND kind=%s',
                           (cycle_id,horizon,kind)).fetchone()
    if current and current['status']==result['status'] and current['data']==payload:
        return
    if current and current['status']=='complete':
        prior = json.loads(current['data'])
        event(conn,cycle_id,None,'outcome_revision',{'horizon':horizon,'kind':kind,'before':prior,'after':result},
              ['outcome_revision',cycle_id,horizon,kind,digest(prior),digest(result)])
    conn.execute('INSERT INTO research_outcomes(cycle_id,horizon,kind,status,updated,data) VALUES(%s,%s,%s,%s,%s,%s) '
                 'ON DUPLICATE KEY UPDATE status=VALUES(status),updated=VALUES(updated),data=VALUES(data)',
                 (cycle_id,horizon,kind,result['status'],now,payload))
    state_store.sync_outcome(conn,cycle_id,horizon,kind,result)


def _removal_contract(data, formal, endpoint, horizon, bar, now):
    removed = data.get('first_removed_at') or data.get('last_review',{}).get('at')
    if formal and removed and endpoint and removed>=endpoint+'T15:00:00+08:00':
        return {'status':'after_endpoint','endpoint':endpoint,'reason':'剔除晚于原端点，无可评持有窗口'}
    result = evaluate_contract(data,formal,endpoint,horizon,bar,now)
    if result['status']=='untriggered':
        return {'status':'missing_reference','endpoint':endpoint,'reason':'剔除后尚未取得合格参考价'}
    return result


def _write_pool_change(conn, cycle, data, stamp, state, check, kind):
    if state=='removed':
        data['first_removed_at'] = data.get('first_removed_at') or stamp
    data['pool_review'] = check
    conn.execute('UPDATE research_cycles SET state=%s,updated=%s,data=%s WHERE id=%s',
                 (state,stamp,store.dumps(data),cycle['id']))
    state_store.sync_cycle(conn,cycle['id'],data)
    if state==cycle['state']:
        return
    event(conn,cycle['id'],None,kind,{**check,'from':cycle['state'],'to':state},
          [kind,cycle['id'],check['day'],check.get('fact_id'),state])
    if check.get('fact_id'):
        review.pin(conn,'pool_review',cycle['id'],[check['fact_id']])
    if state=='removed':
        for horizon,endpoint in data['endpoints'].items():
            result = _removal_contract({**data,'entry_reference':None},bool(cycle['formal']),endpoint,horizon,None,stamp)
            _outcome(conn,cycle['id'],horizon,'removal',result,stamp)


def remove_from_pools(symbol, raw):
    if set(raw)-{'reason','confirm','session'} or raw.get('confirm') is not True:
        raise HTTPException(422,'请确认移出这只股票')
    reason, session = raw.get('reason'),raw.get('session')
    if session is not None and (not isinstance(session,str) or session not in sessions.SESSIONS):
        raise HTTPException(422,'移除范围不符合研究时段')
    if not isinstance(reason,str) or not 1<=len(reason.strip())<=500:
        raise HTTPException(422,'请填写 1 至 500 字的移除理由')
    symbol, stamp = assets.symbol(symbol), store.now()
    with store.db() as conn:
        scope = ' AND '+stock_library.CYCLE_SESSION_SQL+'=%s' if session else ''
        rows = conn.execute('SELECT c.* FROM research_cycles c WHERE symbol=%s AND state IN '+stock_library.ACTIVE_SQL+scope+
                            ' ORDER BY id FOR UPDATE',(symbol,*((session,) if session else ()))).fetchall()
        for row in state_store.overlay_cycles(conn,rows):
            _write_pool_change(conn,row,row['data'],stamp,'removed',
                               {'day':stamp[:10],'outcome':'removed','reason':reason.strip()},'pool_remove')
    return {'symbol':symbol,'removed':len(rows)}


def _daily_pool_check(conn, cycle, day, stamp, data=None):
    data = state_store.load_cycle_data(conn,cycle) if data is None else data
    bar = market.closing_bar(conn,cycle['symbol'],day)
    if not bar or bar.get('suspended') or not (bar.get('close') or 0)>0:
        return
    floor = max((data.get('structure_guard') or {}).get('below') or 0,
                (data.get('rise') or {}).get('invalidation_price') or 0)
    check = {'day':day,'fact_id':bar['fact_id'],'close':bar['close'],'outcome':'kept'}
    if floor:
        check['floor'] = floor
    state = cycle['state']
    comparable = bool(floor and market.comparable_window(conn,cycle['symbol'],{'at':cycle['market_day']},day))
    if comparable and bar['close']<floor:
        state = 'removed'
        check.update(outcome='removed',reason=f"收盘 {bar['close']:g} 元低于原失效位 {floor:g} 元。")
    elif not comparable:
        state = 'needs_evidence'
        reason = '原判缺收盘失效位，转观察待补证。' if not floor else '原判与最新收盘价口径待核，转观察。'
        check.update(outcome='needs_evidence',reason=reason)
    else:
        alerts = reassessment.flags(conn,cycle,data,cutoff=sessions.local_time(day,'15:00'),knowledge=stamp)
        alert = next((item for item in alerts if item['kind'] in ('risk_notice','fact_revised','price_invalidation')),None)
        if alert:
            state = 'needs_evidence'
            check.update(outcome='needs_evidence',reason=alert['detail'],alert_fact=alert['fact_id'])
        elif state=='needs_evidence':
            check.update(outcome='needs_evidence',reason='已核对收盘价，原待补证事项仍需复核。')
        else:
            check['reason'] = f"收盘 {bar['close']:g} 元，守住原失效位 {floor:g} 元，维持原池。"
    _write_pool_change(conn,cycle,data,stamp,state,check,'daily_pool_review')


def maintain_pools(session=None):
    stamp = store.now()
    with store.db() as conn:
        day = pool_market.latest_close_day(conn,stamp)
        if not day:
            return
        cutoff = sessions.local_time(day,'15:00')
        scope = ' AND '+stock_library.CYCLE_SESSION_SQL+'=%s ' if session else ''
        rows = conn.execute(
            'SELECT c.* FROM research_cycles c WHERE c.state IN '+stock_library.ACTIVE_SQL+
            ' AND c.created<%s AND c.market_day<=%s AND EXISTS ('
            "SELECT 1 FROM heads h JOIN facts f ON f.id=h.fact_id JOIN daily_bars b ON b.fact_id=f.id WHERE h.dataset='daily' AND h.day=%s "
            "AND h.symbol=c.symbol AND COALESCE(b.suspended,0)=0 "
            "AND (c.pool_review_fact_id IS NULL OR c.pool_review_fact_id<>f.id)) "+scope+
            'ORDER BY c.id LIMIT 48 FOR UPDATE SKIP LOCKED',(cutoff,day,day,*((session,) if session else ())),
        ).fetchall()
        for row in state_store.overlay_cycles(conn,rows):
            _daily_pool_check(conn,row,day,stamp,row['data'])


def pool_review_status(session=None):
    stamp = store.now()
    with store.db(readonly=True,isolation='REPEATABLE READ') as conn:
        day = pool_market.latest_close_day(conn,stamp)
        counts = stock_library.pool_counts(conn,session)
        active_run = conn.execute('SELECT id,status,stage FROM research_runs WHERE active_slot=1 LIMIT 1').fetchone()
        result = {'day':day,'today':stamp[:10],'pools':counts,'checked':0,'pending':0,'removed':0,'needs_evidence':0,
                  'active':{**dict(active_run),'data':{}} if active_run else None}
        if day:
            cutoff = sessions.local_time(day,'15:00')
            checked = "c.pool_review_day=%s"
            state = "c.pool_review_outcome"
            active = 'c.state IN '+stock_library.ACTIVE_SQL
            scope = ' WHERE '+stock_library.CYCLE_SESSION_SQL+'=%s' if session else ''
            values = conn.execute('SELECT COUNT(DISTINCT CASE WHEN '+checked+' THEN c.symbol END) checked,'
                'COUNT(DISTINCT CASE WHEN '+active+' AND c.created<%s AND NOT COALESCE('+checked+',0) THEN c.symbol END) pending,'
                'COUNT(DISTINCT CASE WHEN '+checked+' AND '+state+"='removed' AND "+stock_library.retired_sql('c.symbol')+
                ' THEN c.symbol END) removed,COUNT(DISTINCT CASE WHEN '+checked+' AND '+state+"='needs_evidence' AND "+active+
                ' THEN c.symbol END) needs_evidence FROM research_cycles c'+scope,
                (day,cutoff,day,day,day,*((session,) if session else ()))).fetchone()
            result.update(dict(values))
    return result


def _thesis_key(conn, decision):
    hypothesis = decision.get('hypothesis') or {}
    driver = hypothesis.get('driver', 'structure')
    identifier = hypothesis.get('anchor_id')
    if driver != 'structure' and identifier:
        row = conn.execute('SELECT dataset,day,symbol,record_key FROM facts WHERE id=%s',(identifier,)).fetchone()
        if row and row['dataset'] in ('announcements','financials','policies','actions','policy_terms'):
            return digest([driver,row['dataset'],row['day'],row['symbol'],row['record_key']])
    return driver


def _record_reviews(conn, run_id, stamp, decision, targets, evidence_cutoff):
    requested = decision.get('review_items') or [{'cycle_id':t['id'],'state':decision['review_state'],
                 'reason':decision['thesis'],'fact_ids':reassessment.cited_ids(decision)} for t in targets]
    by_id = {t['id']:t for t in targets}
    effects = []
    for item in sorted(requested,key=lambda item:item['cycle_id']):
        if item['cycle_id'] not in by_id:
            raise ValueError('复核周期超出冻结范围')
        cy = conn.execute('SELECT * FROM research_cycles WHERE id=%s FOR UPDATE',(item['cycle_id'],)).fetchone()
        if not cy:
            raise ValueError('冻结的原周期已无法读取')
        original = state_store.load_cycle_data(conn,cy)
        retain = ('active' if original.get('entry_reference') else 'waiting') if cy['formal'] else 'observe'
        state = {'retain':retain,'observe':'observe','remove':'removed','needs_evidence':'needs_evidence'}[item['state']]
        if cy['state'] in ('removed','expired','rejected'):
            state = cy['state']
        if state == 'removed' and cy['state'] != 'removed':
            original['first_removed_at'] = stamp
        elif state == 'removed' and not original.get('first_removed_at'):
            first = conn.execute("SELECT created FROM research_events WHERE cycle_id=%s AND kind='review' AND to_state='removed' ORDER BY id LIMIT 1",(cy['id'],)).fetchone()
            original['first_removed_at'] = first[0] if first else original.get('last_review',{}).get('at')
        flags = by_id[cy['id']].get('flags', [])
        accepted = set(item['fact_ids'])
        revised = {f['prior_fact_id']: f['fact_id'] for f in flags if f['kind'] == 'fact_revised' and f['fact_id'] in accepted}
        prior_ids = reassessment._dependencies(conn, cy, original)
        original['dependency_ids'] = sorted({revised.get(fid,fid) for fid in prior_ids} | accepted)[-256:]
        original['last_review'] = {'run_id':run_id,'at':stamp,'evidence_through':evidence_cutoff,'state':state,
                                   'reason':item['reason'],'flag_facts':[f['fact_id'] for f in flags if f['fact_id'] in accepted]}
        conn.execute('UPDATE research_cycles SET state=%s,updated=%s,data=%s WHERE id=%s',
                     (state,stamp,store.dumps(original),cy['id']))
        state_store.sync_cycle(conn,cy['id'],original)
        event(conn,cy['id'],run_id,'review',{'from':cy['state'],'to':state,'reason':item['reason'],
              'checks':decision['next_checks'],'fact_ids':item['fact_ids'],'flags':flags},['review',run_id,cy['id']])
        if state=='removed' and cy['state']!='removed':
            for horizon,endpoint in original['endpoints'].items():
                result = _removal_contract({**original,'entry_reference':None},bool(cy['formal']),endpoint,horizon,None,stamp)
                _outcome(conn,cy['id'],horizon,'removal',result,stamp)
        effects.append({'cycle_id':cy['id'],'state':state,'reason':item['reason'],'fact_ids':item['fact_ids'],
                        'entry':original['entry'],'endpoints':original['endpoints'],'invalidation':original['invalidation'],
                        'rise_name':(original.get('rise') or {}).get('name')})
    return effects


def publish(run_id,report):
    stamp=store.now()
    with store.db() as c:
        row=c.execute('SELECT * FROM research_runs WHERE id=%s FOR UPDATE',(run_id,)).fetchone()
        if not row:raise ValueError('研究不存在')
        if row['report']:return json.loads(row['report'])
        data=json.loads(row['data'])
        if data.get('stop_requested'):raise ValueError('停止已生效，未签发报告')
        profile = preferences.context(data, c)
        bundle=data['bundle']
        extended=data.get('research_plan',{}).get('extended_horizon') if row['kind']=='discovery' else None
        policy = bundle.get('session_policy') or data.get('session_policy', {})
        target = data.get('research_plan',{}).get('target')
        ends = target['horizons'] if target and not extended else endpoints(row['market_day'],c,extended)
        deadline = policy.get('publish_until')
        late = bool(deadline and stamp >= deadline and bundle['mode'] != 'retrospective')
        method_version = bundle.get('method_version') or data.get('method_version') or methods.METHOD_VERSION
        model_id = data.get('rotation', {}).get('current_model_id')
        model = next((entry['model'] for entry in data['plan'].get('chain', []) if entry['model']['id'] == model_id), data['plan']['model'])
        report.update(id=run_id,published_at=stamp,market_day=row['market_day'],knowledge_cutoff=bundle['knowledge_cutoff'],
          market_data_cutoff=bundle['market_data_cutoff'],method_version=method_version,mode='late_review' if late else bundle['mode'],
          kind=row['kind'],horizons=ends,model=model['name'],session=data.get('session','close'),policy_version=policy.get('version'),
          action_valid_until=None if extended else policy.get('action_valid_until'),publish_until=deadline,study_horizon=extended)
        if target:
            report['forecast_target'] = target
        if bundle.get('rise_policy'):
            report['rise_policy_version'] = bundle['rise_policy']['version']
        originals={}
        for review_target in bundle.get('review_targets',data.get('original_cycles',[])):
            originals.setdefault(review_target['symbol'],[]).append(review_target)
        evidence_by_symbol={row['symbol']:row for row in bundle['candidates']}
        affected_cycles = []
        sector_counts = {}
        for decision in report['decisions']:
            if late:
                decision['formal_blockers'].append('已超过原定签发时间，保留迟发回看')
                if decision['action'] in ('select','conditional'):
                    decision['action']='watch'
                continue
            symbol=decision['symbol'];formal=decision['action'] in ('select','conditional')
            if formal:
                checks=evidence.read_checks(c,symbol,stamp)
                if any(checks.get(k,{}).get('state')!='verified' for k in ('risk','actions')):
                    decision['formal_blockers'].append('风险或公司行动核验在签发前已失效')
                    decision['action']='watch';formal=False
                elif reassessment.revisions(c,reassessment.cited_ids(decision),stamp):
                    decision['formal_blockers'].append('签发前发现所引用事实已更正，须复核新版本')
                    decision['action']='watch';formal=False
            if symbol in originals:
                if bundle['mode']=='retrospective':
                    decision['formal_blockers'].append('历史证据仅用于回看，当前周期等待有效截点的复核')
                    continue
                effects = _record_reviews(c,run_id,stamp,decision,originals[symbol],bundle['knowledge_cutoff'])
                first = effects[0]
                decision.update(cycle_id=first['cycle_id'],review_event=True,tracking_state=first['state'],
                                original_endpoints=first['endpoints'],original_entry=first['entry'],
                                original_invalidation=first['invalidation'],review_effects=effects)
                affected_cycles.extend(effect['cycle_id'] for effect in effects)
                continue
            closed_during_run = c.execute(
                "SELECT id FROM research_cycles WHERE symbol=%s AND state='removed' "
                "AND first_removed_at>=%s LIMIT 1",
                (symbol,state_store.sql_time(row['created'])),
            ).fetchone()
            if closed_during_run:
                decision['cycle_id']=closed_during_run['id']
                if formal:
                    decision['action']='watch'
                decision['formal_blockers'].append('研究启动后股票已移出，保留原稿供复核')
                continue
            cap = profile.get('max_industry_recommendations')
            group = evidence_by_symbol.get(symbol,{}).get('industry')
            if formal and cap:
                if not group or sector_counts.get(group,0)>=cap:
                    decision['formal_blockers'].append('行业归属缺失或已达到确认的同行推荐上限')
                    decision['action']='watch';formal=False
                else:
                    sector_counts[group]=sector_counts.get(group,0)+1
            thesis_key = _thesis_key(c, decision)
            horizon = extended or data.get('research_plan', {}).get('horizon', 'D3')
            session = report['session']
            signal = decision.get('rise')
            rise_key = [report['rise_policy_version'],signal['type']] if signal else []
            rise_match = (" AND rise_policy_version=%s AND rise_type=%s" if signal
                          else " AND rise_type IS NULL")
            overlap=c.execute("SELECT * FROM research_cycles WHERE symbol=%s AND formal=%s AND state NOT IN ('expired','removed','rejected') "
                "AND method_version=%s AND thesis_key=%s AND research_horizon=%s AND session=%s"+rise_match+
                " ORDER BY created DESC LIMIT 1 FOR UPDATE",(symbol,formal,method_version,thesis_key,horizon,session,*rise_key)).fetchone()
            if overlap:
                prev=state_store.load_cycle_data(c,overlap)
                last = sessions.final_day(prev.get('endpoints',{}))
                if last and last>=row['market_day'] and bool(overlap['formal'])==formal and (
                        not target or session=='close' or prev['market_day']==row['market_day']):
                    decision.update(cycle_id=overlap['id'],original_entry=prev['entry'],
                                    original_invalidation=prev['invalidation'],original_endpoints=prev['endpoints'])
                    affected_cycles.append(overlap['id'])
                    event(c,overlap['id'],run_id,'reaffirm',{'thesis':decision['thesis'],'original_contract_unchanged':True},['reaffirm',run_id,overlap['id']])
                    continue
            window_key = [session] if target and session!='close' else []
            identity=digest([symbol,method_version,thesis_key,horizon,row['market_day'],formal,
                             'rejected' if decision['action']=='reject' else 'observed',*rise_key,*window_key])
            cycle_id=uuid.uuid4().hex[:16]
            original={'name':decision['name'],'industry':decision['industry'],'thesis':decision['thesis'],'action':decision['action'],
              'entry':decision['entry'],'invalidation':decision['invalidation'],'next_checks':decision['next_checks'],
              'endpoints':ends,'entry_reference':None,'method_version':method_version,'first_run':run_id,
              'published_at':stamp,'market_day':row['market_day'],'mode':bundle['mode'],'reference_kind':'quote_reference_not_trade',
              'session_policy':policy,'adjustment_verified':bool(decision.get('adjustment_verified')),
              'structure_guard':decision.get('structure_guard'),
              'rise':decision.get('rise'),'rise_policy_version':report.get('rise_policy_version'),
              'thesis_key':thesis_key,'research_horizon':horizon,'dependency_ids':reassessment.cited_ids(decision),
              'benchmark_symbols':evidence_by_symbol.get(symbol,{}).get('benchmark_symbols',[]),
              'baseline':'签发前冻结同业篮子与同窗配对规则','formal_blockers':decision['formal_blockers']}
            state='waiting' if formal else 'rejected' if decision['action']=='reject' else 'observe'
            written = c.execute('INSERT IGNORE INTO research_cycles(id,identity_key,run_id,symbol,market_day,created,updated,state,formal,data) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
              (cycle_id,identity,run_id,symbol,row['market_day'],stamp,stamp,state,formal,store.dumps(original)))
            existing=c.execute('SELECT id,state FROM research_cycles WHERE identity_key=%s',(identity,)).fetchone()
            actual=existing['id']
            if written.rowcount:
                state_store.sync_cycle(c,actual,original)
            if not written.rowcount and existing['state'] in ('removed','expired') and formal:
                decision['formal_blockers'].append('同一论点与期限的原周期已关闭，须用独立新依据另行研究')
                decision['action']='watch'
            decision['cycle_id']=actual
            affected_cycles.append(actual)
            event(c,actual,run_id,'original',{'action':decision['action'],'thesis':decision['thesis'],'formal':formal,'endpoints':ends},['original',actual])
            if written.rowcount:
                for window,endpoint in ends.items():
                    _outcome(c,actual,window,'original',evaluate_contract(original,formal,endpoint,window,None,stamp),stamp)
        cited={fid for decision in report['decisions'] for fid in reassessment.cited_ids(decision)}
        cited.update(fid for decision in report['decisions'] for item in decision.get('review_items',[]) for fid in item['fact_ids'])
        cited.update(flag['fact_id'] for targets in originals.values() for target in targets for flag in target.get('flags',[])
                     if any(d.get('review_event') and d['symbol']==target['symbol'] for d in report['decisions']))
        # Pin only cited facts. The compact bundle retains IDs for deterministic calculation provenance.
        for fid in cited:c.execute("INSERT IGNORE INTO evidence_refs VALUES('research',%s,%s,%s)",(run_id,fid,stamp))
        review.freeze_controls(c,run_id,report,bundle,data.get('shadow_signals',{}))
        delivery.register(c,run_id,'report',{'title':report['title'],'market_day':report['market_day'],
                                             'session':report['session'],'mode':report['mode'],
                                             'formal_count':sum(d['action'] in ('select','conditional') and not d.get('review_event') for d in report['decisions'])})
        data.pop('validated_report',None)
        data['published_at']=stamp
        status='completed_limited' if any(d['formal_blockers'] for d in report['decisions']) else 'completed'
        c.execute('UPDATE research_runs SET status=%s,stage=%s,updated=%s,data=%s,report=%s WHERE id=%s',
          (status,'迟发回看已保存' if late else '报告与跟踪已登记',stamp,store.dumps(data),store.dumps(report),run_id))
        state_store.sync_run(c,run_id,data,report)
    try:
        refresh_outcomes(cycle_ids=list(dict.fromkeys(affected_cycles)))
    except Exception:
        log.exception('published report %s; evaluation will retry independently', run_id)
    return report


def correct(run_id, raw):
    if set(raw)-{'symbol','action','reason','confirm'} or raw.get('confirm') is not True:
        raise HTTPException(422,'请明确确认这条原稿更正')
    symbol, action, reason = (raw.get(k) for k in ('symbol','action','reason'))
    if action not in ('note','withdraw') or not isinstance(reason,str) or not 1<=len(reason.strip())<=800:
        raise HTTPException(422,'请提供更正方式及1至800字理由')
    if symbol is not None and not isinstance(symbol,str):
        raise HTTPException(422,'请输入报告内证券代码')
    stamp = store.now()
    with store.db() as conn:
        row = conn.execute('SELECT report,data FROM research_runs WHERE id=%s FOR UPDATE',(run_id,)).fetchone()
        if not row or not row['report']:
            raise HTTPException(404,'没有可更正的已签发原稿')
        report=json.loads(row['report'])
        decision=next((d for d in report['decisions'] if d['symbol']==symbol),None)
        if symbol is not None and (not decision or action=='withdraw' and decision.get('review_event')):
            raise HTTPException(422,'撤回须指定原报告中的推荐或观察对象')
        existing=delivery.corrections(conn,run_id)
        whole = action=='withdraw' and symbol is None
        same=next((item for item in existing if (item.get('symbol'),item['action'])==(symbol,action)
                   and (whole or item['reason']==reason.strip())),None)
        if same:
            return same
        if len(existing)>=50:
            raise HTTPException(409,'原稿更正数量已达上限')
        value={'title':report['title'],'market_day':report['market_day'],'symbol':symbol,'action':action,'reason':reason.strip()}
        identifier,_=delivery.register(conn,run_id,'correction',value)
        if whole:
            run_data=json.loads(row['data'])
            run_data['withdrawn_at']=stamp
            conn.execute('UPDATE research_runs SET data=%s WHERE id=%s',(store.dumps(run_data),run_id))
            state_store.sync_run(conn,run_id,run_data)
        if action=='withdraw':
            targets = report['decisions'] if whole else [decision]
            for target in targets:
                if target.get('review_event') or not target.get('cycle_id'):
                    continue
                cy=conn.execute('SELECT * FROM research_cycles WHERE id=%s FOR UPDATE',(target['cycle_id'],)).fetchone()
                if not cy or cy['run_id']!=run_id or cy['state'] not in reassessment.ACTIVE:
                    continue
                original=state_store.load_cycle_data(conn,cy)
                original['withdrawn_at']=stamp
                original.setdefault('first_removed_at',stamp)
                conn.execute("UPDATE research_cycles SET state='removed',updated=%s,data=%s WHERE id=%s",
                             (stamp,store.dumps(original),cy['id']))
                state_store.sync_cycle(conn,cy['id'],original)
                event(conn,cy['id'],run_id,'correction',{'from':cy['state'],'to':'removed','notice_id':identifier,
                    'reason':reason.strip()},['correction',identifier,cy['id']])
                for horizon,endpoint in original['endpoints'].items():
                    result=_removal_contract({**original,'entry_reference':None},bool(cy['formal']),endpoint,horizon,None,stamp)
                    _outcome(conn,cy['id'],horizon,'removal',result,stamp)
        return {'id':identifier,'at':stamp,**value}

def list_cycles(limit=25,before=None,rise_type=None):
    filters=[];params=[]
    if rise_type:
        filters.append("rise_type=%s")
        params.append(rise_type)
    clause = ' WHERE '+' AND '.join(filters) if filters else ''
    with store.db(readonly=True) as c:
        total=c.execute('SELECT count(*) FROM research_cycles'+clause,params).fetchone()[0]
        if before:
            if len(before)!=16 or not all(ch in '0123456789abcdef' for ch in before):raise HTTPException(422,'跟踪游标无效')
            key = ' AND ' if filters else ' WHERE '
            r=c.execute('SELECT created,id FROM research_cycles'+clause+key+'id=%s',(*params,before)).fetchone()
            if not r:raise HTTPException(409,'跟踪游标已失效，请刷新')
            clause+=key+'(created,id)<(%s,%s)'
            params.extend((r['created'],r['id']))
        rows=c.execute('SELECT * FROM research_cycles'+clause+' ORDER BY created DESC,id DESC LIMIT %s',(*params,limit+1)).fetchall()
        shown=state_store.overlay_cycles(c,rows[:limit])
    return {'items':[{k:r[k] for k in ('id','run_id','symbol','market_day','created','updated','state','formal')}|{'data':r['data']} for r in shown],'total':total,'next':rows[limit-1]['id'] if len(rows)>limit else None}

def cycle_detail(cycle_id):
    with store.db(readonly=True) as c:
        row=c.execute('SELECT * FROM research_cycles WHERE id=%s',(cycle_id,)).fetchone()
        if not row:raise HTTPException(404,'跟踪周期不存在')
        events=[{**dict(r),'data':json.loads(r['data'])} for r in c.execute('SELECT * FROM research_events WHERE cycle_id=%s ORDER BY id DESC LIMIT 100',(cycle_id,))]
        results=[{**dict(r),'data':json.loads(r['data'])} for r in c.execute('SELECT * FROM research_outcomes WHERE cycle_id=%s ORDER BY horizon,kind',(cycle_id,))]
        data=state_store.load_cycle_data(c,row)
        flags=reassessment.flags(c,row,data) if row['state'] in reassessment.ACTIVE else []
    return {**dict(row),'data':data,'events':events,'outcomes':results,'review_flags':flags}


def quote_window(conn, cycle, data, kind):
    if kind=='entry':
        end = data.get('session_policy',{}).get('action_valid_until')
        if not end and data['endpoints'].get('D1'):
            end = sessions.local_time(data['endpoints']['D1'],'14:55')
        start = max(data['published_at'], data.get('session_policy',{}).get('reference_from') or data['published_at'])
    else:
        start = data.get('first_removed_at') or data.get('last_review',{}).get('at')
        final = sessions.final_day(data['endpoints'])
        end = sessions.local_time(final,'14:55') if final else None
    if not start or not end or start>=end:
        return []
    when = 'q.source_time'
    start_time = datetime.fromisoformat(start).astimezone(store.SHANGHAI).replace(tzinfo=None)
    end_time = datetime.fromisoformat(min(end,store.now())).astimezone(store.SHANGHAI).replace(tzinfo=None)
    rows = conn.execute(
        f"SELECT f.id,{when} stamp FROM facts f JOIN quote_snapshots q ON q.fact_id=f.id WHERE f.dataset='quotes' AND f.symbol=%s "
        "AND f.day BETWEEN %s AND %s AND q.source_time>%s AND q.source_time<=%s AND f.first_seen<=%s "
        "AND (TIME(q.source_time) BETWEEN '09:30:00' AND '11:29:59' OR TIME(q.source_time) BETWEEN '13:00:00' AND '14:56:59') "
        f"ORDER BY q.source_time,f.first_seen,f.id LIMIT 1001",
        (cycle['symbol'],start[:10],end[:10],start_time,end_time,store.now()),
    ).fetchall()
    valid=[]
    entry=data['entry']
    signal=data.get('rise') or {}
    trigger=signal.get('trigger_above')
    floor=signal.get('invalidation_price')
    for r in record_store.hydrate(conn, rows[:1000]):
        quote=r['payload'];stamp=state_store.iso_time(r['stamp']);price=quote.get('close')
        clock=stamp[11:19]
        if not ('09:30:00'<=clock<'11:30:00' or '13:00:00'<=clock<'14:57:00'):
            continue
        if sessions.trading_day(conn, stamp[:10]) is not True or not price or not (quote.get('volume') or 0)>0:
            continue
        if kind=='entry' and (entry['low'] is None or not entry['low']<=price<=entry['high']):
            continue
        if kind=='entry' and (trigger is not None and price<=trigger or floor is not None and price<floor):
            continue
        valid.append({'fact_id':r['id'],'at':stamp,'price':price})
        break
    return valid


def available_reference(cycle_id, kind):
    if kind not in ('entry','removal'):raise HTTPException(422,'参考价用途无效')
    with store.db(readonly=True) as conn:
        cy=conn.execute('SELECT * FROM research_cycles WHERE id=%s',(cycle_id,)).fetchone()
        if not cy:raise HTTPException(404,'周期不存在')
        data=state_store.load_cycle_data(conn,cy)
        expected='waiting' if kind=='entry' else 'removed'
        if not cy['formal'] or cy['state']!=expected or data.get(kind+'_reference'):
            return {'items':[]}
        if kind=='entry' and any(f['kind'] in reassessment.CRITICAL for f in reassessment.flags(conn,cy,data)):
            return {'items':[],'blocked':'原参与或结构条件已失效，先复核原论点'}
        return {'items':quote_window(conn,cy,data,kind)}


def record_reference(cycle_id,fact_id,confirmed,kind='entry'):
    if confirmed is not True:raise HTTPException(422,'必须明确确认原非价格条件已满足')
    if kind not in ('entry','removal'):raise HTTPException(422,'参考价用途无效')
    with store.db() as c:
        cy=c.execute('SELECT * FROM research_cycles WHERE id=%s FOR UPDATE',(cycle_id,)).fetchone()
        if not cy:raise HTTPException(404,'周期不存在')
        data=state_store.load_cycle_data(c,cy)
        required='waiting' if kind=='entry' else 'removed'
        if not cy['formal'] or cy['state']!=required:raise HTTPException(409,'只有相应状态的正式周期可登记参考报价')
        ref_key='entry_reference' if kind=='entry' else 'removal_reference'
        if data.get(ref_key):raise HTTPException(409,'已登记首个合格参考价，不能重置锚点')
        if kind=='entry' and any(f['kind'] in reassessment.CRITICAL for f in reassessment.flags(c,cy,data)):
            raise HTTPException(409,'原参与或结构条件已失效，先复核原论点')
        eligible = quote_window(c,cy,data,kind)
        if not eligible or eligible[0]['fact_id']!=fact_id:
            raise HTTPException(422,'请选择原窗口内的首个合格参考报价')
        data[ref_key]={**eligible[0],'confirmed_at':store.now(),'kind':'quote_reference_not_trade',
                       'conditions_confirmed':True,'adjustment_verified':data.get('adjustment_verified')}
        data[kind+'_peer_refs']=review.capture_peers(c,data.get('benchmark_symbols',[]),data[ref_key]['at'])
        c.execute('UPDATE research_cycles SET state=%s,updated=%s,data=%s WHERE id=%s',('active' if kind=='entry' else 'removed',store.now(),store.dumps(data),cycle_id))
        state_store.sync_cycle(c,cycle_id,data)
        c.execute("INSERT IGNORE INTO evidence_refs VALUES('cycle',%s,%s,%s)",(cycle_id,fact_id,store.now()))
        review.pin(c,'cycle',cycle_id,[q['fact_id'] for q in data[kind+'_peer_refs']])
        event(c,cycle_id,None,'trigger' if kind=='entry' else 'removal_reference',data[ref_key],[ref_key,cycle_id])
    refresh_outcomes(cycle_ids=[cycle_id])
    return cycle_detail(cycle_id)

def evaluate_contract(data,formal,endpoint,horizon,bar,now):
    if not formal:return {'status':'observation','reason':'候补观察不是正式推荐，不计正式命中率','endpoint':endpoint}
    if not endpoint:return {'status':'missing_calendar','reason':'缺少原定交易日端点','endpoint':None}
    if now<endpoint+'T15:00:00+08:00':return {'status':'not_due','endpoint':endpoint}
    ref=data.get('entry_reference')
    if not ref:return {'status':'untriggered','endpoint':endpoint,'reason':'原窗口内未登记合格参考价'}
    if ref['at']>endpoint+'T15:00:00+08:00':return {'status':'late_trigger','endpoint':endpoint,'reason':'触发晚于原端点，不顺延'}
    if not bar or bar.get('close') is None or bar.get('suspended'):return {'status':'missing_price','endpoint':endpoint,'reason':'终点无合格收盘价'}
    if not data.get('adjustment_verified'):return {'status':'not_comparable','endpoint':endpoint,'reason':'公司行动与起终价格可比性未核验'}
    value=bar['close']/ref['price']-1
    return {'status':'complete','endpoint':endpoint,'reference':ref,'close':bar['close'],'return':value,'direction':'up' if value>0 else 'down' if value<0 else 'flat','benchmark_return':None,'excess_return':None,'kind':'quote_reference_not_trade'}

def refresh_outcomes(*, cycle_ids=None):
    if cycle_ids == []:
        return
    now=store.now()
    with store.db() as c:
        if cycle_ids:
            rows=c.execute("SELECT * FROM research_cycles WHERE id IN ("+','.join(['%s']*len(cycle_ids))+") FOR UPDATE",tuple(cycle_ids)).fetchall()
        else:
            # Bounded rotation among formal rows. Refresh time is independent of user-facing state changes.
            marker=store.setting('research_outcomes_cursor','',conn=c)
            rows=c.execute("SELECT * FROM research_cycles WHERE id>%s ORDER BY id LIMIT 100 FOR UPDATE",(marker,)).fetchall()
            store.set_setting('research_outcomes_cursor',rows[-1]['id'] if len(rows)==100 else '',conn=c)
        for row in state_store.overlay_cycles(c,rows):
            data=row['data']
            for horizon,endpoint in data['endpoints'].items():
                found=market.closing_bar(c,row['symbol'],endpoint)
                original = data
                if endpoint and found and data.get('entry_reference') and data.get('adjustment_verified'):
                    original = {**data, 'adjustment_verified':market.comparable_window(c,row['symbol'],data['entry_reference'],endpoint)}
                result=evaluate_contract(original,bool(row['formal']),endpoint,horizon,found,now)
                identity=f"{row['id']}/{horizon}/original"
                if result['status']=='complete':
                    result['closing_fact']=found['fact_id']
                    review.pin(c,'outcome',identity,[found['fact_id']])
                    review.benchmark(c,result,data.get('benchmark_symbols',[]),data.get('entry_peer_refs',[]),identity)
                _outcome(c,row['id'],horizon,'original',result,now)
                if row['state']=='removed':
                    removal_data={**data,'entry_reference':data.get('removal_reference')}
                    if endpoint and found and removal_data['entry_reference'] and data.get('adjustment_verified'):
                        removal_data['adjustment_verified']=market.comparable_window(c,row['symbol'],removal_data['entry_reference'],endpoint)
                    result_remove=_removal_contract(removal_data,bool(row['formal']),endpoint,horizon,found,now)
                    identity=f"{row['id']}/{horizon}/removal"
                    if result_remove['status']=='complete':
                        result_remove['closing_fact']=found['fact_id']
                        review.pin(c,'outcome',identity,[found['fact_id']])
                        review.benchmark(c,result_remove,data.get('benchmark_symbols',[]),data.get('removal_peer_refs',[]),identity)
                    _outcome(c,row['id'],horizon,'removal',result_remove,now)
            end=sessions.final_day(data['endpoints'])
            if end and now>=end+'T15:00:00+08:00' and row['state'] not in ('expired','removed','rejected'):
                c.execute('UPDATE research_cycles SET state=%s,updated=%s WHERE id=%s',('expired',now,row['id']))
                event(c,row['id'],None,'expiry',{'endpoint':end,'reason':'到达原定观察窗口终点'},['expiry',row['id']])
        review.refresh(c,now)

def performance(rise_type=None):
    class_clause = " AND c.rise_type=%s" if rise_type else ""
    params = (rise_type,) if rise_type else ()
    outcomes = " FROM research_outcomes o JOIN research_cycles c ON c.id=o.cycle_id WHERE c.formal=1"+class_clause
    with store.db(readonly=True) as c:
        rows=c.execute("SELECT o.horizon,o.kind,o.status,count(*) n"+outcomes+
                       " GROUP BY o.horizon,o.kind,o.status",params).fetchall()
        wins=c.execute("SELECT o.horizon,o.kind,count(*) n"+outcomes+" AND o.status='complete' AND "
                       "((o.kind='original' AND o.direction='up') OR "
                       "(o.kind='removal' AND o.direction='down')) "
                       "GROUP BY o.horizon,o.kind",params).fetchall()
        watch=c.execute('SELECT count(*) FROM research_cycles c WHERE c.formal=0'+class_clause,params).fetchone()[0]
        cycle_counts=c.execute("SELECT COUNT(*) cycles,COUNT(DISTINCT symbol) securities,"
                               "COUNT(DISTINCT symbol,c.thesis_key) theses "
                               "FROM research_cycles c WHERE 1=1"+class_clause,params).fetchone()
        if rise_type:
            mentions=c.execute("SELECT COUNT(DISTINCT r.id) reports,COUNT(*) mentions FROM research_runs r "
                               "JOIN research_decisions d ON d.run_id=r.id "
                               "WHERE r.report IS NOT NULL AND d.rise_type=%s",params).fetchone()
        else:
            mentions=c.execute("SELECT COUNT(*) reports,SUM(decision_count) mentions "
                               "FROM research_runs WHERE report IS NOT NULL").fetchone()
        paired=c.execute("SELECT o.horizon,o.kind,COUNT(*) n,"
                         "AVG(o.excess_return) excess"+outcomes+
                         " AND o.status='complete' AND o.excess_return IS NOT NULL "
                         "GROUP BY o.horizon,o.kind",params).fetchall()
    groups={}
    for row in rows:groups.setdefault((row['horizon'],row['kind']),{})[row['status']]=row['n']
    winning={(r['horizon'],r['kind']):r['n'] for r in wins}
    pairs={(r['horizon'],r['kind']):(r['n'],float(r['excess'])) for r in paired}
    result=[]
    for h in (('PM','D1','D3') if any(key[0]=='PM' for key in groups) else ('D1','D3')):
        for kind in ('original','removal'):
            statuses=groups.get((h,kind),{});total=sum(statuses.values());evaluable=statuses.get('complete',0)
            due=total-statuses.get('not_due',0)-statuses.get('missing_calendar',0)
            paired_count,mean_excess=pairs.get((h,kind),(0,None))
            result.append({'horizon':h,'kind':kind,'total':total,'due':due,'evaluable':evaluable,'wins':winning.get((h,kind),0),
              'hit_rate':winning.get((h,kind),0)/evaluable if evaluable else None,'coverage':evaluable/due if due else None,
              'paired':paired_count,'mean_excess':mean_excess,'statuses':statuses})
    return {'groups':result,'observations':watch,'rise_type':rise_type,'counts':{**dict(cycle_counts),'reports':mentions['reports'],
            'mentions':int(mentions['mentions'] or 0)},'note':'原判按参考入池后上涨评价，剔除按生效后下跌评价。全部、到期和可评样本分别显示。'}
