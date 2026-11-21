"""Freeze a historical price range for conditional short-horizon scenarios."""
from __future__ import annotations

import math


VERSION = 'historical-scenarios-v1'
HORIZONS = {'D1':(1,20),'D3':(3,12)}
MIN_DIRECTION = 6
WINDOW = 61


def statements(value):
    return [value[key] for key in ('upside','downside','chase_risk') if value and value.get(key)]


def trading_dates(conn, first_day, last_day):
    rows = conn.execute(
        "SELECT h.day,cal.trading FROM heads h JOIN calendar_records cal ON cal.fact_id=h.fact_id "
        "WHERE h.dataset='calendar' AND h.symbol='*' AND h.day BETWEEN %s AND %s "
        "ORDER BY h.day DESC LIMIT 150", (first_day,last_day),
    ).fetchall()
    return [row['day'] for row in reversed(rows) if row['trading'] == 1]


def _price(value):
    return type(value) in (int,float) and math.isfinite(value) and value>0


def _quantile(ordered, fraction):
    pos = (len(ordered)-1)*fraction
    low = int(pos)
    return ordered[low] + (ordered[math.ceil(pos)]-ordered[low])*(pos-low)


def _band(values, price):
    if len(values)<MIN_DIRECTION or not _price(price):
        return None
    low = _quantile(values,.25)
    high = _quantile(values,.75)
    return {'return_low':low,'return_high':high,'price_low':round(price*(1+low),4),
            'price_high':round(price*(1+high),4),'samples':len(values)}


def _comparable(before, after, action_days):
    return (before is not None and after is not None and after['day'] not in action_days
            and _price(after.get('preclose')) and abs(after['preclose']/before['close']-1)<=.001)


def reference(bars, dates, metrics, action_days=(), *, target=None):
    """Use completed, comparable close pairs and non-overlapping multi-day spans."""
    cutoff = metrics['day']
    partial = metrics.get('partial_bar')
    days = [day for day in dates if day<cutoff or day==cutoff and not partial][-WINDOW:]
    daily = {bar['day']:bar for bar in bars if not bar.get('partial_bar') and not bar.get('suspended')
             and _price(bar.get('close'))}
    sequence = [daily.get(day) for day in days]
    action_days = set(action_days)
    price = metrics.get('close')
    ranges = {}
    for key,(step,minimum) in HORIZONS.items():
        if target and key not in target['horizons']:
            continue
        values = []
        for end in range(len(sequence)-1,step-1,-step):
            span = sequence[end-step:end+1]
            if all(_comparable(a,b,action_days) for a,b in zip(span,span[1:])):
                values.append(span[-1]['close']/span[0]['close']-1)
        rising = sorted(value for value in values if value>0)
        falling = sorted(value for value in values if value<0)
        enough = len(values)>=minimum
        ranges[key] = {'samples':len(values),'rising':len(rising),'falling':len(falling),
                       'minimum':minimum,'minimum_direction':MIN_DIRECTION,
                       'upside':_band(rising,price) if enough else None,
                       'downside':_band(falling,price) if enough else None}
    if target and 'PM' in target['horizons']:
        ranges['PM']={'samples':0,'rising':0,'falling':0,'upside':None,'downside':None}
        days=[]
    return {'version':VERSION,'reference_price':price,'as_of':metrics.get('quote_time') or cutoff,
            'history_start':days[0] if days else None,'history_end':days[-1] if days else None,
            'horizons':ranges}
