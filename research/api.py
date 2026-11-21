from fastapi import APIRouter,Query,HTTPException
from foundation import assets, collection, deployment, database as store
import os
from research import calibration, counterparty, coverage, delivery, discovery, engine, evidence, feedback, learning, ledger, market, methods, model_transport, multidim, originals, outlook, planning, portfolio, preferences, preparation, reassessment, recaps, review, rises, screening, sessions, state_store, stock_library, structure, usage

router=APIRouter(prefix='/api/v2')
RISE_TYPE_PATTERN = '^('+'|'.join(rises.TYPES)+')$'

def instance_status():
    enabled = bool(deployment.workers_enabled())
    alive = lambda thread: bool(thread and thread.is_alive())
    return {'name': deployment.settings().get('name', '序研'),
            'workers_enabled': enabled, 'automatic_executor': deployment.settings().get('name', '序研'),
            'shared_schedules': False,
            'collection_clock': enabled and alive(collection.engine.clock),
            'research_clock': enabled and alive(engine.engine.clock)}


def research_schedule_status(value):
    instance = instance_status()
    return {**value, 'instance': instance,
            'effective_enabled': list(value['config']['enabled']) if instance['research_clock'] else []}


def recap_schedule_status(value):
    instance = instance_status()
    return {kind: {**plan, 'effective_enabled': bool(plan['enabled'] and instance['research_clock'])}
            for kind, plan in value.items()} | {'instance': instance}


@router.get('/research')
def dashboard(session: str|None=Query(None,pattern='^(noon|tail|close)$'), workbench: bool=False):
    runs=engine.list_runs(8,session=session)
    now=store.now()
    with store.db(readonly=True) as c:
        clause = " AND session=%s" if session else ''
        latest=c.execute("SELECT id,market_day,published_at FROM research_runs WHERE report IS NOT NULL AND withdrawn_at IS NULL"+
                         clause+" ORDER BY created DESC,id DESC LIMIT 1",(session,) if session else ()).fetchone()
        active=c.execute("SELECT id FROM research_runs WHERE status IN ('queued','running','waiting') LIMIT 1").fetchone()
        day=c.execute("SELECT max(day) FROM heads WHERE dataset='daily'").fetchone()[0] if not workbench else None
    report=None
    if latest:
        report={**dict(latest),'published_at':state_store.iso_time(latest['published_at'])} if workbench else stock_library.presentation(engine.get_run(latest[0]))
    current=next((row for row in runs['items'] if not row['withdrawn_at'] and
                  (not workbench or row['market_day']==now[:10])),None)
    attempt=None
    if current:
        attempt=(report if not workbench and report and report['id']==current['id'] else
                 stock_library.presentation(engine.get_run(current['id']),panels=workbench))
    running=None
    if active:
        running=attempt if attempt and attempt['id']==active[0] else engine.get_run(active[0])
        if workbench:
            running={key:running[key] for key in ('id','status','stage','market_day')}|{
                'data':{key:running['data'].get(key) for key in ('session','trigger','research_plan','tool_receipt')}}
    metrics = {} if workbench else {'performance':ledger.performance(),'usage':usage.summary()}
    return {'latest':report,'attempt':attempt,'active':running,'runs':runs,'latest_daily':day,
      'catalog':methods.catalog(),**metrics,'schedule':research_schedule_status(sessions.status()),'instance':instance_status(),'now':now,
      'features':{'originals':True,'portfolio':True,'feedback':True,'controls':True,'structure':True,'notices':True,'research_plan':True},'test_mode':os.environ.get('STOCK_UI_ACCEPTANCE')=='1'}

@router.get('/usage')
def usage_summary(run_id:str|None=Query(None,max_length=16)):
    return usage.summary(run_id)

@router.get('/usage/calls')
def usage_calls(limit:int=Query(25,ge=1,le=50),before:str|None=Query(None,max_length=16),run_id:str|None=Query(None,max_length=16)):
    return usage.calls(limit,before,run_id)

@router.get('/research/plan')
def plan():
    try:
        p=model_transport.freeze_plan()
        p['unresolved_requests']=engine.unresolved_count()
        return p
    except model_transport.ModelFailure as exc:raise HTTPException(409,str(exc)) from None

@router.post('/research/intent')
def intent_plan(raw: dict):
    profile = preferences.current() if raw.get('use_preferences',True) is True else {}
    result = planning.plan(raw,profile)
    window = sessions.choose(result['session'],result['market_day'])
    return {**result,'window':window,'tools':{name:planning.TOOLS[name] for name in result['tool_names']}}

@router.get('/opportunities')
def opportunities(session: str=Query('close',pattern='^(noon|tail|close)$'), day: str|None=Query(None,max_length=10)):
    slot=sessions.choose(session,assets.day(day) if day else None)
    bundle = discovery.snapshot(slot['market_day'],slot=slot,profile=preferences.current(),method=learning.active())
    bundle.pop('scope_pack',None)
    bundle.pop('screening_rows',None)
    bundle.pop('rise_preview',None)
    with store.db(readonly=True) as conn:
        hidden = stock_library.retired(conn,[c['symbol'] for c in bundle['candidates']])
    bundle['candidates'] = [c for c in bundle['candidates'] if c['symbol'] not in hidden]
    return bundle


@router.get('/opportunities/rises')
def opportunity_rises(session: str=Query('close',pattern='^(noon|tail|close)$'),
                      day: str|None=Query(None,max_length=10), rise_type: str|None=Query(None,pattern=RISE_TYPE_PATTERN),
                      q: str=Query('',max_length=40), limit: int=Query(25,ge=1,le=50),
                      after: int=Query(0,ge=0,le=10000), input_hash: str|None=Query(None,max_length=64)):
    slot=sessions.choose(session,assets.day(day) if day else None)
    bundle=discovery.snapshot(slot['market_day'],slot=slot,profile=preferences.current(),method=learning.active())
    return screening.rise_preview(bundle,rise_type,q,limit,after,input_hash)


@router.get('/opportunities/{symbol}/analysis')
def opportunity_analysis(symbol: str, session: str=Query('close',pattern='^(noon|tail|close)$'),
                         day: str|None=Query(None,max_length=10)):
    symbol = assets.symbol(symbol)
    _require_opportunity(symbol)
    slot = sessions.choose(session,assets.day(day) if day else None)
    profile = {**preferences.current(),'candidate_limit':1,'boards':[],'excluded_symbols':[]}
    bundle = discovery.snapshot(slot['market_day'],[symbol],slot=slot,profile=profile,
                                method=learning.active(),market_context=True,outlook_version=outlook.VERSION)
    if not bundle['candidates']:
        raise HTTPException(409,'这只股票缺有效截点行情或足够的历史日线，暂不能生成多维分析')
    candidate = bundle['candidates'][0]
    return {'symbol':symbol,'name':candidate['name'],'market_day':slot['market_day'],'session':session,
            'cutoff':bundle['market_data_cutoff'],'knowledge_cutoff':bundle['knowledge_cutoff'],'mode':bundle['mode'],
            'rise':candidate['metrics'].get('rise'),'rise_policy':bundle['rise_policy'],
            'outlook':candidate['outlook_reference'],'outlook_source':'local',
            'counterparty_reference':counterparty.reference(candidate),
            'analysis':multidim.build(bundle['market'],candidate,pointed=True)}


@router.get('/methods')
def method_catalog():return methods.catalog()

@router.get('/method-calibration')
def method_calibration(horizon: str = Query('D3',pattern='^(D1|D3)$')):
    return calibration.evaluate(learning.active()['version'],horizon)

@router.post('/research/runs')
def create_run(raw:dict):return engine.submit(raw)

@router.get('/research/runs')
def runs(limit:int=Query(20,ge=1,le=50),before:str|None=Query(None,max_length=16),
         session: str|None=Query(None,pattern='^(noon|tail|close)$'),
         outcome: str|None=Query(None,pattern='^(published|unpublished)$'),
         start: str|None=Query(None,max_length=10), end: str|None=Query(None,max_length=10)):
    return engine.list_runs(limit,before,session=session,outcome=outcome,start=start,end=end)

@router.get('/research/runs/{run_id}')
def run_detail(run_id:str, progress:bool=False):
    if progress:
        with store.db(readonly=True) as conn:
            row=conn.execute(
                "SELECT id,status,stage,market_day,"
                "session,published_at,withdrawn_at,has_bundle "
                "FROM research_runs WHERE id=%s",(run_id,),
            ).fetchone()
        if not row:
            raise HTTPException(404,'研究任务不存在')
        return {**dict(row),'published_at':state_store.iso_time(row['published_at']),
                'withdrawn_at':state_store.iso_time(row['withdrawn_at'])}
    return stock_library.presentation(engine.get_run(run_id))

@router.get('/research/runs/{run_id}/screening')
def run_screening(run_id: str, stage: str = Query('all',pattern='^(all|selected|not_selected)$'),
                  path: str | None = Query(None,pattern='^O0[1-8]$'), q: str = Query('',max_length=40),
                  limit: int = Query(25,ge=1,le=50), after: int = Query(0,ge=0,le=10000),
                  rise_type: str | None = Query(None,pattern=RISE_TYPE_PATTERN)):
    return screening.page(engine.get_run(run_id),stage,path,q,limit,after,rise_type)
@router.get('/research/runs/{run_id}/coverage')
def run_coverage(run_id: str, state: str = Query(...,max_length=30), limit: int = Query(25,ge=1,le=50),
                 offset: int = Query(0,ge=0,le=10000)):
    run = engine.get_run(run_id)
    candidates = run['data'].get('bundle',{}).get('candidates',[])
    with store.db(readonly=True) as conn:
        blocked = {c['symbol'] for c in candidates}-stock_library.visible(run,conn)
    return coverage.page(run_id,state,limit,offset,blocked)

@router.post('/research/runs/{run_id}/corrections')
def correct_run(run_id: str, raw: dict):
    return ledger.correct(run_id, raw)

@router.get('/research-notices')
def research_notices(limit: int = Query(20,ge=1,le=50), before: int | None = Query(None,ge=1)):
    return delivery.page(limit,before)

@router.post('/research-notices/{identifier}/read')
def read_notice(identifier: int):
    return delivery.mark_read(identifier)

@router.post('/research-notices/{identifier}/send')
def send_notice(identifier: int, raw: dict):
    return delivery.send(identifier,raw.get('confirm'),raw.get('acknowledge_unknown',False))

@router.get('/pending-reassessments')
def pending_reassessments():
    stamp = store.now()
    with store.db(readonly=True) as conn:
        items = reassessment.active_cycles(conn,stamp,stamp,limit=8,require_alert=True)
    return {'items':[{'id':v['id'],'symbol':v['symbol'],'name':v['data']['name'],'flags':v['flags']} for v in items]}

@router.get('/research-preparation')
def preparation_options():
    return {'items':preparation.options()}

@router.post('/research-preparation')
def prepare_research(raw: dict):
    return preparation.start(raw)

@router.get('/research/runs/{run_id}/stocks/{symbol}/bars')
def stock_bars(run_id:str,symbol:str):
    run=engine.get_run(run_id)
    bundle=run['data'].get('bundle',{})
    candidate=next((c for c in bundle.get('candidates',[]) if c['symbol']==symbol),None)
    if not candidate:raise HTTPException(404,'证券不在本轮冻结范围')
    frozen = candidate.get('outlook_reference')
    with store.db(readonly=True) as conn:
        stock_library.require_visible(run,symbol,conn)
        bars = market.frozen_bars(symbol,candidate.get('bar_ids',candidate['evidence_ids']),65,conn)
        reference = frozen
        if not reference:
            dates = outlook.trading_dates(conn,bars[0]['day'],bars[-1]['day']) if bars else []
            reference = outlook.reference(bars,dates,candidate['metrics'],candidate.get('structure_basis',{}).get('action_days',()))
    return {'items':bars[-60:],
            'basis':'读取本轮冻结的事实版本，盘中最后一柱是截点快照',
            'outlook':reference,'outlook_source':'sent' if frozen else 'archive_calculation',
            'counterparty_reference':counterparty.reference(candidate),
            'analysis':multidim.build(bundle.get('market',{}),candidate,pointed=bool(run['data'].get('symbols')))}

@router.get('/research/runs/{run_id}/stocks/{symbol}/originals')
def stock_originals(run_id:str,symbol:str):
    run=engine.get_run(run_id)
    candidate=next((c for c in run['data'].get('bundle',{}).get('candidates',[]) if c['symbol']==symbol),None)
    if not candidate:raise HTTPException(404,'该证券不在冻结研究中')
    with store.db(readonly=True) as conn:
        stock_library.require_visible(run,symbol,conn)
        return originals.attach_quotes(conn,candidate.get('originals',{}))

def _structure_replay(bundle, symbol, box, reversal, step):
    candidate = next((c for c in bundle.get('candidates', []) if c['symbol'] == symbol), None)
    if not candidate:
        raise HTTPException(404, '该证券不在本轮冻结范围')
    ids = candidate.get('bar_ids', candidate['evidence_ids'])
    bars = market.frozen_bars(symbol, ids, structure.WINDOW)
    if not bars or candidate.get('bar_ids') and [b['id'] for b in bars] != ids:
        raise HTTPException(409, '部分冻结行情已超出保留期限，无法重算完整结构')
    basis = candidate.get('structure_basis', {})
    ratio = candidate['metrics']['relative_volume20']
    if box is not None:
        if step is not None and step > len(bars):
            raise HTTPException(422, '回放位置越过冻结行情')
        visible = bars[:step]
        study = structure.analyze(visible, partial_ratio=ratio, frames=len(visible) < len(bars), **basis)
        return {'point_figure': structure.point_figure(visible, study.get('summary', study), box, reversal)}
    replay = structure.analyze(bars, frames=True, partial_ratio=ratio, **basis)
    return {'bars': bars, 'replay': replay, 'mode': bundle['mode'], 'frozen_at': bundle['knowledge_cutoff']}


@router.get('/research/runs/{run_id}/stocks/{symbol}/structure')
def run_structure(run_id: str, symbol: str, box: float | None = Query(None, ge=.01, le=10000),
                  reversal: int = Query(3, ge=1, le=5), step: int | None = Query(None, ge=1, le=structure.WINDOW)):
    run = engine.get_run(run_id)
    symbol = assets.symbol(symbol)
    with store.db(readonly=True) as conn:
        stock_library.require_visible(run,symbol,conn)
    return _structure_replay(run['data'].get('bundle', {}), symbol, box, reversal, step)


def _require_opportunity(symbol):
    with store.db(readonly=True) as conn:
        if stock_library.retired(conn,[symbol]):
            raise HTTPException(404,'这只股票已退出展示')


@router.get('/opportunities/{symbol}/structure')
def opportunity_structure(symbol: str, session: str = Query('close', pattern='^(noon|tail|close)$'),
                          day: str | None = Query(None, max_length=10), box: float | None = Query(None, ge=.01, le=10000),
                          reversal: int = Query(3, ge=1, le=5), input_hash: str | None = Query(None, max_length=64),
                          step: int | None = Query(None, ge=1, le=structure.WINDOW)):
    symbol = assets.symbol(symbol)
    _require_opportunity(symbol)
    slot = sessions.choose(session, assets.day(day) if day else None)
    bundle = discovery.snapshot(slot['market_day'], slot=slot, profile=preferences.current(), method=learning.active())
    if input_hash and bundle['input_hash'] != input_hash:
        raise HTTPException(409, '预览行情已更新，请刷新研究列表后重试')
    return _structure_replay(bundle, symbol, box, reversal, step)

@router.post('/research/runs/{run_id}/stop')
def stop_run(run_id:str):return engine.stop(run_id)

@router.post('/research/runs/{run_id}/reconcile')
def reconcile_run(run_id:str):return engine.reconcile(run_id)

@router.get('/cycles')
def cycles(limit:int=Query(25,ge=1,le=50),before:str|None=Query(None,max_length=16),
           rise_type:str|None=Query(None,pattern=RISE_TYPE_PATTERN)):
    return ledger.list_cycles(limit,before,rise_type)

@router.get('/stock-library')
def library(limit:int=Query(25,ge=1,le=25),before:str|None=Query(None,max_length=40),
            after:str|None=Query(None,max_length=40),pool:str|None=Query(None,pattern='^(buy|watch)$'),
            start:str|None=Query(None,max_length=10),end:str|None=Query(None,max_length=10),
            state:str|None=Query(None,pattern='^('+'|'.join(stock_library.STATES)+')$'),
            stars:str|None=Query(None,pattern='^([1-5]|unrated)$'),
            rise_type:str|None=Query(None,pattern=RISE_TYPE_PATTERN),
            session:str|None=Query(None,pattern='^(noon|tail|close)$'),
            cycle_session:str|None=Query(None,pattern='^(noon|tail|close)$'),
            run_id:str|None=Query(None,pattern='^[0-9a-f]{16}$'),
            q:str=Query('',max_length=40),symbol:str|None=Query(None,max_length=16)):
    return stock_library.page(limit,before,after=after,pool=pool,start=start,end=end,state=state,stars=stars,rise_type=rise_type,
                              query=q,symbol=symbol,session=session,cycle_session=cycle_session,run_id=run_id)


@router.post('/stock-library/{symbol}/remove')
def remove_stock(symbol:str,raw:dict):
    return ledger.remove_from_pools(symbol,raw)


@router.get('/pool-review')
def pool_review(session:str|None=Query(None,pattern='^(noon|tail|close)$')):
    return ledger.pool_review_status(session)


@router.post('/pool-review/refresh')
def refresh_pool_review(session:str|None=Query(None,pattern='^(noon|tail|close)$')):
    ledger.maintain_pools(session)
    return ledger.pool_review_status(session)

@router.get('/cycles/{cycle_id}')
def cycle(cycle_id:str):return ledger.cycle_detail(cycle_id)

@router.post('/cycles/{cycle_id}/reference')
def reference(cycle_id:str,raw:dict):
    fid=raw.get('fact_id')
    if type(fid) is not int or fid<=0:raise HTTPException(422,'报价事实编号无效')
    return ledger.record_reference(cycle_id,fid,raw.get('conditions_confirmed'),raw.get('kind','entry'))

@router.get('/cycles/{cycle_id}/references')
def reference_options(cycle_id:str,kind:str='entry'):
    return ledger.available_reference(cycle_id,kind)

@router.get('/performance')
def performance(rise_type:str|None=Query(None,pattern=RISE_TYPE_PATTERN)):
    return ledger.performance(rise_type)


@router.get('/recap/daily')
def daily_recap(day:str|None=Query(None,max_length=10)):
    return recaps.daily(day)


@router.get('/recap/daily/news')
def recap_news(day:str=Query(...,max_length=10),limit:int=Query(8,ge=1,le=20),before:int|None=Query(None,ge=1)):
    return recaps.news(day,limit,before)


@router.get('/recap/monthly')
def monthly_recap(month:str|None=Query(None,pattern='^20[0-9]{2}-(0[1-9]|1[0-2])$')):
    return recaps.monthly(month)


@router.get('/recap/schedule')
def recap_schedule():
    return recap_schedule_status(recaps.schedule_status())


@router.put('/recap/schedule')
def update_recap_schedule(raw:dict):
    return recap_schedule_status(recaps.save_schedule(raw))


@router.post('/performance/refresh')
def refresh_performance():
    ledger.refresh_outcomes()
    return ledger.performance()


@router.get('/research-schedule')
def schedule():
    return research_schedule_status(sessions.status())


@router.put('/research-schedule')
def update_schedule(raw:dict):
    return research_schedule_status(sessions.save(raw))


@router.get('/research-preferences')
def profile():
    return preferences.current()


@router.put('/research-preferences')
def update_profile(raw:dict):
    return preferences.save(raw)


@router.get('/research-checks/{symbol}')
def checks(symbol:str):
    with store.db(readonly=True) as conn:
        return evidence.read_checks(conn, assets.symbol(symbol), store.now())


@router.post('/research-checks')
def write_check(raw:dict):
    return evidence.save_check(raw)


@router.get('/method-experiments')
def experiments():
    return learning.list_status()


@router.post('/method-experiments')
def create_experiment(raw:dict):
    return learning.create(raw)


@router.get('/method-experiments/{identifier}')
def experiment(identifier:str):
    return learning.detail(identifier)


@router.post('/method-experiments/{identifier}/decision')
def experiment_decision(identifier:str, raw:dict):
    return learning.change(identifier, raw.get('action'), raw.get('confirm'))


@router.post('/method-rollback')
def rollback(raw:dict):
    return learning.rollback(raw.get('confirm'))


@router.get('/policy-originals')
def policy_originals(limit:int=Query(25,ge=1,le=50),before:int|None=Query(None,ge=1)):
    return originals.list_policies(limit,before)


@router.post('/policy-originals')
def import_policy_original(raw:dict):
    return originals.import_policy(raw)


@router.get('/review-controls')
def control_samples(limit:int=Query(25,ge=1,le=50),before:int|None=Query(None,ge=1)):
    return {**review.page(limit,before),'summary':review.summary()}


@router.get('/portfolio/{mode}/positions')
def portfolio_positions(mode:str,limit:int=Query(25,ge=1,le=50),after:str=Query('',max_length=12)):
    return {**portfolio.positions(mode,limit,after),'test_mode':os.environ.get('STOCK_UI_ACCEPTANCE')=='1'}


@router.get('/portfolio/{mode}/fills')
def portfolio_fills(mode:str,limit:int=Query(25,ge=1,le=50),before:int|None=Query(None,ge=1)):
    return portfolio.fills(mode,limit,before)


@router.post('/portfolio/{mode}/fills')
def record_portfolio_fills(mode:str,raw:dict):
    return portfolio.record(mode,raw.get('items'))


@router.post('/portfolio/{mode}/import')
def import_portfolio_fills(mode:str,raw:dict):
    return portfolio.import_csv(mode,raw.get('csv'))


@router.post('/portfolio/{mode}/fills/{identifier}/void')
def void_portfolio_fill(mode:str,identifier:int,raw:dict):
    return portfolio.void(mode,identifier,raw.get('note'))


@router.get('/portfolio/{mode}/symbols/{symbol}')
def portfolio_symbol(mode:str,symbol:str):
    return portfolio.symbol_summary(mode,symbol)


@router.get('/research-feedback')
def feedback_status():
    return feedback.status()


@router.post('/research-feedback')
def feedback_proposal(raw:dict):
    return feedback.propose(raw)


@router.post('/research-feedback/{identifier}')
def feedback_decision(identifier:str,raw:dict):
    return feedback.decide(identifier,raw.get('action'),raw.get('confirm'))
