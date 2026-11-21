"""Time-matched comparisons and prospectively frozen non-selection samples."""
from __future__ import annotations

from collections import defaultdict
import json

from foundation import database as store
from research import learning, market, sessions, state_store


MINUTE_WINDOW = 60
COHORTS = ('selected','watch','rejected','unreviewed','outside')


def afternoon_symbols(now):
    if not '13:02'<=now[11:16]<'13:04':
        return []
    day=now[:10]
    with store.db(readonly=True) as conn:
        prior=conn.execute("SELECT 1 FROM collection_jobs WHERE created>=%s AND "
                           "`trigger`='afternoon_review' LIMIT 1",(day,)).fetchone()
        if prior:
            return []
        rows=conn.execute("SELECT DISTINCT c.symbol FROM research_controls c JOIN research_runs r ON r.id=c.run_id "
                          "WHERE c.start_at=%s AND c.horizon='PM' AND c.status='awaiting_quote' "
                          "AND r.withdrawn_at IS NULL ORDER BY c.symbol LIMIT 160",
                          (sessions.local_time(day,'13:05'),)).fetchall()
    return [row[0] for row in rows]


def pin(conn, owner_type, owner_id, ids):
    for identifier in set(ids):
        conn.execute('INSERT IGNORE INTO evidence_refs VALUES(%s,%s,%s,%s)',(owner_type,owner_id,identifier,store.now()))


def capture_peers(conn, symbols, at):
    return [quote for symbol in symbols if (quote:=market.quote_near(conn,symbol,at,seconds=MINUTE_WINDOW))]


def benchmark(conn, result, roster, stored, identity):
    if result['status']!='complete' or len(roster)!=3:
        return result
    end=result['endpoint']
    anchor=result['reference']['at']
    references={r['symbol']:r for r in stored}
    if len(references)<len(roster):
        references.update({r['symbol']:r for r in capture_peers(conn,[s for s in roster if s not in references],anchor)})
    components=[]
    for symbol in roster:
        reference=references.get(symbol)
        bar=market.closing_bar(conn,symbol,end) if reference else None
        if bar and (bar.get('close') or 0)>0 and not bar.get('suspended') and market.comparable_window(conn,symbol,reference,end):
            components.append({'symbol':symbol,'start_fact':reference['fact_id'],'end_fact':bar['fact_id'],
                               'return':bar['close']/reference['price']-1})
    result['benchmark']={'kind':'frozen_peer_basket','roster':roster,'matched':len(components),'anchor':anchor,
                         'window_seconds':MINUTE_WINDOW,'components':components}
    if len(components)==len(roster):
        value=sum(row['return'] for row in components)/len(components)
        result.update(benchmark_return=value,excess_return=result['return']-value)
        pin(conn,'outcome',identity,[x[key] for x in components for key in ('start_fact','end_fact')])
    return result


def freeze_controls(conn, run_id, report, bundle, shadow):
    if report['kind']!='discovery' or report['mode'] in ('retrospective','late_review') or report.get('study_horizon'):
        return
    session=report.get('session','close')
    target=report.get('forecast_target')
    day=report['market_day'] if target or session!='close' else report['horizons'].get('D1')
    if not day:return
    basis='market_close' if target and session!='noon' else 'session_quote'
    at=sessions.local_time(day,'15:00' if basis=='market_close' else {'noon':'13:05','tail':'14:46','close':'09:35'}[session])
    if report['published_at']>=at and not (target and session=='close'):
        return
    cohorts={'select':'selected','conditional':'selected','watch':'watch','reject':'rejected'}
    samples={row['symbol']:cohorts[row['action']] for row in report['decisions'] if not row.get('review_event')}
    for row in bundle.get('comparison_samples',[]):
        samples.setdefault(row['symbol'],row['cohort'])
    features={row['symbol']:learning.observation_features(row) for row in bundle['candidates']}
    features.update({row['symbol']:{k:v for k,v in row.items() if k not in ('symbol','cohort')}
                     for row in bundle.get('comparison_samples',[])})
    anchors={row['symbol']:{'symbol':row['symbol'],'at':at,'price':row['metrics']['close'],'fact_id':row['bar_ids'][-1]}
             for row in bundle['candidates'] if target and session=='close' and row.get('bar_ids') and (row['metrics'].get('close') or 0)>0}
    for symbol,cohort in samples.items():
        for horizon,endpoint in report['horizons'].items():
            if endpoint:
                data={'published_at':report['published_at'],'method':report['method_version'],
                      'quote_window_seconds':300 if target and session=='noon' else MINUTE_WINDOW,
                      'shadow':{key:signals[symbol] for key,signals in shadow.items() if symbol in signals}}
                if target:
                    data.update(target_version=target['version'],reference_basis=basis)
                if symbol in anchors:
                    data['reference']=anchors[symbol]
                if symbol in features:data['features']=features[symbol]
                written=conn.execute('INSERT IGNORE INTO research_controls(run_id,symbol,cohort,horizon,start_at,endpoint,status,data) '
                             'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(run_id,symbol,cohort,horizon,at,endpoint,'awaiting_quote',store.dumps(data)))
                if written.rowcount:
                    state_store.sync_control(conn,written.lastrowid,data)


def refresh(conn,now):
    cursor=store.setting('review_control_cursor',0,conn=conn)
    rows=conn.execute("SELECT * FROM research_controls WHERE id>%s AND start_at<=%s AND "
                      "(status IN ('awaiting_quote','awaiting_close','missing_end') OR "
                      "(status='missing_start' AND reference_basis='market_close')) "
                      "ORDER BY id LIMIT 60 FOR UPDATE",(cursor,now)).fetchall()
    store.set_setting('review_control_cursor',rows[-1]['id'] if len(rows)==60 else 0,conn=conn)
    for row in rows:
        data=json.loads(row['data'])
        reference=data.get('reference')
        if not reference:
            if data.get('reference_basis')=='market_close':
                start_bar=market.closing_bar(conn,row['symbol'],row['start_at'][:10])
                if start_bar and (start_bar.get('close') or 0)>0 and not start_bar.get('suspended'):
                    reference={'symbol':row['symbol'],'at':row['start_at'],'price':start_bar['close'],'fact_id':start_bar['fact_id']}
            else:
                reference=market.quote_near(conn,row['symbol'],row['start_at'],since=data['published_at'],seconds=data['quote_window_seconds'])
            if reference:
                data['reference']=reference
                pin(conn,'control',str(row['id']),[reference['fact_id']])
        at_end=now>=sessions.local_time(row['endpoint'],'15:00')
        if not reference:
            state='missing_start' if at_end else 'awaiting_quote'
        elif not at_end:
            state='awaiting_close'
        else:
            bar=market.closing_bar(conn,row['symbol'],row['endpoint'])
            if not bar or not (bar.get('close') or 0)>0 or bar.get('suspended'):
                state='missing_end'
            elif not market.comparable_window(conn,row['symbol'],reference,row['endpoint']):
                state='not_comparable'
            else:
                state='complete'
                data.update(return_=bar['close']/reference['price']-1,closing_fact=bar['fact_id'])
                pin(conn,'control',str(row['id']),[bar['fact_id'],reference['fact_id']])
        if state!=row['status'] or data.get('reference') and not json.loads(row['data']).get('reference'):
            conn.execute('UPDATE research_controls SET status=%s,data=%s WHERE id=%s',(state,store.dumps(data),row['id']))
            state_store.sync_control(conn,row['id'],data)


def summary(conn=None):
    if conn is None:
        with store.db(readonly=True) as conn:
            return summary(conn)
    groups=defaultdict(lambda: {'total':0,'evaluable':0,'positive':0,'returns':None,'statuses':{}})
    rows=conn.execute("SELECT cohort,horizon,status,COUNT(*) n,AVG(return_value) mean_return, "
                      "SUM(CASE WHEN return_value>0 THEN 1 ELSE 0 END) positive "
                      "FROM research_controls GROUP BY cohort,horizon,status").fetchall()
    for row in rows:
        group=groups[row['cohort'],row['horizon']]
        group['total']+=row['n'];group['statuses'][row['status']]=row['n']
        if row['status']=='complete':
            group.update(evaluable=row['n'],positive=int(row['positive']),returns=float(row['mean_return']))
    horizons=('PM','D1','D3') if any(h=='PM' for _,h in groups) else ('D1','D3')
    return {'groups':[{'cohort':cohort,'horizon':horizon,**groups[cohort,horizon]} for cohort in COHORTS for horizon in horizons],
            'basis':'同一报告内冻结入选、观察、拒绝、未研究及池外对照，使用预设同窗参考报价。'}


def page(limit=25,before=None):
    from research import stock_library
    where=' WHERE NOT ('+stock_library.retired_sql('r.symbol')+')'+(' AND id<%s' if before else '')
    with store.db(readonly=True) as conn:
        rows=conn.execute('SELECT * FROM research_controls r'+where+' ORDER BY id DESC LIMIT %s', (before,limit+1) if before else (limit+1,)).fetchall()
    items=[{**dict(row),'data':json.loads(row['data'])} for row in rows[:limit]]
    return {'items':items,'next':items[-1]['id'] if len(rows)>limit else None}
