"""Complete a bounded valid-bar sample from stored facts after suspensions.

The minimum sample size stays unchanged. No prices or pre-listing bars are made up.
"""
from collections import deque
from datetime import date,timedelta
from foundation import record_store


def expand(conn,history,end,known_at,*,minimum=60,maximum=65):
    short=[symbol for symbol,bars in sorted(history.items())
           if sum(bool(b.get('close')) and not b.get('suspended') for b in bars)<minimum]
    result={'checked':min(len(short),64),'deferred':max(0,len(short)-64),'recovered':0,'symbols':{}}
    if not short:return result
    selected=short[:64]
    floor=max('2026-01-01',(date.fromisoformat(end)-timedelta(days=365)).isoformat())
    marks=','.join(['%s']*len(selected))
    rows=conn.execute(
        "SELECT h.symbol,f.id,f.first_seen FROM heads h FORCE INDEX (head_symbol) "
        "JOIN facts f ON f.id=h.fact_id JOIN daily_bars d ON d.fact_id=f.id WHERE h.dataset='daily' AND h.symbol IN ("+marks+") "
        "AND h.day BETWEEN %s AND %s AND f.first_seen<=%s "
        "AND d.`close`>0 AND COALESCE(d.suspended,0)=0 "
        "ORDER BY h.symbol,h.day DESC,h.record_key LIMIT 20001",(*selected,floor,end,known_at)).fetchall()
    if len(rows)>20000:
        result.update(checked=0,deferred=len(short),reason='历史补读达到边界，保留缺口')
        return result
    samples={symbol:[] for symbol in selected}
    for row in record_store.hydrate(conn,rows):
        sample=samples[row['symbol']]
        if len(sample)>=maximum:continue
        bar=row['payload']
        if not bar.get('close') or bar.get('suspended'):continue
        sample.append({**bar,'_id':row['id'],'_known_at':row['first_seen']})
    for symbol,sample in samples.items():
        before=sum(bool(b.get('close')) and not b.get('suspended') for b in history[symbol])
        if len(sample)>before:
            history[symbol]=deque(reversed(sample),maxlen=maximum)
        after=max(before,len(sample))
        recovered=before<minimum<=after
        result['recovered']+=int(recovered)
        result['symbols'][symbol]={'before':before,'after':after,'recovered':recovered,
                                   'basis':'按真实有效交易日补读已有历史；停牌与新股历史缺口保留'}
    return result
