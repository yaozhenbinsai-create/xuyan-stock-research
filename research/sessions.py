"""Trading-session windows and opt-in research scheduling."""
from __future__ import annotations

from datetime import datetime, timedelta
import json

from fastapi import HTTPException

from foundation import assets, database as store
from foundation import directory as security_directory


VERSION = 'session-windows-v3'
TARGET_VERSION = 'session-targets-v1'
SESSIONS = {
    'noon': {'name': '午盘', 'cutoff': '11:30', 'start': '11:31', 'launch_until': '12:35', 'publish_until': '12:50',
             'goal':'寻找当日下午可能上涨的股票','horizon':'PM','evaluation':'午后 13:00–13:05 可核报价至当日收盘'},
    'tail': {'name': '尾盘', 'cutoff': '14:35', 'start': '14:36', 'launch_until': '14:45', 'publish_until': '14:45',
             'goal':'寻找下一交易日可能上涨的股票','horizon':'D1','evaluation':'研究日正式收盘至下一交易日收盘'},
    'close': {'name': '收盘', 'cutoff': '15:00', 'start': '15:30', 'launch_until': '20:30', 'publish_until': '09:30',
              'goal':'寻找未来 1–3 个交易日看好的超短线股票','horizon':'D3','evaluation':'研究日正式收盘至后第 1、3 个交易日收盘'},
}
DEFAULTS = {'enabled': [], 'symbols': [], 'quote_age_seconds': 300, 'min_coverage': 0.8}
CONFIG_KEY = 'research_session_policy'
STATE_KEY = 'research_session_state'


def local_time(day, hhmm):
    return f'{day}T{hhmm}:00+08:00'


def trading_day(conn, day):
    row = conn.execute(
        "SELECT c.trading FROM heads h JOIN calendar_records c ON c.fact_id=h.fact_id "
        "WHERE h.dataset='calendar' AND h.day=%s AND h.symbol='*' LIMIT 1", (day,),
    ).fetchone()
    return bool(row[0]) if row else None


def next_trading_days(conn, day):
    rows = conn.execute(
        "SELECT h.day,c.trading FROM heads h JOIN calendar_records c ON c.fact_id=h.fact_id "
        "WHERE h.dataset='calendar' AND h.day>%s AND h.symbol='*' ORDER BY h.day LIMIT 31", (day,),
    )
    return [r[0] for r in rows if r[1]]


def next_trading_day(conn, day):
    return next(iter(next_trading_days(conn,day)),None)


def final_day(endpoints):
    return max((day for day in endpoints.values() if day),default=None)


def previous_trading_day(conn, day):
    rows = conn.execute(
        "SELECT h.day,c.trading FROM heads h JOIN calendar_records c ON c.fact_id=h.fact_id "
        "WHERE h.dataset='calendar' AND h.day<%s AND h.symbol='*' ORDER BY h.day DESC LIMIT 31", (day,),
    )
    return next((r[0] for r in rows if r[1]), None)


def config(conn=None):
    saved = store.setting(CONFIG_KEY, {}, conn=conn)
    saved.pop('max_requests', None)
    return {**DEFAULTS, **saved}


def window(session, day, conn, *, now=None, settings=None):
    if session not in SESSIONS:
        raise HTTPException(422, '研究时段须为午盘、尾盘或收盘')
    spec = SESSIONS[session]
    settings = config(conn) if settings is None else settings
    now = now or store.now()
    days = next_trading_days(conn,day) if session!='noon' else []
    d1 = days[0] if days else None
    d3 = days[2] if len(days)>=3 else None
    following = d1 if session=='close' else day
    ends = {'PM':day} if session=='noon' else {'D1':d1} if session=='tail' else {'D1':d1,'D3':d3}
    cutoff = local_time(day, spec['cutoff'])
    opening = local_time(day, spec['start'])
    deadline = local_time(following, spec['publish_until']) if following else None
    historical = day < now[:10] or bool(deadline and now >= deadline)
    # A close review after the original market day can still be timely before next open.
    if session == 'close' and deadline and opening <= now < deadline:
        historical = False
    return {
        'version': VERSION, 'session': session, 'label': spec['name'], 'market_day': day,
        'target':{'version':TARGET_VERSION,'goal':spec['goal'],'evaluation':spec['evaluation'],
                  'primary':spec['horizon'],'horizons':ends},
        'market_data_cutoff': cutoff, 'opens_at': opening,
        'launch_until': local_time(day, spec['launch_until']), 'publish_until': deadline,
        'action_valid_until': local_time(following, '14:55') if following else None,
        'reference_from': local_time(day, '13:00') if session == 'noon' else opening,
        'quote_age_seconds': settings['quote_age_seconds'], 'min_coverage': settings['min_coverage'],
        'retrospective': historical, 'trading_day': trading_day(conn, day),
    }


def choose(session, day=None, *, now=None):
    now = now or store.now()
    with store.db(readonly=True) as conn:
        if day is None and session == 'close':
            latest = conn.execute("SELECT MAX(day) FROM heads WHERE dataset='daily' AND day<=%s", (now[:10],)).fetchone()[0]
            day = latest or now[:10]
        value = window(session, day or now[:10], conn, now=now)
    if value['market_day'] > now[:10] or now < value['market_data_cutoff']:
        raise HTTPException(409, f"{value['label']}截点尚未到达，行情截止 {value['market_data_cutoff'][11:16]}")
    if session != 'close' and value['trading_day'] is not True:
        raise HTTPException(409, '该日期缺少已确认的交易日历')
    return value


def save(raw):
    if set(raw) - {'enabled', 'symbols', 'quote_age_seconds', 'min_coverage', 'confirm', 'acknowledge_unknown'}:
        raise HTTPException(422, '时段设置包含未知字段')
    enabled = raw.get('enabled', [])
    symbols = raw.get('symbols', [])
    if not isinstance(enabled, list) or any(not isinstance(x, str) or x not in SESSIONS for x in enabled):
        raise HTTPException(422, '自动研究时段无效')
    if not isinstance(symbols, list) or len(symbols) > 8:
        raise HTTPException(422, '指定范围最多8只证券，留空使用有证据的范围')
    seconds = raw.get('quote_age_seconds', DEFAULTS['quote_age_seconds'])
    coverage = raw.get('min_coverage', DEFAULTS['min_coverage'])
    if type(seconds) is not int or not 30 <= seconds <= 900:
        raise HTTPException(422, '报价新鲜度须在30至900秒之间')
    if type(coverage) not in (int, float) or not 0.5 <= coverage <= 1:
        raise HTTPException(422, '自动研究覆盖率须在50%至100%之间')
    if enabled and raw.get('confirm') is not True:
        raise HTTPException(422, '请确认启用所选自动研究时段')
    with store.db() as conn:
        value = {
            'enabled': list(dict.fromkeys(enabled)), 'symbols': list(dict.fromkeys(assets.symbol(x) for x in symbols)),
            'quote_age_seconds': seconds, 'min_coverage': coverage,
            'confirmed_at': store.now() if enabled else None,
            'acknowledged_through': store.now() if enabled and raw.get('acknowledge_unknown') is True else None,
        }
        store.set_setting(CONFIG_KEY, value, conn=conn)
    return status()


def status():
    now = store.now()
    with store.db(readonly=True) as conn:
        saved = config(conn)
        slots = [window(s, now[:10], conn, now=now, settings=saved) for s in SESSIONS]
        log = store.setting(STATE_KEY, {}, conn=conn)
        order = {name: index for index, name in enumerate(SESSIONS)}
        recent = sorted(log.values(), key=lambda row: (row['day'], order.get(row['session'], -1)), reverse=True)[:15]
        run_ids = [row['run_id'] for row in recent if row.get('run_id')]
        job_ids = [row['collection_job'] for row in recent if row.get('collection_job')]
        runs = {}
        if run_ids:
            rows = conn.execute("SELECT id,status,stage,market_coverage coverage "
                                "FROM research_runs WHERE id IN ("+','.join(['%s']*len(run_ids))+")",tuple(run_ids))
            runs = {row['id']:{'status':row['status'],'stage':row['stage'],
                               'coverage':float(row['coverage']) if row['coverage'] is not None else None} for row in rows}
        jobs = {}
        if job_ids:
            rows = conn.execute("SELECT id,status,done,total FROM collection_jobs WHERE id IN ("+
                                ','.join(['%s']*len(job_ids))+")",tuple(job_ids))
            jobs = {row['id']:dict(row) for row in rows}
    return {'now': now, 'version': VERSION, 'config': saved, 'windows': slots,
            'recent': [{**row,'result':runs.get(row.get('run_id')),'capture':jobs.get(row.get('collection_job'))} for row in recent]}


def remember_slot(day, session, state, detail, run_id=None):
    with store.db() as conn:
        before = (datetime.fromisoformat(day) - timedelta(days=14)).date().isoformat()
        log = {k: v for k, v in store.setting(STATE_KEY, {}, conn=conn).items() if v['day'] >= before}
        key = f'{day}/{session}'
        prior = log.get(key, {})
        log[key] = {**prior, 'day': day, 'session': session, 'state': state, 'detail': detail,
                    'run_id': None if state == 'capturing' else run_id}
        if state == 'capturing':
            log[key]['collection_job'] = run_id
        store.set_setting(STATE_KEY, log, conn=conn)


def due(now=None):
    """The caller owns dispatch. Expired slots are recorded without model requests."""
    now = now or store.now()
    day = now[:10]
    with store.db(readonly=True) as conn:
        settings = config(conn)
        if not settings['enabled'] or not settings.get('confirmed_at'):
            return []
        log = store.setting(STATE_KEY, {}, conn=conn)
        earliest = max(settings['confirmed_at'][:10], (datetime.fromisoformat(day)-timedelta(days=7)).date().isoformat())
        calendar = conn.execute("SELECT h.day,c.trading FROM heads h JOIN calendar_records c ON c.fact_id=h.fact_id "
                                "WHERE h.dataset='calendar' AND h.symbol='*' AND h.day BETWEEN %s AND %s ORDER BY h.day LIMIT 8", (earliest,day))
        result = []
        for market_day, trading in calendar:
            if not trading:
                continue
            for session in settings['enabled']:
                if log.get(f'{market_day}/{session}', {}).get('state') not in (None,'capturing','capture_unavailable'):
                    continue
                value = window(session, market_day, conn, now=now, settings=settings)
                if value['opens_at'] <= now and settings['confirmed_at'] < value['launch_until']:
                    result.append((value, settings))
        return result


def captures(now):
    day = now[:10]
    with store.db(readonly=True) as conn:
        settings = config(conn)
        if not settings['enabled'] or trading_day(conn, day) is not True:
            return []
        if store.setting('automation_paused', True, conn=conn):
            return []
        log = store.setting(STATE_KEY, {}, conn=conn)
        symbols = settings['symbols']
        age = settings['quote_age_seconds']
        lead = capture_lead(age,bool(symbols))
        eligible = []
        for session in settings['enabled']:
            cutoff = local_time(day, SESSIONS[session]['cutoff'])
            opens = (datetime.fromisoformat(cutoff) - timedelta(seconds=lead)).isoformat()
            if opens <= now < cutoff and f'{day}/{session}' not in log:
                eligible.append(session)
        if not eligible:
            return []
        if not symbols:
            _, roster = security_directory.market_roster(conn,day,known_at=now,limit=6000)
            symbols = sorted(s for s, info in roster.items() if info.get('status') == '1')
            if not symbols:
                return []
        return [(day, session, symbols) for session in eligible]


def capture_lead(age,pointed=False):
    # Whole-market collection previously started exactly on the age boundary.
    # A vendor timestamp lag of a few seconds then invalidated the first batch.
    # Keep the saved quote-age rule and reserve time for that observed lag.
    return min(age,60) if pointed else max(1,age-min(60,max(5,age//5)))


def remaining_seconds(value):
    return (datetime.fromisoformat(value) - datetime.fromisoformat(store.now())).total_seconds() if value else None
