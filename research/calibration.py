"""An out-of-time reliability check for observed cohort frequencies."""
from __future__ import annotations

from collections import defaultdict
import math
from statistics import fmean

from foundation import database as store


def _reliability(rows):
    if len(rows) < 120 or len({r['market_day'] for r in rows}) < 30:
        return {'state':'insufficient','reason':'需要至少120个可评同窗样本及30个交易日'}
    days = sorted({r['market_day'] for r in rows})
    boundary=days[int(len(days)*.7)]
    # Remove overlapping labels at the split boundary.
    train=[r for r in rows if r['market_day']<boundary and r['endpoint']<boundary]
    holdout=[r for r in rows if r['market_day']>=boundary]
    groups=defaultdict(list)
    for row in train:
        groups[row['cohort']].append(row['positive'])
    supported={key:fmean(values) for key,values in groups.items() if len(values)>=25}
    predicted=[(r,supported[r['cohort']]) for r in holdout if r['cohort'] in supported]
    if len(predicted)<40 or len({r['market_day'] for r,_ in predicted})<10:
        return {'state':'insufficient','reason':'训练组或时间外可核样本不足','split_day':boundary}
    base=fmean([r['positive'] for r in train])
    by_day=defaultdict(list)
    for row,probability in predicted:
        by_day[row['market_day']].append(((probability-row['positive'])**2,(base-row['positive'])**2))
    brier=fmean([fmean([a for a,_ in group]) for group in by_day.values()])
    baseline=fmean([fmean([b for _,b in group]) for group in by_day.values()])
    if brier>=baseline:
        return {'state':'unqualified','reason':'时间外误差未优于统一基准','brier':brier,'baseline_brier':baseline,'split_day':boundary}
    result=[]
    for cohort,p in sorted(supported.items()):
        tested=[r for r,_ in predicted if r['cohort']==cohort]
        if len(tested)<12:
            continue
        observed=fmean([r['positive'] for r in tested])
        # Report the interval for descriptive comparison; do not certify independence.
        n=len(tested);z=1.96;scale=1+z*z/n
        center=(observed+z*z/(2*n))/scale
        width=z*math.sqrt(observed*(1-observed)/n+z*z/(4*n*n))/scale
        if center-width<=p<=center+width:
            result.append({'cohort':cohort,'frequency':p,'observed':observed,'test_n':n,
                           'interval':[max(0,center-width),min(1,center+width)]})
    return {'state':'calibrated' if result else 'unqualified','split_day':boundary,'brier':brier,
            'baseline_brier':baseline,'groups':result,'test_days':len(by_day),
            'reason':'分组历史频率按交易日做时间外检验，区间是样本描述，个股概率保持未知'}


def evaluate(version, horizon):
    with store.db(readonly=True) as conn:
        rows=conn.execute(
            "SELECT r.market_day,c.symbol,c.endpoint,c.cohort,c.start_at,"
            "c.return_value>0 positive "
            "FROM research_controls c JOIN research_runs r ON r.id=c.run_id "
            "WHERE c.horizon=%s AND c.status='complete' AND c.method_version=%s "
            "ORDER BY r.market_day DESC,c.start_at ASC,c.id ASC LIMIT 2001",(horizon,version),
        ).fetchall()
    distinct={}
    for row in rows[:2000]:
        distinct.setdefault((row['market_day'],row['symbol']),row)
    return {'horizon':horizon,'version':version,'sample_n':len(distinct),**_reliability(list(distinct.values()))}
