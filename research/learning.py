"""Versioned, bounded method experiments over matched forward observations."""
from __future__ import annotations

from collections import defaultdict
import json
from statistics import fmean
import uuid

from fastapi import HTTPException

from foundation import assets, database as store


ACTIVE_KEY = 'research_active_method'
EXPERIMENT_KEY = 'research_method_experiments'
BASE = {'version':'evidence-paths-v0.2','relative_volume_min':1.3,'close_location_min':.65,'sector_breadth_min':.65}
FACTORS = {
    'relative_volume_min':{'label':'突破相对量','min':1.0,'max':3.0,'step':.05,'path':'O03'},
    'close_location_min':{'label':'突破收盘位置','min':.5,'max':.95,'step':.05,'path':'O03'},
    'sector_breadth_min':{'label':'同行上涨家数占比','min':.5,'max':.95,'step':.05,'path':'O02'},
}


def active(conn=None):
    return {**BASE,**store.setting(ACTIVE_KEY, {}, conn=conn)}


def experiments(conn=None):
    return store.setting(EXPERIMENT_KEY, [], conn=conn)


def _locked_experiments(conn):
    conn.execute('INSERT IGNORE INTO settings VALUES(%s,%s)', (EXPERIMENT_KEY, '[]'))
    return json.loads(conn.execute('SELECT data FROM settings WHERE `key`=%s FOR UPDATE', (EXPERIMENT_KEY,)).fetchone()[0])


def list_status():
    with store.db(readonly=True) as conn:
        method=active(conn)
        return {'active':method,'previous':store.setting('research_previous_method',None,conn=conn),
                'items':experiments(conn),'factors':FACTORS,'diagnostics':diagnostics(conn,method),
                'scope':'比较同一批已冻结的入选、未选和池外样本。'}


def create(raw):
    if set(raw) - {'title', 'threshold','factor', 'train_start', 'train_end', 'validation_end', 'test_end',
                   'min_samples', 'min_days','cost_bps','auto_publish','confirm_auto'}:
        raise HTTPException(422, '实验包含未知字段')
    title = raw.get('title')
    threshold = raw.get('threshold')
    factor=raw.get('factor','relative_volume_min')
    spec=FACTORS.get(factor) if isinstance(factor,str) else None
    if not spec or not isinstance(title,str) or not title.strip() or len(title)>100 or type(threshold) not in (int,float) or not spec['min']<=threshold<=spec['max']:
        raise HTTPException(422,'填写实验名称，并选择有效的参数和阈值')
    dates = {key: assets.day(raw.get(key)) for key in ('train_start', 'train_end', 'validation_end', 'test_end')}
    if not dates['train_start'] <= dates['train_end'] < dates['validation_end'] < dates['test_end'] < store.today():
        raise HTTPException(422, '历史训练、验证和测试按时间顺序分段，结束日须早于今天')
    minimum = raw.get('min_samples', 20)
    days = raw.get('min_days', 6)
    if type(minimum) is not int or not 20 <= minimum <= 300 or type(days) is not int or not 6 <= days <= 60:
        raise HTTPException(422, '最少20个可评周期、6个交易日，实际门槛可提高')
    cost = raw.get('cost_bps')
    if cost is not None and (type(cost) not in (int,float) or not 0<=cost<=1000):
        raise HTTPException(422,'实验每次往返总成本须为0至1000个基点')
    automatic = raw.get('auto_publish') is True
    if automatic and (raw.get('confirm_auto') is not True or cost is None):
        raise HTTPException(422,'自动发布须明确确认，并预先登记往返成本情景')
    with store.db() as conn:
        items = _locked_experiments(conn)
        if len(items) >= 20 or sum(e['state'] == 'shadow' for e in items) >= 3:
            raise HTTPException(409, '实验保留上限为20项，同时最多3个影子实验')
        baseline = active(conn)
        if threshold == baseline[factor]:
            raise HTTPException(422, '候选参数须与当前版本有差异')
        entry = {'id':uuid.uuid4().hex[:16],'title':title.strip(),'threshold':threshold,'factor':factor,'sample_basis':'same_window_controls',
                 'registered_at': store.now(), 'baseline': baseline, **dates,
                 'min_samples': minimum, 'min_days': days, 'state': 'shadow',
                 'cost_bps':cost,'auto_publish':automatic,
                 'release_policy':'matched-daily-v1'}
        store.set_setting(EXPERIMENT_KEY, [entry, *items], conn=conn)
        store.audit('research_experiment_created', {'id': entry['id']}, conn=conn)
    return entry


def shadow_candidates(conn=None):
    return [{k:e[k] for k in ('id','threshold','registered_at','baseline')}|{'factor':e.get('factor','relative_volume_min')}
            for e in experiments(conn) if e['state'] == 'shadow']


def signal(metrics, threshold):
    return (metrics.get('comparable') and metrics.get('close', 0) > metrics.get('prior_high20', float('inf'))
            and (metrics.get('close_location') or 0) >= .65 and (metrics.get('relative_volume20') or 0) >= threshold)


def experiment_signal(candidate, method, factor):
    metrics=candidate['metrics']
    if FACTORS[factor]['path']=='O02':
        sector=candidate.get('sector',{})
        return bool(sector.get('members',0)>=5 and sector['advancing']/sector['members']>=method['sector_breadth_min']
                    and (sector['mean_return'] or 0)>0 and metrics.get('comparable') and metrics.get('return_1') is not None
                    and metrics['return_1']>sector['mean_return'] and metrics['close']>metrics['ma20'])
    return bool(metrics.get('comparable') and metrics.get('close',0)>metrics.get('prior_high20',float('inf'))
                and (metrics.get('close_location') or 0)>=method['close_location_min']
                and (metrics.get('relative_volume20') or 0)>=method['relative_volume_min'])


def observation_features(candidate):
    return {'metrics':{k:candidate['metrics'].get(k) for k in ('comparable','close','prior_high20','close_location','relative_volume20','return_1','ma20')},
            'sector':{k:v for k,v in candidate.get('sector',{}).items() if k in ('members','advancing','mean_return')}}


def observe(items, bundle):
    candidates=bundle['candidates']+bundle.get('comparison_samples',[])
    return {e['id']:{c['symbol']:experiment_signal(c,{**BASE,**e['baseline'],e['factor']:e['threshold']},e['factor'])
                    for c in candidates if 'metrics' in c} for e in items if e['baseline']['version']==bundle['method_version']}


def _observations(conn, method_version, start='0000-01-01', limit=2001):
    rows=conn.execute("SELECT c.id,c.symbol,c.cohort,c.endpoint,c.status,c.data,r.market_day,r.created "
                      "FROM research_controls c JOIN research_runs r ON r.id=c.run_id WHERE c.horizon='D3' AND r.market_day>=%s "
                      "AND c.method_version=%s ORDER BY r.market_day,c.start_at,c.id LIMIT %s",
                      (start,method_version,limit)).fetchall()
    seen=set()
    observations=[]
    for row in rows[:limit-1]:
        key=(row['market_day'],row['symbol'])
        if key in seen:continue
        seen.add(key)
        data=json.loads(row['data'])
        if 'features' in data:
            observations.append({**dict(row),'data':data})
    return observations,len(rows)>=limit


def diagnostics(conn, method):
    rows,_=_observations(conn,method['version'],limit=601)
    matured=[r for r in rows if r['status']=='complete' and r['endpoint']<store.today()]
    days=sorted({r['market_day'] for r in matured})
    if len(days)<12:
        return {'sample_days':len(days),'evaluable':len(matured),'proposals':[],'state':'insufficient'}
    split=len(days)//2
    train_end=days[split]
    validation_end=days[split+max(1,(len(days)-split)//2)]
    test_end=max(r['endpoint'] for r in matured)
    train=[r for r in matured if r['market_day']<=train_end and r['endpoint']<=train_end]
    proposals=[]
    for factor,spec in FACTORS.items():
        threshold=round(min(spec['max'],method[factor]+(0.2 if factor=='relative_volume_min' else .1)),2)
        if threshold<=method[factor]:continue
        stricter={**method,factor:threshold}
        changed=[r for r in train if experiment_signal(r['data']['features'],method,factor) and not experiment_signal(r['data']['features'],stricter,factor)]
        losses=[r for r in changed if r['data']['return_']<0]
        loss_days=len({r['market_day'] for r in losses})
        if len(losses)>=3 and loss_days>=3 and len(losses)>sum(r['data']['return_']>0 for r in changed):
            proposals.append({'factor':factor,'threshold':threshold,'title':f"复核{spec['label']}边界",
                              'train_start':days[0],'train_end':train_end,'validation_end':validation_end,'test_end':test_end,
                              'losses':len(losses),'training_days':loss_days,'changed':len(changed)})
    return {'sample_days':len(days),'evaluable':len(matured),'proposals':proposals,'state':'observed'}


def _stats(rows, cost=0):
    valid = [r for r in rows if r['return'] is not None]
    by_day = defaultdict(list)
    for row in valid:
        by_day[row['day']].append((row['return']-cost)*(int(row['candidate'])-int(row['baseline'])))
    result = {'total': len(rows), 'evaluable': len(valid), 'days': len({r['day'] for r in valid}),
              'different': sum(r['baseline'] != r['candidate'] for r in valid),
              'equal_day_advantage':fmean([fmean(group) for group in by_day.values()]) if by_day else None,
              'improved_days':sum(fmean(group)>0 for group in by_day.values())}
    for side in ('baseline', 'candidate'):
        selected = sorted(r['return']-cost for r in valid if r[side])
        tail = selected[:max(1,len(selected)//10)]
        result[side] = {'selected': len(selected), 'mean_per_sample': fmean([r['return']-cost if r[side] else 0 for r in valid]) if valid else None,
                        'mean_selected': fmean(selected) if selected else None,
                        'worst_selected': min(selected) if selected else None,
                        'tail_mean':fmean(tail) if tail else None,
                        'missed_winners': sum(r['return'] > cost and not r[side] for r in valid)}
    return result


def evaluate(e, conn):
    # Compare unique original cycles only. Reaffirmation and later reviews cannot add samples.
    if e.get('sample_basis')=='same_window_controls':
        observations,truncated=_observations(conn,e['baseline']['version'],e['train_start'])
        groups=defaultdict(list)
        factor=e['factor'];baseline={**BASE,**e['baseline']};candidate={**baseline,factor:e['threshold']}
        for row in observations:
            stamp=row['created'];day=row['market_day'];outcome=row['data']
            if stamp<e['registered_at']:
                stage,last=next(((name,e[key]) for name,key in (('train','train_end'),('validation','validation_end'),('test','test_end')) if day<=e[key]),(None,None))
                if not stage or row['endpoint']>last:continue
                prediction=experiment_signal(outcome['features'],candidate,factor)
            else:
                if any(window['from']<=stamp and (not window.get('to') or stamp<window['to']) for window in e.get('pauses',[])):
                    continue
                signals=outcome.get('shadow',{})
                if e['id'] not in signals or row['endpoint']>=store.today():continue
                stage='shadow';prediction=signals[e['id']]
            groups[stage].append({'day':day,'baseline':experiment_signal(outcome['features'],baseline,factor),
                                  'candidate':prediction,'return':outcome.get('return_') if row['status']=='complete' else None})
        return _evaluation(e,groups,truncated)
    rows = conn.execute(
        "SELECT c.id,c.symbol,c.market_day,r.created,r.data,o.status,o.data outcome FROM research_cycles c "
        "JOIN research_runs r ON r.id=c.run_id LEFT JOIN research_outcomes o ON o.cycle_id=c.id "
        "AND o.horizon='D3' AND o.kind='original' WHERE c.formal=1 AND c.market_day>=%s "
        "AND r.report IS NOT NULL AND c.method_version=%s "
        "ORDER BY c.market_day,c.id LIMIT 2001", (e['train_start'], e['baseline']['version']),
    ).fetchall()
    groups = defaultdict(list)
    for row in rows[:2000]:
        data = json.loads(row['data'])
        bundle = data.get('bundle', {})
        if bundle.get('mode') == 'retrospective':
            continue
        day = row['market_day']
        current = next((c for c in bundle.get('candidates', []) if c['symbol'] == row['symbol']), None)
        if not current:
            continue
        outcome = json.loads(row['outcome']) if row['outcome'] else {}
        endpoint = outcome.get('endpoint')
        before_registration = row['created'] < e['registered_at']
        if before_registration:
            stage, last_day = next(((name, e[key]) for name, key in (
                ('train', 'train_end'), ('validation', 'validation_end'), ('test', 'test_end')) if day <= e[key]), (None, None))
            if not stage or not endpoint or endpoint > last_day:
                continue
            predicted = bool(signal(current['metrics'], e['threshold']))
        else:
            shadow = data.get('shadow_signals', {}).get(e['id'], {})
            if row['symbol'] not in shadow or not endpoint or endpoint >= store.today():
                continue
            stage, predicted = 'shadow', shadow[row['symbol']]
        groups[stage].append({'day': day, 'baseline': bool(signal(current['metrics'], e['baseline']['relative_volume_min'])),
                              'candidate': predicted, 'return': outcome.get('return') if row['status'] == 'complete' else None})
    return _evaluation(e,groups,len(rows)>2000)


def _evaluation(e, groups, truncated):
    stages = {name:_stats(groups[name],(e.get('cost_bps') or 0)/10000) for name in ('train','validation','test','shadow')}

    def passed(part):
        a, b = part['baseline'], part['candidate']
        return (part['evaluable'] >= e['min_samples'] and part['days'] >= e['min_days'] and part['different'] > 0
                and part['evaluable'] / part['total'] >= .8
                and a['selected'] > 0 and b['selected'] > 0
                and b['mean_per_sample'] > a['mean_per_sample']
                and part['equal_day_advantage'] > 0 and part['improved_days'] >= 3
                and b['missed_winners'] <= a['missed_winners'] and b['worst_selected'] >= a['worst_selected'])

    return {'stages':stages,'truncated':truncated,
            'qualified':not truncated and passed(stages['test']) and passed(stages['shadow']),
            'cost_bps':e.get('cost_bps'),
            'basis':'同窗配对并按交易日等权看改进。缺价保持未知，影子信号在报告签发前冻结。'}


def detail(identifier):
    with store.db(readonly=True) as conn:
        value = next((e for e in experiments(conn) if e['id'] == identifier), None)
        if not value:
            raise HTTPException(404, '实验不存在')
        return {'experiment': value, 'evaluation': value.get('release_evaluation') or evaluate(value, conn)}


def _publish(conn, value, evaluation, current, trigger):
    store.set_setting('research_previous_method', current, conn=conn)
    store.set_setting(ACTIVE_KEY,{**current,'version':f"method-{value['id']}",value.get('factor','relative_volume_min'):value['threshold']},conn=conn)
    value.update(state='published',released_at=store.now(),release_evaluation=evaluation,release_trigger=trigger)


def change(identifier, action, confirm):
    if confirm is not True or action not in ('publish', 'reject','pause','resume'):
        raise HTTPException(422, '请明确确认实验处理方式')
    with store.db() as conn:
        items = _locked_experiments(conn)
        value = next((e for e in items if e['id'] == identifier and e['state'] in ('shadow','paused')), None)
        if not value:
            raise HTTPException(409, '实验不存在或已经处理')
        if action == 'publish':
            current = active(conn)
            evaluation = evaluate(value, conn)
            if value['state']!='shadow' or current['version'] != value['baseline']['version'] or not evaluation['qualified']:
                raise HTTPException(409, '基线已变化或时间外与前瞻影子证据尚未达到发布门槛')
            _publish(conn,value,evaluation,current,'manual')
        elif action == 'reject':
            value.update(state='rejected',released_at=store.now(),release_evaluation=evaluate(value,conn))
        elif action == 'pause' and value['state']=='shadow':
            pauses = value.setdefault('pauses',[])
            if len(pauses)>=20:
                raise HTTPException(409,'暂停记录已达上限')
            pauses.append({'from':store.now()})
            value['state']='paused'
        elif action == 'resume' and value['state']=='paused':
            if sum(e['state']=='shadow' for e in items)>=3 or active(conn)['version']!=value['baseline']['version']:
                raise HTTPException(409,'影子窗口已满或实验基线已变化')
            value['pauses'][-1]['to']=store.now()
            value['state']='shadow'
        else:
            raise HTTPException(409,'实验状态与操作不符')
        store.set_setting(EXPERIMENT_KEY, items, conn=conn)
        store.audit('research_experiment_decision', {'id': identifier, 'action': action}, conn=conn)
    return list_status()


def auto_publish():
    if not any(e.get('auto_publish') and e['state']=='shadow' for e in experiments()):
        return
    with store.db() as conn:
        items = _locked_experiments(conn)
        current = active(conn)
        for value in reversed(items):
            if value['state']!='shadow' or not value.get('auto_publish') or value['baseline']['version']!=current['version']:
                continue
            evaluation=evaluate(value,conn)
            if evaluation['qualified']:
                _publish(conn,value,evaluation,current,'preauthorized')
                store.set_setting(EXPERIMENT_KEY,items,conn=conn)
                store.audit('research_experiment_decision',{'id':value['id'],'action':'auto_publish'},conn=conn)
                break


def rollback(confirm):
    if confirm is not True:
        raise HTTPException(422, '请确认回退方法')
    with store.db() as conn:
        _locked_experiments(conn)
        previous = store.setting('research_previous_method', None, conn=conn)
        if not previous:
            raise HTTPException(409, '没有可回退的上一版本')
        store.set_setting(ACTIVE_KEY, previous, conn=conn)
        store.set_setting('research_previous_method', None, conn=conn)
        store.audit('research_method_rollback', {'version': previous['version']}, conn=conn)
    return list_status()
