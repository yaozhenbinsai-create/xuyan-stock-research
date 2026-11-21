"""Point-in-time market recaps and reviews by the original forecast window."""
from __future__ import annotations

from datetime import date,datetime,timedelta
import json
from math import prod
import re

from fastapi import HTTPException

from foundation import assets, collection, database as store, record_store
from foundation import directory as security_directory
from research import ledger, pool_market, sessions, stock_library, state_store


PRICES = """WITH prices AS (
    SELECT h.day,h.symbol,q.close/q.preclose-1 move,q.amount
      FROM heads h JOIN daily_bars q ON q.fact_id=h.fact_id
     WHERE h.dataset='daily' AND h.day BETWEEN %s AND %s
       AND q.close>0 AND q.preclose>0 AND COALESCE(q.suspended,0)=0
) """
_STATS = ("COUNT(*) sample,SUM(move>0) up,SUM(move<0) down,SUM(move=0) flat,"
          "AVG(move) move,SUM(amount) amount")
_NEWS_TYPES = (
    ('业绩改善','业绩预增|预盈|扭亏为盈'),
    ('订单或项目','中标|签订.{0,12}重大合同|签署.{0,12}重大合同'),
    ('回购或增持','回购.{0,12}(方案|预案)|增持.{0,12}(计划|公告)'),
    ('审批进展','获批|取得.{0,12}批复|收到.{0,12}批准|获得.{0,12}批复'),
    ('风险解除','撤销.{0,12}风险警示|解除.{0,12}风险警示'),
)
_NEWS_POSITIVE = '|'.join(expr for _,expr in _NEWS_TYPES)
_NEWS_EXCLUDE = '终止|取消|提示性|不予|未获|问询|风险提示|未中标|实施进展|进展暨|完成公告'
_SCHEDULE = 'recap_schedule'
_TIMES = {'daily':'19:00', 'monthly':'19:30'}


def _archive_key(kind, period):
    return f'recap-{kind}:{period}'


def schedule_status():
    with store.db(readonly=True) as conn:
        policy=store.setting(_SCHEDULE,{},conn=conn)
        result={}
        for kind, at in _TIMES.items():
            prefix=_archive_key(kind,'')
            row=conn.execute('SELECT `key` FROM settings WHERE `key` LIKE %s ORDER BY `key` DESC LIMIT 1',
                             (prefix+'%',)).fetchone()
            result[kind]={'enabled':bool(policy.get(kind)),'at':at,'latest':row[0][len(prefix):] if row else None}
    return result


def save_schedule(raw):
    if set(raw)!=set(_TIMES) or any(type(value) is not bool for value in raw.values()):
        raise HTTPException(422,'每日与每月复盘的启用状态须为布尔值')
    with store.db() as conn:
        conn.execute('INSERT IGNORE INTO settings VALUES(%s,%s)',(_SCHEDULE,'{}'))
        row=conn.execute('SELECT data FROM settings WHERE `key`=%s FOR UPDATE',(_SCHEDULE,)).fetchone()
        previous=json.loads(row[0])
        now=store.now()
        policy={kind:(previous.get(kind) or now) if enabled else None for kind,enabled in raw.items()}
        if policy!=previous:
            store.set_setting(_SCHEDULE,policy,conn=conn)
            store.audit('修改定时复盘',raw,conn=conn)
    return schedule_status()


def _as_floats(row):
    values=dict(row)
    for key in ('sample','up','down','flat'):
        values[key]=int(values[key] or 0)
    for key in ('move','amount'):
        values[key]=float(values[key]) if values[key] is not None else None
    values['breadth']=values['up']/values['sample'] if values['sample'] else None
    return values


def _daily_series(conn,start,end):
    if not end or end<start:
        return []
    rows = [_as_floats(row) for row in conn.execute(
        PRICES+'SELECT day,'+_STATS+",GROUP_CONCAT(DISTINCT LEFT(symbol,2) ORDER BY LEFT(symbol,2) SEPARATOR ',') markets "
        'FROM prices GROUP BY day ORDER BY day LIMIT 31',(start,end))]
    for row in rows:
        row['markets'] = (row.get('markets') or '').split(',') if row.get('markets') else []
    return rows


def _direction(move,breadth,minimum=.55):
    if move>0 and breadth>=minimum:
        return 'strong'
    if move<0 and breadth<=1-minimum:
        return 'weak'
    return 'mixed'


def _market_tomorrow(current,history,next_day):
    if not current or not next_day:
        return {'state':'pending','day':next_day,'title':'等待完整收盘与交易日历'}
    recent=history[-3:]
    votes=[_direction(row['move'],row['breadth']) for row in recent]
    today=votes[-1]
    continued=len(votes)>=3 and today!='mixed' and votes.count(today)>=2
    state=today if continued else 'mixed'
    title={'strong':'偏强情景','weak':'偏弱情景','mixed':'方向待确认'}[state]
    confirm={
        'strong':'明日早盘上涨比例维持在 55% 以上，等权涨跌保持为正，才继续按偏强观察。',
        'weak':'明日早盘上涨比例仍低于 45%，等权涨跌为负，偏弱情景才延续。',
        'mixed':'先看上涨比例是否突破 55% 或跌破 45%，并核对等权涨跌方向是否一致。',
    }[state]
    invalidate={
        'strong':'上涨比例跌回 50% 以下或等权转负，重新判断。',
        'weak':'上涨比例回到 50% 以上且等权转正，重新判断。',
        'mixed':'宽度和等权方向同向扩散后，切换到对应情景。',
    }[state]
    return {'state':state,'day':next_day,'title':title,'samples':len(recent),'confirm':confirm,'invalidate':invalidate,
            'limitation':'仅按样本上涨比例和等权涨跌生成规则情景；未综合主要指数、资金和事件，不代表经验证的次日预测。',
            'reason':f"当日上涨 {current['up']} / {current['sample']} 只，等权涨跌 {current['move']:+.2%}。"
                     f"近 {len(recent)} 个已有日线的交易日有 {votes.count('strong')} 天偏强、{votes.count('weak')} 天偏弱。"
                     +('' if len(recent)>=3 else '同口径近期样本不足，保持观察。')}


def _describe_group(row,market_move):
    row = dict(row)
    row['relative'] = row['move']-market_move
    row['direction'] = _direction(row['move'],row['breadth'],.60)
    row['sample_limited'] = row['sample']<8
    row['relative_direction'] = ('stronger' if row['relative']>=.002 else
                                 'weaker' if row['relative']<=-.002 else 'similar')
    if row['sample_limited']:
        row['observation'] = '样本较少，仅描述当日表现。'
    elif row['direction']=='strong' and row['relative_direction']=='stronger':
        row['observation'] = '观察上涨宽度能否延续。'
    elif row['direction']=='weak' and row['relative_direction']=='weaker':
        row['observation'] = '观察弱势是否收敛。'
    else:
        row['observation'] = '相对表现与当日强弱分开核对，等待后续确认。'
    return row


def _scope_label(market):
    names = {'sh':'沪','sz':'深','bj':'京'}
    markets = (market or {}).get('markets',[])
    return ''.join(names[code] for code in ('sh','sz','bj') if code in markets)+'已存股票样本' if markets else '已存股票样本'


def _groups(conn,day,market_move,dataset):
    field='g_values.`group`' if dataset=='industry' else 'g_values.board'
    excluded=(" AND COALESCE(g_values.unclassified,0)=0 "
              " AND COALESCE(g_values.source_effective_date,%s)<=%s "
              if dataset=='industry' else '')
    params=(day,day,dataset,dataset,day,*((day,day) if dataset=='industry' else ()))
    if dataset=='basics':
        _,roster=security_directory.market_roster(conn,day)
        if not roster:return []
        ids=tuple(v['fact_id'] for v in roster.values())
        group_join=(' FROM prices p JOIN facts g ON g.symbol=p.symbol AND g.id IN ('+','.join(['%s']*len(ids))+') '
                    'JOIN security_records g_record ON g_record.fact_id=g.id '
                    'JOIN security_details g_values ON g_values.id=g_record.security_id ')
        params=(day,day,*ids)
    else:
        group_join=(" FROM prices p JOIN heads hg ON hg.dataset=%s AND hg.symbol=p.symbol "
                    "AND hg.day=(SELECT MAX(day) FROM heads WHERE dataset=%s AND day<=%s) JOIN facts g ON g.id=hg.fact_id "
                    "JOIN classification_records g_record ON g_record.fact_id=g.id "
                    "JOIN classification_values g_values ON g_values.id=g_record.classification_id ")
    rows=conn.execute(
        PRICES+"SELECT "+field+" name,"+_STATS+
        group_join+" WHERE "+field+" IS NOT NULL "+excluded+
        " GROUP BY "+field+" HAVING sample>=5 ORDER BY move DESC,name LIMIT 200",params).fetchall()
    rows=[_as_floats(row) for row in rows if row['name'] and row['name'] not in ('null','未分类','未知')]
    if dataset=='industry' and len(rows)>12:
        rows=rows[:6]+rows[-6:]
    return [_describe_group(row,market_move) for row in rows]


def daily(day=None, *, archived=True):
    now=store.now()
    with store.db(readonly=True,isolation='REPEATABLE READ') as conn:
        latest=pool_market.latest_close_day(conn,now)
        day=assets.day(day) if day else latest or now[:10]
        if day>now[:10]:
            raise HTTPException(422,'请选择今天或更早的市场日')
        market_note=conn.execute(
            "SELECT id,market_analysis text,published_at,mode FROM research_runs WHERE market_day=%s "
            "AND kind='discovery' AND report IS NOT NULL AND withdrawn_at IS NULL "
            "AND session='close' ORDER BY created DESC,id DESC LIMIT 1",
            (day,)).fetchone()
        attempt=conn.execute(
            "SELECT id,market_day,status,stage,created,'close' session,"
            "published_at,withdrawn_at,has_bundle "
            "FROM research_runs WHERE market_day=%s AND kind='discovery' "
            "AND session='close' "
            "ORDER BY created DESC,id DESC LIMIT 1",(day,)).fetchone()
        context={'attempt':dict(attempt) if attempt else None,
                 'note':dict(market_note) if market_note else None}
        for item in context.values():
            if item:
                for field in ('published_at','withdrawn_at'):
                    if field in item:
                        item[field]=state_store.iso_time(item[field])
        common={'day':day,'latest_day':latest,'index_data':False,
                'basis':'仅统计已存有效股票未复权日线；等权涨跌为样本股日涨跌的平均值，不是指数涨跌。',
                'research_note':context['note'],'research_attempt':context['attempt'],
                'research_context':context,'research_context_frozen':False}
        archive=store.setting(_archive_key('daily',day),conn=conn) if archived else None
        if archive:
            result={**common,**archive['summary'],'archive':archive['receipt']}
            frozen=archive['summary'].get('research_context')
            result['research_context_frozen']=frozen is not None
            if frozen is not None:
                result.update(research_note=frozen['note'],research_attempt=frozen['attempt'])
                if frozen!=context:
                    result['current_research_context']=context
            else:
                result['research_context_note']='旧归档未保存关联研究身份；所示研究状态和原稿为当前补充。'
            result['scope_label']=_scope_label(result.get('market'))
            if result.get('market'):
                for key in ('boards','industries'):
                    result[key]=[_describe_group(row,result['market']['move']) for row in result[key]]
            return result
        history=_daily_series(conn,(date.fromisoformat(day)-timedelta(days=12)).isoformat(),min(day,latest) if latest else None)
        current=history[-1] if history and history[-1]['day']==day else None
        next_day=sessions.next_trading_day(conn,day)
        return {**common,'next_day':next_day,'market':current,'scope_label':_scope_label(current),'recent':history[-6:],
                'inference':_market_tomorrow(current,history,next_day),
                'boards':_groups(conn,day,current['move'],'basics') if current else [],
                'industries':_groups(conn,day,current['move'],'industry') if current else [],'archive':None}


def news(day,limit,before=None):
    day=assets.day(day)
    where=(" WHERE h.dataset='announcements' AND h.day=%s "
           "AND a.published BETWEEN %s AND %s "
           "AND a.title REGEXP %s "
           "AND NOT a.title REGEXP %s "
           "AND NOT ("+stock_library.retired_sql('h.symbol')+")")
    now=store.now()
    last=min(day+'T23:59:59+08:00',now)
    args=(day,datetime.fromisoformat(day+'T00:00:00'),
          datetime.fromisoformat(last).astimezone(store.SHANGHAI).replace(tzinfo=None),_NEWS_POSITIVE,_NEWS_EXCLUDE)
    join=" FROM heads h JOIN facts f ON f.id=h.fact_id JOIN announcements a ON a.fact_id=f.id"
    with store.db(readonly=True,isolation='REPEATABLE READ') as conn:
        total=conn.execute("SELECT COUNT(*)"+join+where,args).fetchone()[0]
        if before:
            where+=' AND f.id<%s'
            args+= (before,)
        rows=conn.execute("SELECT f.id,f.symbol,a.title"+join+where+
                          " ORDER BY f.id DESC LIMIT %s",(*args,limit+1)).fetchall()
        displayed=record_store.hydrate(conn,rows[:limit])
        policies=conn.execute("SELECT f.id,p.title "
                              "FROM heads h JOIN facts f ON f.id=h.fact_id JOIN policy_documents p ON p.fact_id=f.id "
                              "WHERE h.dataset='policies' AND h.day=%s "
                              "ORDER BY f.id DESC LIMIT 6",(day,)).fetchall()
    items=[]
    for row in displayed:
        kind=next((name for name,pattern in _NEWS_TYPES if re.search(pattern,row['title'])),'公告线索')
        items.append({key:row[key] for key in ('id','symbol','title')} |
                     {'published':row['payload']['published'],'kind':kind})
    return {'items':items,'policies':[dict(row) for row in policies],'total':total,'next':items[-1]['id'] if len(rows)>limit else None}


def _session_summaries(conn,start,end):
    slot="r.session"
    valid="r.report IS NOT NULL AND r.withdrawn_at IS NULL"
    sample="r.mode NOT IN ('retrospective','late_review') AND r.study_horizon IS NULL"
    scope=" r.kind='discovery' AND r.market_day BETWEEN %s AND %s"
    rows=conn.execute(
        "SELECT "+slot+" slot,COUNT(*) tasks,SUM("+valid+") issued,"
        "SUM("+valid+" AND "+sample+" AND r.has_forecast_target=1) targeted,"
        "SUM("+valid+" AND "+sample+" AND r.has_forecast_target=0) legacy,"
        "SUM(r.status='data_limited') data_limited,SUM(r.status IN ('failed','unknown','interrupted','expired')) failures "
        "FROM research_runs r WHERE"+scope+" GROUP BY slot",(start,end)).fetchall()
    counts=conn.execute(
        "SELECT "+slot+" slot,SUM(d.action='select') buy,SUM(d.action IN ('conditional','watch')) watch "
        "FROM research_runs r JOIN research_decisions d ON d.run_id=r.id "
        "WHERE"+scope+" AND "+valid+" AND NOT "+stock_library.WITHDRAWN_SQL+
        " AND COALESCE(d.review_event,0)=0 GROUP BY slot",(start,end)).fetchall()
    controls=conn.execute(
        "SELECT "+slot+" slot,d.horizon,d.cohort,COUNT(*) total,SUM(d.status='complete') scored,"
        "SUM(d.status='complete' AND d.return_value>0) up,"
        "SUM(d.status IN ('missing_start','missing_end','not_comparable')) missing "
        "FROM research_controls d JOIN research_runs r ON r.id=d.run_id WHERE"+scope+
        " AND d.target_version=%s AND "+valid+
        " AND NOT "+stock_library.WITHDRAWN_SQL+" AND d.cohort IN ('selected','watch') GROUP BY slot,d.horizon,d.cohort",
        (start,end,sessions.TARGET_VERSION)).fetchall()
    lookup={row['slot']:dict(row) for row in rows}
    decisions={row['slot']:dict(row) for row in counts}
    result=[]
    for session,spec in sessions.SESSIONS.items():
        stats={key:int(lookup.get(session,{}).get(key) or 0) for key in ('tasks','issued','targeted','legacy','data_limited','failures')}
        stats.update({key:int(decisions.get(session,{}).get(key) or 0) for key in ('buy','watch')})
        outcomes=[{**{key:row[key] for key in ('horizon','cohort')},
                   **{key:int(row[key] or 0) for key in ('total','scored','up','missing')}}
                  for row in controls if row['slot']==session]
        result.append({'session':session,'goal':spec['goal'],'evaluation':spec['evaluation'],**stats,
                       'outcomes':outcomes,'improvement':_improvement(session,stats,outcomes)})
    return result


def _improvement(session,stats,outcomes):
    if stats.get('failures'):
        return (f"本时段 {stats['tasks']} 次提交中有 {stats['failures']} 次运行问题，先核对失败和远端终态未知的原请求、"
                "资料及签发窗口，恢复正常出报；再按原窗口评价已签发样本，不用观察上涨比例替代正式入选表现。")
    if not stats['targeted']:
        return {'noon':'先核对 11:30 截点覆盖，并在午后开盘保留可核报价，积累完整的当日样本。',
                'tail':'补齐 14:35 截点和次日正式收盘对照，再检查隔夜风险条件。',
                'close':'积累按新口径签发的样本，分别复核第 1 日和第 3 日。'}[session]
    if stats['data_limited'] or any(item['missing'] for item in outcomes):
        return '优先查采集和可比性缺口。按截点行情、参考价、公司行动和终点行情分别核对。'
    selected=[item for item in outcomes if item['cohort']=='selected' and item['scored']>=10]
    if not selected:
        return '继续记录相同窗口的入选和观察表现。每个窗口达到 10 个可评入选样本后，再比较命中和覆盖。'
    if any(item['up']/item['scored']<.5 for item in selected):
        return '复查同窗口失效原判，优先分离结构失效、板块同步性和风险证据。先做时间外复测。'
    return '用同窗观察样本作对照，核对相对方向与覆盖，再决定规则实验是否需要扩大。'


def monthly(month=None, *, archived=True):
    now=store.now()
    month=month or now[:7]
    try:
        first=date.fromisoformat(month+'-01')
    except ValueError:
        raise HTTPException(422,'月份须为 YYYY-MM') from None
    if month>now[:7]:
        raise HTTPException(422,'请选择当前或历史月份')
    after=(first.replace(day=28)+timedelta(days=4)).replace(day=1)
    start=first.isoformat()
    end=min((after-timedelta(days=1)).isoformat(),now[:10])
    with store.db(readonly=True,isolation='REPEATABLE READ',timeout=60) as conn:
        archive=store.setting(_archive_key('monthly',month),conn=conn) if archived else None
        if archive:
            return {**archive['summary'],'archive':archive['receipt']}
        latest=pool_market.latest_close_day(conn,now)
        market=_daily_series(conn,start,min(end,latest) if latest else None)
        studies=_session_summaries(conn,start,end)
        calendar=conn.execute(
            "SELECT COUNT(*) FROM heads h JOIN calendar_records c ON c.fact_id=h.fact_id WHERE h.dataset='calendar' "
            "AND h.symbol='*' AND h.day BETWEEN %s AND %s AND c.trading=1",
            (start,min(end,latest) if latest else end)).fetchone()[0]
    width=sum(row['breadth'] for row in market)/len(market) if market else None
    path=prod(1+row['move'] for row in market)-1 if market else None
    if len(market)<5:
        next_step='已有日线天数较少。下月先补足交易日和同窗口样本，再判断是否改选股阈值。'
    elif width>=.55:
        next_step='宽度整体偏强。下月复核强势延续在不同板块的覆盖，并记录放量失守的反例。'
    elif width<=.45:
        next_step='宽度整体偏弱。下月重点复核逆势强股的板块支撑，拆开记录反弹和持续性。'
    else:
        next_step='宽度整体分化。下月把个股相对板块表现与大盘同步涨跌分开评估。'
    return {'month':month,'through':market[-1]['day'] if market else None,'partial':month==now[:7],
            'market_days':market,'expected_days':calendar,'mean_breadth':width,'equal_weight_path':path,
            'positive_days':sum(row['move']>0 for row in market),'sessions':studies,
            'next_month':after.isoformat()[:7],'next_step':next_step,'archive':None}


def _daily_due(conn, now, since):
    day=pool_market.latest_close_day(conn,now)
    if (not day or day<since[:10] or now<sessions.local_time(day,_TIMES['daily'])
            or store.setting(_archive_key('daily',day),conn=conn)
            or not collection.market_daily_received(conn,day) or sessions.trading_day(conn,day) is not True):
        return None
    evening=conn.execute("SELECT j.status FROM schedules s LEFT JOIN collection_jobs j "
                         "ON j.id=s.last_job AND s.last_slot=%s WHERE s.id='announcements'",(day,)).fetchone()
    news_status=evening[0] if evening and evening[0] else 'unconfirmed'
    if now<sessions.local_time(day,'21:00'):
        if news_status in ('unconfirmed','queued','running','waiting'):
            return None
        if conn.execute("SELECT 1 FROM research_runs WHERE active_slot=1 AND kind='discovery' AND market_day=%s "
                        "AND session='close' LIMIT 1",(day,)).fetchone():
            return None
    return day,news_status


def _monthly_due(conn, now, since):
    month_end=date.fromisoformat(now[:10]).replace(day=1)-timedelta(days=1)
    month=month_end.isoformat()[:7]
    following=sessions.next_trading_days(conn,month_end.isoformat())
    if len(following)<3:
        return None
    settled=following[2]
    if (settled<since[:10] or now<sessions.local_time(settled,_TIMES['monthly'])
            or store.setting(_archive_key('monthly',month),conn=conn)
            or not collection.market_daily_received(conn,settled)):
        return None
    return month,settled


def _archive(kind, period, since, summary, receipt):
    with store.db() as conn:
        row=conn.execute('SELECT data FROM settings WHERE `key`=%s FOR UPDATE',(_SCHEDULE,)).fetchone()
        if row and json.loads(row[0]).get(kind)==since:
            conn.execute('INSERT IGNORE INTO settings VALUES(%s,%s)',
                         (_archive_key(kind,period),store.dumps({'summary':summary,'receipt':receipt})))


def schedule_tick():
    now=store.now()
    with store.db(readonly=True) as conn:
        policy=store.setting(_SCHEDULE,{},conn=conn)
        daily_due=_daily_due(conn,now,policy['daily']) if policy.get('daily') else None
        monthly_due=_monthly_due(conn,now,policy['monthly']) if policy.get('monthly') else None
    if daily_due:
        day,news_status=daily_due
        data=daily(day,archived=False)
        if data['market'] and data['next_day']:
            pool=ledger.pool_review_status('close')
            news_total=news(day,1)['total']
            stamp=store.now()
            summary={key:data[key] for key in ('market','next_day','recent','inference','boards','industries','research_context')}
            receipt={'at':stamp,'late':stamp>=sessions.local_time(data['next_day'],'09:30'),
                     'news_status':news_status,'news_total':news_total,
                     'pool':{key:pool[key] for key in ('pools','checked','pending','needs_evidence','removed')} if pool['day']==day else None}
            _archive('daily',day,policy['daily'],summary,receipt)
    if monthly_due:
        month,settled=monthly_due
        data=monthly(month,archived=False)
        pending=sum(row['total']-row['scored']-row['missing'] for item in data['sessions'] for row in item['outcomes'])
        if not pending or now[:10]>settled:
            data.pop('archive')
            _archive('monthly',month,policy['monthly'],data,{'at':store.now(),'settled_day':settled,'pending':pending})
