"""Verified source adapters, extracted without legacy workers or research code."""
from __future__ import annotations
import hashlib,json,re,subprocess,sys
from urllib.parse import urlparse
from datetime import datetime
import httpx
from foundation import assets as ms, database as store, record_store


class SourceError(Exception):
    def __init__(self,message,kind="upstream"):
        super().__init__(message);self.kind=kind

def number(value):
    try:
        x=float(value)
        return x if x==x and abs(x)!=float('inf') else None
    except (TypeError,ValueError):return None

def http(method,url,*,timeout=20,**kwargs):
    try:
        response=httpx.request(method,url,timeout=timeout,follow_redirects=False,headers={"User-Agent":"StockResearch/0.2 (local personal research)"},**kwargs)
    except httpx.TimeoutException:raise SourceError("来源请求超时","timeout") from None
    except httpx.HTTPError:raise SourceError("来源网络连接失败","network") from None
    if response.status_code==403:raise SourceError("来源拒绝访问（HTTP 403），请检查来源权限","restricted")
    if response.status_code==429:raise SourceError("来源限流（HTTP 429），等待后按原批次重试","rate_limit")
    if response.status_code in (408,500,502,503,504):
        raise SourceError(f"来源暂时未完成请求（HTTP {response.status_code}）","network")
    if response.status_code!=200:raise SourceError(f"来源返回 HTTP {response.status_code}")
    if len(response.content)>30_000_000:raise SourceError("响应超过大小上限","schema")
    return response

def _bs_diagnostic_suffix(value):
    phases={'login':'登录','query':'查询','pagination':'分页','logout':'退出登录'}
    if not isinstance(value,dict):return ''
    phase=value.get('failed_phase')
    if not isinstance(phase,str) or phase not in phases:return ''
    stages=value.get('stages')
    if not isinstance(stages,list) or len(stages)>4:return ''
    entry=next((item for item in stages if isinstance(item,dict) and item.get('phase')==phase and item.get('status')=='failed'),None)
    if entry is None:return ''
    details=[phases[phase]+'阶段']
    signals=entry.get('sdk_signals')
    if isinstance(signals,list) and 'sdk_peer_closed' in signals:
        details.append('报文未完连接关闭')
    if isinstance(signals,list) and 'sdk_timeout_reported' in signals:
        details.append('SDK报告超时')
    elapsed=entry.get('elapsed_ms')
    if type(elapsed) is int and 0<=elapsed<=3_600_000:
        details.append(f'阶段耗时{elapsed/1000:.1f}秒')
    return '（'+'；'.join(details)+'）'


def bs_call(op,params,timeout):
    try:
        result=subprocess.run([sys.executable,"-m","foundation.baostock_worker"],input=json.dumps({"operation":op,"params":params}),capture_output=True,text=True,encoding="utf-8",cwd=store.ROOT,timeout=timeout,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    except subprocess.TimeoutExpired:raise SourceError("BaoStock 请求超时，已结束本次连接；可稍后重试","timeout") from None
    try:body=json.loads(result.stdout)
    except (ValueError,TypeError):raise SourceError("BaoStock 返回无法解析","schema") from None
    if body.get('error'):
        code=re.search(r'\((\d+)\)',str(body['error']))
        code=code[1] if code else '未提供'
        if code in ('10002007','10002006'):
            detail=_bs_diagnostic_suffix(body.get('diagnostics'))
            raise SourceError('BaoStock 网络收发失败（'+code+'）'+detail+'，系统稍后续传此范围','network')
        if code=='10001001' and not str(body['error']).startswith('BaoStock 登录失败'):
            # The isolated worker already checked a successful login. A query
            # or pagination losing that session is a completed, unpublished
            # request, so the same unit can reconnect rather than abort siblings.
            raise SourceError('BaoStock 查询会话已失效（10001001），本次连接已结束，稍后重连续传原范围','network')
        raise SourceError('BaoStock 请求未完成，错误码 '+code,'upstream')
    return body['rows']

def volume_in_shares(raw,amount,low,high):
    """Validate the vendor lot/share convention against traded value, never guess by board."""
    raw=number(raw)
    if raw==0:return 0.0,"zero"
    if not raw or not amount or not low or not high:return None,"unknown"
    choices=[factor for factor in (1,100) if low*.98<=amount/(raw*factor)<=high*1.02]
    return (raw*choices[0],"shares" if choices[0]==1 else "lots_to_shares") if len(choices)==1 else (None,"unknown")

def board(code):
    if code.startswith('bj.'):return '北交所'
    if code.startswith(('sh.688','sh.689')):return '科创板'
    if code.startswith(('sz.300','sz.301')):return '创业板'
    return '主板' if code.startswith(('sh.60','sz.00')) else '其他'

def normalize_bs(op,raw,p):
    result=[]
    for r in raw:
        code=r.get('code') or p.get('symbol') or '*'
        if code!='*':
            try:code=ms.symbol(code)
            except Exception:continue
        if op=='calendar':
            result.append({"day":r['calendar_date'],"symbol":"*","trading":r['is_trading_day']=='1',"markets":["sh","sz"],"raw":r});continue
        if op in ('basics','universe'):
            if op=='basics' and str(r.get('type'))!='1':continue
            if not code.startswith(('sh.60','sh.688','sh.689','sz.00','sz.30','bj.')):continue
            # Details describe listing status; the daily universe describes
            # that date's market roster. A pointed query is never a census.
            key = 'detail' if op=='basics' else 'universe_partial' if p.get('symbols') else 'universe'
            if op=='universe' and p.get('symbols') and code not in p['symbols']:continue
            result.append({"day":ms.today() if op=='basics' else p['end'],"symbol":code,"key":key,"name":r.get('code_name',r.get('codeName','')),"listed":r.get('ipoDate') or None,"delisted":r.get('outDate') or None,"status":r.get('status') or r.get('tradeStatus'),"board":board(code),"raw":r});continue
        if op in ('industry','concept','area'):
            if not code.startswith(('sh.60','sh.688','sh.689','sz.00','sz.30','bj.')):continue
            if p.get('symbols') and code not in p['symbols']:continue
            fields={'industry':('industry','industryClassification'),'concept':('concept','conceptClassification'),'area':('area','areaClassification')}
            key,system=fields[op]
            group=r.get(key) or r.get(key+'Name') or r.get(key+'_name')
            unclassified=not bool(group)
            if not group:
                if key not in r and key+'Name' not in r and key+'_name' not in r:raise SourceError(f"分类字段不匹配，实际字段：{','.join(r.keys())[:150]}","schema")
                group='未分类（来源空值）'
            effective=r.get('updateDate') or r.get('date')
            if effective and effective>p['end']:raise SourceError('返回的分类版本晚于所查日期','schema')
            if not effective and p['end']!=ms.today():raise SourceError('接口未提供可验证的历史分类日期','unsupported')
            result.append({"day":p['end'],"symbol":code,"key":hashlib.sha1((str(r.get(system,''))+group).encode()).hexdigest()[:20],"group":group,"taxonomy":r.get(system) or 'BaoStock '+op,"observed_at":ms.today(),"source_effective_date":effective,"historical_effective_unknown":not bool(effective),"unclassified":unclassified,"raw":r});continue
        if op in ('daily','daily_all','minutes'):
            v={"day":r.get('date') or p['end'],"symbol":code,"adjust":"raw","raw":r}
            for field in ('open','high','low','close','volume','amount'):v[field]=number(r.get(field))
            v.update(preclose=number(r.get('preclose')),turnover=number(r.get('turn')),pe=number(r.get('peTTM')),pb=number(r.get('pbMRQ')),pct=number(r.get('pctChg')),suspended=r.get('tradestatus')=='0',is_st=r.get('isST')=='1',volume_unit='shares')
            if op=='minutes':
                stamp=r.get('time','');v.update(key=f"{p.get('period',5)}:{stamp}",source_time=stamp,period=p.get('period',5),closed=stamp[:12]<ms.local_now().strftime('%Y%m%d%H%M'))
            result.append(v);continue
        if op in ('profit','balance','cashflow','growth'):
            pub=r.get('pubDate')
            if not pub:raise SourceError("财务数据缺少实际披露日期","schema")
            result.append({"day":pub,"symbol":code,"key":op+':'+r.get('statDate',''),"report_type":op,"report_date":r.get('statDate'),"published":pub,"fields":r,"raw":r});continue
        if op=='actions':
            day = r.get('dividOperateDate')
            if not day or not p['start']<=day<=p['end']:
                raise SourceError('复权因子记录缺少范围内的有效日期','schema')
            factors = {k:number(r.get(k)) for k in ('foreAdjustFactor','backAdjustFactor','adjustFactor')}
            if not any(v is not None and v>0 for v in factors.values()):
                raise SourceError('复权因子缺少可核数值','schema')
            result.append({"day":day,"symbol":code,"key":"factor","record_kind":"event","fields":factors})
    if op=='actions':
        result.append({'day':p['end'],'symbol':p['symbol'],'key':'coverage:'+p['start'], 'record_kind':'coverage',
                       'coverage_from':p['start'],'coverage_through':p['end'],'event_count':len(result)})
    return result

def tx_call(op,p,timeout):
    codes=p.get('symbols') or [p.get('symbol','sh.600000')]
    compact=[ms.symbol(s).replace('.','') for s in codes]
    if op=='quotes':
        response=http('GET','https://qt.gtimg.cn/q='+','.join(compact),timeout=timeout)
        text=response.content.decode('gb18030',errors='replace');result=[]
        for code,body in re.findall(r'v_((?:sh|sz|bj)\d{6})="([^"]*)"',text):
            v=body.split('~')
            if len(v)<47 or not re.fullmatch(r'\d{14}',v[30]):continue
            amount=(number(v[37])*10000) if number(v[37]) is not None else None
            low,high=number(v[34]),number(v[33]);volume,unit=volume_in_shares(v[36],amount,low,high)
            stamp=datetime.strptime(v[30],'%Y%m%d%H%M%S').replace(tzinfo=ms.SHANGHAI).isoformat()
            result.append({"day":stamp[:10],"symbol":ms.symbol(code),"key":v[30],"name":v[1],"close":number(v[3]),"preclose":number(v[4]),"open":number(v[5]),"high":high,"low":low,"volume":volume,"amount":amount,"pct":number(v[32]),"turnover":number(v[38]),"pe":number(v[39]),"pb":number(v[46]),"source_time":stamp,"observed_at":ms.local_now().isoformat(),"volume_unit":unit,"raw":v})
        return result
    code=compact[0];period=int(p.get('period',5))
    if op=='daily':
        response=http('GET','https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get',timeout=timeout,params={'param':f"{code},day,{p['start']},{p['end']},640,"})
        field='day'
    else:
        if period not in (5,15,30,60):raise SourceError("分钟周期只支持 5/15/30/60","unsupported")
        response=http('GET','https://web3.ifzq.gtimg.cn/appstock/app/kline/mkline',timeout=timeout,params={'param':f'{code},m{period},,320'})
        field=f'm{period}'
    try:raw=response.json()['data'][code][field]
    except (ValueError,KeyError,TypeError):raise SourceError("腾讯行情结构改变或该证券不受支持","schema") from None
    result=[]
    for r in raw:
        stamp=r[0];d=stamp if op=='daily' else f'{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}'
        if not p['start']<=d<=p['end']:continue
        v={"day":d,"symbol":ms.symbol(code),"open":number(r[1]),"close":number(r[2]),"high":number(r[3]),"low":number(r[4]),"adjust":"raw","raw":r}
        amount=number(r[8])*10000 if op=='daily' and len(r)>8 and number(r[8]) is not None else None
        volume,unit=volume_in_shares(r[5],amount,v['low'],v['high'])
        v.update(volume=volume,amount=amount,volume_unit=unit,raw_volume=number(r[5]))
        if op=='daily':v['turnover']=number(r[7]) if len(r)>7 else None
        else:v.update(key=f'{period}:{stamp}',source_time=stamp,period=period,closed=stamp<ms.local_now().strftime('%Y%m%d%H%M'))
        result.append(v)
    return result

class RecordBatch(list):
    page_info=None

def announcements(p,timeout):
    # One page per durable unit. The queue applies provider limits between pages.
    stock_filter=''
    if p.get('symbols'):
        cache=store.setting('cninfo_orgs',{})
        if cache.get('day')!=ms.today():
            body=http('GET','https://www.cninfo.com.cn/new/data/szse_stock.json',timeout=timeout).json()
            cache={'day':ms.today(),'values':{r['code']:r['orgId'] for r in body['stockList']}}
            store.set_setting('cninfo_orgs',cache)
        mapped=[]
        for code in p['symbols']:
            org=cache['values'].get(code[3:])
            if not org:raise SourceError('公告机构映射中没有 '+code,'unsupported')
            mapped.append(code[3:]+','+org)
        stock_filter=';'.join(mapped)
    page=int(p.get('page',1))
    response=http('POST','https://www.cninfo.com.cn/new/hisAnnouncement/query',timeout=timeout,data={
        'pageNum':str(page),'pageSize':'30','column':'szse','tabName':'fulltext',
        'seDate':p['start']+'~'+p['end'],'stock':stock_filter,'searchkey':'','category':'',
        'sortName':'time','sortType':'desc','isHLtitle':'true'})
    try:body=response.json()
    except ValueError:raise SourceError('公告返回结构无法解析','schema') from None
    if 'announcements' not in body or 'totalAnnouncement' not in body:raise SourceError('公告响应缺少分页与记录字段','schema')
    total=int(body['totalAnnouncement']);pages=(total+29)//30
    if pages>500:raise SourceError('单日公告超过 15000 条上限，请按股票获取','incomplete')
    raw=body.get('announcements') or []
    if not raw and page<=pages:raise SourceError('公告分页中途返回空页','incomplete')
    result=RecordBatch()
    result.page_info={'page':page,'pages':pages,'total':total,'next':page+1 if page<pages else None}
    for r in raw:
        c=r.get('secCode','')
        if not re.fullmatch(r'\d{6}',c):continue
        exchange='sh' if c.startswith('6') else 'sz' if c.startswith(('0','3')) else 'bj'
        code=exchange+'.'+c
        if not ms.ASHARE.fullmatch(code):continue
        if p.get('symbols') and code not in p['symbols']:continue
        stamp=datetime.fromtimestamp(r['announcementTime']/1000,ms.SHANGHAI).isoformat()
        if not p['start']<=stamp[:10]<=p['end']:raise SourceError('公告返回日期超出所查范围','schema')
        path=r.get('adjunctUrl','');url='https://static.cninfo.com.cn/'+path if path and not path.startswith(('http','//')) else None
        result.append({'day':stamp[:10],'symbol':code,'key':str(r['announcementId']),'title':re.sub('<[^>]+>','',r.get('announcementTitle','')),'published':stamp,'url':url,'text':None,'raw':r})
    return result

def announcement_text(p,timeout):
    with store.db(readonly=True) as c:
        r=c.execute("SELECT f.id FROM facts f JOIN heads h ON h.fact_id=f.id WHERE f.id=%s AND h.dataset='announcements'",(p['record_id'],)).fetchone()
        record=record_store.load(c,r[0]) if r else None
    if not r:raise SourceError('公告记录已修订，请重新提交正文任务','stale')
    url=record.get('url','');parts=urlparse(url)
    if parts.scheme!='https' or parts.netloc!='static.cninfo.com.cn' or not parts.path.lower().endswith('.pdf') or parts.query or parts.fragment:raise SourceError('公告链接不在巨潮 PDF 白名单内','unsupported')
    content=http('GET',url,timeout=timeout).content
    if len(content)>20_000_000 or not content.startswith(b'%PDF-'):raise SourceError('公告不是有效 PDF 或超过 20MB','unsupported')
    try:
        command=[sys.executable,'-m','foundation.pdf_worker']
        if re.search(r'年度报告|半年度报告|季度报告|财务报告|业绩快报',record.get('title','')):
            command.append('--filing')
        result=subprocess.run(command,input=content,capture_output=True,cwd=store.ROOT,timeout=35,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        parsed=json.loads(result.stdout)
    except (subprocess.TimeoutExpired,ValueError):raise SourceError('公告解析超时或失败，未生成正文','parse') from None
    if not parsed.get('text') or len(parsed['text'].strip())<100:raise SourceError('公告文字不足，可能为扫描件，不能作为已阅读正文','parse')
    return [{**record,**parsed,'text_extracted_at':store.now(),'document_sha256':hashlib.sha256(content).hexdigest()}]

def collect(src,params):
    if not src['implemented']:raise SourceError("该接口尚未实现或确认，不能执行","unsupported")
    if src['provider']=='baostock':values=normalize_bs(src['operation'],bs_call(src['operation'],params,src['timeout']),params)
    elif src['provider']=='tencent':values=tx_call(src['operation'],params,src['timeout'])
    elif src['provider']=='cninfo':
        if src['operation']=='filing_financials':
            from research.filing_financials import collect as filing_financials
            values=filing_financials(params)
        elif src['operation']=='announcement_text':values=announcement_text(params,src['timeout'])
        else:values=announcements(params,src['timeout'])
    elif src['provider']=='sina' and src['operation']=='flow_history':
        from foundation.funds import fetch
        values=fetch(params,src['timeout'])
    elif src['provider']=='official' and src['operation']=='policies':
        from foundation.policy_feed import collect as policy_feed
        values=policy_feed(params,src['timeout'])
    else:raise SourceError("来源未批准使用","unsupported")
    checked_policy=src['dataset']=='policies' and getattr(values,'checked',False)
    if not values and src['dataset']!='announcements' and not checked_policy:raise SourceError("该范围没有返回记录：可能未发布、不支持或超出回溯窗口","empty")
    for value in values:ms.validate_record(src['dataset'],value)
    return values
