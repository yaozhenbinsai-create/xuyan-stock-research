"""Vendor flow statistics and comparable series; never account ownership."""
import json
from datetime import date,timedelta
from foundation import assets as ms

SINA_URL='https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_zjlrqs'
METHOD='新浪公开页主力净额 r0_net；按供应商成交分档统计，非账户资金；原始金额为元，比例为小数。日统计无确切成交时刻，样本与最终行情有差异，收盘集合竞价覆盖未经确认。占比沿用新浪口径，不混用其他源成交额；不同供应商不可直接拼接。'


def fetch(params,timeout):
    from foundation.sources import http,number,SourceError
    code=ms.symbol(params.get('symbol') or params['symbols'][0])
    if not code.startswith(('sh.','sz.')):raise SourceError('资金接口仅完成沪深样本验证，北交所仍待核验','coverage')
    response=http('GET',SINA_URL,timeout=timeout,params={'page':1,'num':20,'sort':'opendate','asc':0,'daima':code.replace('.','')})
    try:rows=response.json()
    except ValueError:raise SourceError('资金接口返回格式变化','schema') from None
    if not isinstance(rows,list):raise SourceError('资金接口未返回记录列表','schema')
    result=[]
    for r in rows:
        if not isinstance(r,dict) or not r.get('opendate'):raise SourceError('资金记录缺少日期','schema')
        day=ms.day(r['opendate'])
        if not params['start']<=day<=params['end']:continue
        net=number(r.get('r0_net'));ratio=number(r.get('r0_ratio'))
        if net is None or ratio is None or abs(ratio)>1.001:raise SourceError('资金金额或比例口径校验失败','schema')
        # The public table has no observation clock. Never manufacture one.
        result.append({'day':day,'symbol':code,'key':'sina:daily','provider':'sina','basis':'sina_r0_v1','period':'daily',
                       'net_main':net,'ratio_main':ratio,'net_all':number(r.get('netamount')),'close':number(r.get('trade')),
                       'unit':'CNY','value_type':'daily_total','time_precision':'day','observed_at':ms.local_now().isoformat(),
                       'method':METHOD,'history_window':'最近20条，缺交易日不补零','raw':r})
    return result

