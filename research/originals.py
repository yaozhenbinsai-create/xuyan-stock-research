"""Official policy pages and anchored filing evidence for a frozen research run."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from html.parser import HTMLParser
import re
from threading import Lock
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import HTTPException

from foundation import assets, database as store, retention, storage_policy, record_store


POLICY_HOSTS = {'www.gov.cn','www.mee.gov.cn','www.most.gov.cn','www.miit.gov.cn','www.mofcom.gov.cn'}
ARTICLE_ROOTS = {'ucap-content','pages_content','trs_editor','trs_editor_view','article-content','article-content-main',
                 'article_content','con_con','gsj_htmlcon_bot'}
BLOCKS = {'div','p','li','section','h2','h3','td'}
PAGE = re.compile(r'【(PDF 第 \d+ 页|原文第 \d+ 段)】\s*')
FILING = re.compile(r'年度报告|半年度报告|季度报告|财务报告|业绩快报')
BUSINESS = re.compile(r'主营业务|主要业务|分产品|分行业|营业收入构成|收入占比|主要产品')
FISCAL = re.compile(r'(20\d{2})\s*年\s*(第一季度|一季度|半年度|第三季度|三季度|年度)')
_FILING_TITLE_PERIOD = re.compile(
    r'(?:(?P<year>20\d{2})\s*年\s*(?P<period>第一季度|一季度|半年度|第三季度|三季度|年度|度)'
    r'|(?P<bare>第一季度|一季度|半年度|第三季度|三季度|年度))\s*报告'
    r'\s*(?:全文|正文|摘要)?\s*(?:[（(][^()（）]{0,40}[)）]\s*)?$')
DATE = re.compile(r'(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})(?:日)?')
UNITS = {'元':Decimal(1),'千元':Decimal(1000),'万元':Decimal(10000),'亿元':Decimal(100000000)}
NUMERIC = re.compile(r'^[\s:：]*(?:人民币)?\s*([-−(（]?[\d,，]+(?:\.\d+)?[)）]?)')
METRICS = {'主营业务收入':'main_revenue','营业收入':'operating_revenue',
           '归属于上市公司股东的净利润':'parent_profit','归属于母公司股东的净利润':'parent_profit',
           '经营活动产生的现金流量净额':'operating_cash'}
_Q3_COLUMNS = re.compile(
    r'(?:项目)?本报告期(?:\(7[-—至]9月\))?本报告期比上年同期'
    r'(?:增减(?:变动)?(?:幅度)?|增长率?)(?:\([%％]\))?'
    r'年初至报告期末(?:\(1[-—至]9月\))?年初至报告期末比上年同期'
    r'(?:增减(?:变动)?(?:幅度)?|增长率?)(?:\([%％]\))?')
_YTD_CASH_COLUMNS = re.compile(
    r'合并年初(?:到|至)报告期末现金流量表单位[:：](?:人民币)?(?P<unit>亿元|万元|千元|元)'
    r'(?:币种[:：]人民币)?项目本期发生额上期发生额')
_CELL = re.compile(r'\s*(?:(?P<number>[-−+]?\d[\d,，]*(?:\.\d+)?|[（(]\d[\d,，]*(?:\.\d+)?[)）])'
                   r'(?P<percent>\s*[%％])?|(?P<missing>不适用|—|–|--|-(?!\d)))')
_request_lock = Lock()


def filing_title_period(title):
    """Identify an actual periodic report, including explicitly yearless titles.

    A fiscal label alone also occurs in operations, impairment and investor
    meeting announcements. Those documents are not the periodic filing.
    """
    title = re.sub(r'[_＿]+', ' ', title or '').strip()
    if any(word in title for word in ('第三支柱', '资本管理', '英文', 'H股',
                                     '关于', '审议', '审核', '提示性公告')):
        return None
    match = _FILING_TITLE_PERIOD.search(title)
    if not match:return None
    label=match['period'] or match['bare']
    return match['year'], '年度' if label=='度' else label


class PolicyPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.meta = {}
        self.title = []
        self.header = []
        self.paragraphs = []
        self.current = []

    def _flush(self):
        text=' '.join(''.join(self.current).split())
        if text:self.paragraphs.append(text)
        self.current=[]

    def handle_starttag(self, tag, attributes):
        a = dict(attributes)
        if tag == 'meta':
            key = (a.get('name') or a.get('property') or '').lower()
            if a.get('content'):
                self.meta[key] = a['content']
        if (tag in BLOCKS or tag=='br') and any(x[1] for x in self.tags):
            self._flush()
        if tag in ('meta','link','img','input','br','hr','source','wbr'):
            return
        classes = set((a.get('class') or '').lower().split())
        identifier = (a.get('id') or '').lower()
        body = identifier in ARTICLE_ROOTS or bool(classes & ARTICLE_ROOTS)
        header = identifier in {'pubtime','pubdate','con_time'} or bool(classes & {'pages-date','article-date','pubtime','article-time'})
        self.tags.append((tag,body,header))

    def handle_endtag(self, tag):
        if tag in BLOCKS and any(x[1] for x in self.tags):
            self._flush()
        for index in range(len(self.tags)-1,-1,-1):
            if self.tags[index][0] == tag:
                del self.tags[index:]
                break

    def handle_data(self, data):
        tags = {x[0] for x in self.tags}
        if tags & {'script','style','noscript'}:
            return
        if 'title' in tags or 'h1' in tags:
            self.title.append(data)
        if any(x[2] for x in self.tags):
            self.header.append(data)
        if any(x[1] for x in self.tags):
            self.current.append(data)


def _day(text):
    match = DATE.search(text or '')
    if not match:
        return None
    try:
        return date(*map(int,match.groups())).isoformat()
    except ValueError:
        return None


def parse_policy(html):
    page = PolicyPage()
    page.feed(html)
    page.close()
    title = page.meta.get('articletitle') or page.meta.get('og:title') or ''.join(page.title)
    title = ' '.join(title.split())[:240]
    dates=[_day(text) for text in [page.meta.get(k) for k in ('pubdate','articlepubdate','publishdate','firstpublishedtime','article:published_time','createtime')] + [' '.join(page.header)]]
    published=max((value for value in dates if value),default=None)
    body = [(i,text) for i,text in enumerate(page.paragraphs,1) if len(text)>3]
    if not published or not title or sum(len(x[1]) for x in body)<100:
        raise HTTPException(422,'该页面缺少可核的发布日期或政策正文')
    canonical = '\n'.join(text for _,text in body)
    end = re.search(r'自\s*(20\d{2}年\d{1,2}月\d{1,2}日)\s*起(?:施行|实施|执行|生效)', canonical)
    text = '\n'.join(f'【原文第 {i} 段】\n{paragraph}' for i,paragraph in body)[:11500]
    return {'title':title,'published':published,'text':text,'effective_day':_day(end[1]) if end else None,
            'document_sha256':sha256(store.dumps([title,published,canonical]).encode()).hexdigest(),
            'text_scope':'按政策原文段落保留至多11500字'}


def official_url(value):
    if not isinstance(value,str) or len(value)>400:
        raise HTTPException(422,'请填写官方政策正文链接')
    try:
        parts=urlsplit(value.strip())
        valid = parts.scheme=='https' and parts.netloc.lower() in POLICY_HOSTS and parts.path.endswith(('.htm','.html','.shtml'))
    except ValueError:
        valid=False
    if not valid:
        raise HTTPException(422,'仅支持已接入的政府网或中央部门 HTTPS 政策正文链接')
    return urlunsplit(('https',parts.netloc.lower(),parts.path,'',''))


def _download(url):
    if not _request_lock.acquire(blocking=False):
        raise HTTPException(409,'上一份政策原件仍在获取')
    try:
        with httpx.Client(timeout=12,follow_redirects=False,trust_env=False) as client:
            with client.stream('GET',url,headers={'User-Agent':'StockResearch/0.3 local research'}) as response:
                if response.status_code!=200 or 'html' not in response.headers.get('content-type','').lower():
                    raise HTTPException(502,'官方地址未返回可读取的政策正文')
                data=bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data)>1_000_000:
                        raise HTTPException(422,'政策页面超出本次读取上限')
                return data.decode(response.encoding or 'utf-8',errors='replace')
    except (httpx.HTTPError, LookupError):
        raise HTTPException(502,'官方原件读取失败，未登记证据') from None
    finally:
        _request_lock.release()


def import_policy(raw):
    if set(raw)-{'url','terms'}:
        raise HTTPException(422,'政策原件包含未知字段')
    url=official_url(raw.get('url'))
    terms=raw.get('terms')
    if not isinstance(terms,list) or not 1<=len(terms)<=6 or any(not isinstance(t,str) or not 2<=len(t.strip())<=24 for t in terms):
        raise HTTPException(422,'填写1至6个可在原文中定位的产业或产品术语')
    terms=list(dict.fromkeys(t.strip() for t in terms))
    original=parse_policy(_download(url))
    if original['published']>store.today() or any(term not in original['text'] for term in terms):
        raise HTTPException(422,'术语须出现在已公开原文的保留范围内')
    key=sha256(url.encode()).hexdigest()
    value={**original,'day':original['published'],'symbol':'*','key':key,'url':url}
    retention.ensure_capacity('policies')
    with store.db() as conn:
        identifier,inserted=assets.insert_fact(conn,'policies','official_policy',value)
        changed=getattr(conn,'heads_changed',False)
        if changed:assets.bump_generation(conn,'policies')
        if inserted:retention.account(conn,'policies',len(store.dumps(storage_policy.normalize('policies',value)).encode()),1)
        name=f'policy_terms:{identifier}'
        if store.setting(name,[],conn=conn)!=terms:
            store.set_setting(name,terms,conn=conn)
            assets.bump_generation(conn,'policy_terms')
    if changed:assets.refresh_stats('policies')
    return {'fact_id':identifier,'title':original['title'],'published':original['published'],'added':int(inserted),'terms':terms}


def policies(conn, cutoff, published_through=None, limit=25, before=None):
    where="h.dataset='policies' AND f.first_seen<=%s AND h.day<=%s"
    params=[cutoff,published_through or cutoff[:10]]
    if before:
        row=conn.execute("SELECT day,id FROM facts WHERE id=%s AND dataset='policies'",(before,)).fetchone()
        if not row:raise HTTPException(409,'政策分页游标已失效')
        where+=' AND (h.day,f.id)<(%s,%s)';params.extend(row)
    rows=conn.execute('SELECT f.id FROM heads h JOIN facts f ON h.fact_id=f.id WHERE '+where+
                      ' ORDER BY h.day DESC,f.id DESC LIMIT %s',(*params,limit+1)).fetchall()
    items=[{**row['payload'],'id':row['id'],'terms':store.setting(f"policy_terms:{row['id']}",[],conn=conn)} for row in record_store.hydrate(conn,rows[:limit])]
    return {'items':items,'next':items[-1]['id'] if len(rows)>limit else None}


def list_policies(limit,before):
    with store.db(readonly=True) as conn:
        result=policies(conn,store.now(),limit=limit,before=before)
    for item in result['items']:
        item.pop('text',None)
    return result


def _anchor(fact, start, end, label):
    body=fact['text']
    quote=body[start:end]
    location=next((m[1] for m in reversed(list(PAGE.finditer(body,0,start)))), '原文节选')
    return {'id':f"{fact['id']}:{start}:{end}",'fact_id':fact['id'],'start':start,'end':end,
            'location':location,'label':label,'sha256':sha256(quote.encode()).hexdigest(),'url':fact.get('url')}


def _passage(fact, start, end, label):
    body=fact['text']
    left=max(body.rfind('\n',0,start)+1,start-90,0)
    boundary=body.find('\n',end)
    right=min(boundary if boundary>=0 else len(body),end+160)
    return _anchor(fact,left,right,label)


def _financial_rows(fact):
    identified=filing_title_period(fact['title'])
    if not identified or not identified[0]:
        return []
    period=FISCAL.search(f'{identified[0]}年{identified[1]}报告')
    body=fact['text'];rows=[]
    code=period[1]+'-'+{'第一季度':'03-31','一季度':'03-31','半年度':'06-30','第三季度':'09-30','三季度':'09-30','年度':'12-31'}[period[2]]
    for label,metric in METRICS.items():
        # PDF extraction often wraps Chinese account labels across lines.
        for match in re.finditer(r'\s*'.join(map(re.escape,label)),body):
            # Use explicit currency units from the same table page. Exclude explanations and percent-only lines.
            page_start=max((m.end() for m in PAGE.finditer(body,0,match.start())),default=0)
            unit=re.findall(r'单位\s*[:：]\s*(?:人民币\s*)?(亿元|万元|千元|元)(?![\u4e00-\u9fff])',body[max(page_start,match.start()-650):match.start()])
            tail_start=match.end()
            tail=body[tail_start:tail_start+180]
            row_unit=re.match(r'\s*[（(]\s*(?:人民币\s*)?(亿元|万元|千元|元)\s*[)）]',tail)
            if row_unit:
                unit=[row_unit[1]];tail_start+=row_unit.end();tail=body[tail_start:tail_start+180]
            context=body[max(page_start,match.start()-650):match.start()]
            line_start=body.rfind('\n',0,match.start())+1
            row_like=re.fullmatch(r'\s*(?:其中\s*[:：])?\s*',body[line_start:match.start()]) is not None
            if metric=='operating_cash' and code.endswith('09-30') and row_like:
                # This narrow two-column case is proved by an explicit YTD
                # consolidated cash-flow title, currency unit and column header
                # on the same page. It never borrows a previous page's heading.
                cash_start=max(page_start,match.start()-2400)
                cash_context=body[cash_start:match.start()]
                cash_positions=[i for i,ch in enumerate(cash_context) if not ch.isspace()]
                cash_compact=''.join(cash_context[i] for i in cash_positions)
                cash_headers=list(_YTD_CASH_COLUMNS.finditer(cash_compact))
                cash_header=cash_headers[-1] if cash_headers else None
                if cash_header and re.search(r'单位[:：]|(?:母公司|合并).{0,24}(?:现金流量表|利润表|资产负债表)|项目本期发生额上期发生额',
                                             cash_compact[cash_header.end():]):
                    cash_header=None
                if cash_header and (not row_unit or row_unit[1]==cash_header['unit']):
                    cells=[];offset=0
                    for _ in range(2):
                        cell=_CELL.match(tail,offset)
                        if not cell:break
                        cells.append(cell);offset=cell.end()
                    if len(cells)==2 and not any(cell['percent'] for cell in cells) and not _CELL.match(tail,offset):
                        if not cells[0]['missing']:
                            amount=_amount_yuan(cells[0]['number'],cash_header['unit'])
                            if amount is not None:
                                rows.append({'metric':metric,'label':label,'period':code,'basis':'cumulative',
                                             'amount_yuan':str(amount),'document':fact['id'],
                                             'anchor':_anchor(fact,cash_start+cash_positions[cash_header.start()],
                                                               tail_start+cells[0].end(),label)})
                        break
            if not unit:continue
            if code.endswith('09-30'):
                # Only a complete, ordered same-page header plus four typed
                # cells proves which current-year amount is quarterly or YTD.
                positions=[i for i,ch in enumerate(context) if not ch.isspace()]
                compact=''.join(context[i] for i in positions).replace('（','(').replace('）',')')
                header=_Q3_COLUMNS.search(compact)
                cells=[];offset=0
                if header and row_like:
                    for _ in range(4):
                        cell=_CELL.match(tail,offset)
                        if not cell:break
                        cells.append(cell);offset=cell.end()
                    valid=(len(cells)==4 and all(not cells[i]['percent'] for i in (0,2))
                           and all(cells[i]['missing'] or cells[i]['percent'] or '(%)' in header[0] or '(％)' in header[0]
                                   for i in (1,3)) and not _CELL.match(tail,offset))
                    if valid:
                        header_start=max(page_start,match.start()-650)+positions[header.start()]
                        for index,basis in ((0,'quarterly'),(2,'cumulative')):
                            cell=cells[index]
                            if cell['missing']:continue
                            amount=_amount_yuan(cell['number'],unit[-1])
                            if amount is None:continue
                            rows.append({'metric':metric,'label':label,'period':code,'basis':basis,
                                         'amount_yuan':str(amount),'document':fact['id'],
                                         'anchor':_anchor(fact,header_start,tail_start+cell.end(),label)})
                        break
            number=NUMERIC.match(tail)
            if not number or tail[number.end():].lstrip().startswith(('%','％')):
                continue
            amount=_amount_yuan(number[1],unit[-1])
            if amount is None:continue
            # A Q3 row needs an explicit year-to-date qualifier before it is compared to a cumulative report.
            context=body[max(page_start,match.start()-420):match.start()]
            basis='cumulative' if code.endswith(('03-31','12-31')) or '年初至报告期末' in context and '本报告期' not in context else 'undetermined'
            if code.endswith('09-30') and (not row_like or metric=='operating_cash'):basis='undetermined'
            rows.append({'metric':metric,'label':label,'period':code,'basis':basis,'amount_yuan':str(amount),
                         'document':fact['id'],'anchor':_anchor(fact,match.start(),tail_start+number.end(),label)})
            break
    return rows


def _amount_yuan(literal,unit):
    literal=literal.replace(',','').replace('，','').replace('−','-').replace('（','(').replace('）',')')
    try:
        return Decimal('-'+literal[1:-1] if literal.startswith('(') and literal.endswith(')') else literal)*UNITS[unit]
    except InvalidOperation:
        return None


def _original_filings(conn,symbol,knowledge,publication):
    rows=conn.execute("SELECT f.id FROM heads h JOIN facts f ON f.id=h.fact_id JOIN announcements a ON a.fact_id=f.id WHERE h.dataset='announcements' "
                      "AND h.symbol=%s AND h.day<=%s AND f.first_seen<=%s AND f.source='cninfo_text' "
                      "AND a.title REGEXP '报告|业绩快报' "
                      "ORDER BY h.day DESC,f.id DESC LIMIT 10",(symbol,publication[:10],knowledge)).fetchall()
    facts=[]
    for row in record_store.hydrate(conn,rows):
        value=row['payload']
        if not value.get('text') or not FILING.search(value['title']) or len(value.get('published',''))>10 and value['published']>publication:
            continue
        facts.append({**value,'id':row['id']})
    return facts[:4]


def packet(conn,symbol,knowledge,publication,policy_rows):
    filings=_original_filings(conn,symbol,knowledge,publication)
    anchors={}
    comparisons=[]
    metrics=[]
    exposure=[]
    for filing in filings:
        for row in _financial_rows(filing):
            if len(metrics)>=14:break
            anchor=row.pop('anchor');anchors[anchor['id']]=anchor
            metrics.append({**row,'anchor_id':anchor['id']})
        text=filing['text']
        for business in list(BUSINESS.finditer(text))[:22]:
            context=text[business.start():business.end()+240]
            for policy in policy_rows:
                term=next((term for term in policy['terms'] if term in context and term in policy['text']),None)
                if not term or any(x['term']==term and x['policy_id']==policy['id'] for x in exposure) or len(exposure)>=4:
                    continue
                at=text.find(term,business.start(),business.end()+240)
                pa=policy['text'].find(term)
                a=_anchor(filing,business.start(),min(len(text),at+len(term)+55),'公司业务披露')
                b=_passage(policy,pa,pa+len(term),'政策适用条文')
                anchors[a['id']]=a;anchors[b['id']]=b
                exposure.append({'term':term,'policy_id':policy['id'],'filing_id':filing['id'],'effective_day':policy.get('effective_day'),
                                 'policy_published':policy['published'],'policy_title':policy['title'],
                                 'anchors':[b['id'],a['id']],'status':'candidate_link','basis':'业务原文与政策术语有交集，适用条件与经济影响须逐项核对'})
    for current in metrics:
        if current['basis']!='cumulative':continue
        comparison=next((old for old in metrics if old['document']!=current['document'] and
                         (old['period'],old['metric'],old['basis'])==(current['period'],current['metric'],current['basis'])),None)
        if not comparison or any(x['current']['metric']==current['metric'] and x['current']['period']==current['period'] for x in comparisons):continue
        difference=Decimal(current['amount_yuan'])-Decimal(comparison['amount_yuan'])
        comparisons.append({'current':current,'previous':comparison,'delta_yuan':str(difference),'state':'matches' if difference==0 else 'differs'})
    return {'financial_fields':metrics,'comparisons':comparisons,'industry_links':exposure,'anchors':list(anchors.values()),
            'filings':[{'id':f['id'],'title':f['title'],'url':f.get('url'),'document_sha256':f.get('document_sha256')} for f in filings],
            'basis':'数字按明确单位和同期间口径核对；未抽取页与未确定口径分别保留。'}


def attach_quotes(conn, packet):
    anchors=packet.get('anchors',[])
    ids=sorted({a['fact_id'] for a in anchors})
    if not ids:return packet
    values=record_store.load_many(conn,ids)
    texts={fid:payload.get('text','') for fid,payload in values.items()}
    result=[]
    for anchor in anchors:
        quote=texts.get(anchor['fact_id'],'')[anchor['start']:anchor['end']]
        result.append({**anchor,'quote':quote if sha256(quote.encode()).hexdigest()==anchor['sha256'] else None})
    return {**packet,'anchors':result}
