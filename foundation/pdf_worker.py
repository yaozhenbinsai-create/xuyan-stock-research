"""Bounded subprocess for announcement text extraction; no external PDF actions."""
import io,json,re,sys,time
from pypdf import PdfReader

def _words(values):
    return '|'.join(r'\s*'.join(map(re.escape,word)) for word in values)


SUMMARY_MARKERS=re.compile(_words(('主要会计数据','主要财务数据')))
ACCOUNT_MARKERS=re.compile(_words(('主营业务收入','营业收入','归属于上市公司股东的净利润',
                                  '归属于母公司股东的净利润','经营活动产生的现金流量净额')))
BUSINESS_MARKERS=re.compile(_words(('主营业务','营业收入构成','分产品','分行业')))
FILING_MARKERS=re.compile('|'.join((SUMMARY_MARKERS.pattern,ACCOUNT_MARKERS.pattern,BUSINESS_MARKERS.pattern)))
TABLE_CONTEXT=re.compile(r'单位\s*[:：]|项目\s+(?:本报告期|本期发生额)|合并[^\n]{0,24}(?:利润表|现金流量表)')


def _merge(ranges):
    merged=[]
    for start,end in sorted(ranges):
        if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
        else:merged.append((start,end))
    return merged


def _account_row(text,match):
    start=text.rfind('\n',0,match.start())+1
    return re.fullmatch(r'\s*(?:(?:[一二三四五六七八九十\d]+[、.．])|其中\s*[:：])?\s*',text[start:match.start()]) is not None


def _financial_range(text,match):
    # Compact financial pages retain their actual same-page table context.
    if len(text)<=2400:return (0,len(text))
    start=max(0,match.start()-560)
    floor=max(0,match.start()-2400)
    context=list(TABLE_CONTEXT.finditer(text,floor,match.start()))
    if context:start=min(start,context[0].start())
    elif match.start()<2400:start=0
    end=min(len(text),match.end()+300)
    line_end=text.find('\n',end)
    if line_end>=0:end=line_end
    return start,end


def extract_filing(reader):
    candidates=[];chars=0;scanned=0
    deadline=time.monotonic()+25
    for index,page in enumerate(reader.pages):
        if index>=80 or time.monotonic()>=deadline:break
        scanned=index+1
        stream=page.get_contents()
        if stream and len(stream.get_data())>8_000_000:continue
        text=(page.extract_text() or '')[:24000]
        if not text.strip():continue
        summaries=list(SUMMARY_MARKERS.finditer(text))[:8]
        accounts=[m for m in ACCOUNT_MARKERS.finditer(text) if _account_row(text,m)][:8]
        financial=_merge([_financial_range(text,m) for m in summaries+accounts])
        for start,end in financial:candidates.append((0 if summaries else 1,index,start,end,text))
        for start,end in _merge([(max(0,m.start()-560),min(len(text),m.end()+300))
                                 for m in list(FILING_MARKERS.finditer(text))[:8]]):
            candidates.append((2,index,start,end,text))
        if index==0:candidates.append((3,index,0,min(len(text),650),text))
    selected=[];covered={}
    # Do not let early generic business mentions spend the entire text budget
    # before later financial summaries and accounting rows have been considered.
    for _,index,start,end,text in sorted(candidates,key=lambda item:item[:4]):
        fragments=[(start,end)]
        for left,right in covered.get(index,[]):
            fragments=[piece for a,b in fragments for piece in
                       ([(a,min(b,left))] if a<left else [])+([(max(a,right),b)] if b>right else [])
                       if piece[0]<piece[1]]
        for start,end in fragments:
            marker=f'【PDF 第 {index+1} 页】\n'
            available=11000-chars-len(marker)
            if available<=35:continue
            if end-start>available:
                # A budget cut must not turn a partial amount into a full cell.
                end=text.rfind('\n',start,start+available)
                if end<=start:continue
            part=marker+text[start:end]
            if len(part)>35:
                selected.append((index,start,part));chars+=len(part)+1
                covered[index]=_merge(covered.get(index,[])+[(start,end)])
    parts=[part for _,_,part in sorted(selected)]
    return {'text':'\n'.join(parts),'text_pages':len({part.split('\n',1)[0] for part in parts}),
            'total_pages':len(reader.pages),'text_scope':f'检查前{scanned}页，优先财务摘要及会计行的同页表头、单位和原文上下文；最多11000字'}


def extract(content, filing=False):
    reader=PdfReader(io.BytesIO(content));parts=[];used=0
    if filing:return extract_filing(reader)
    for i,page in enumerate(reader.pages):
        if i>=30 or used>=18000:break
        # Decompressed pathological streams can exhaust memory before text extraction.
        stream=page.get_contents()
        if stream and len(stream.get_data())>8_000_000:continue
        text=(page.extract_text() or '')[:18000-used]
        parts.append(f'【PDF 第 {i+1} 页】\n{text}');used+=len(text)
    return {'text':'\n'.join(parts),'text_pages':len(parts),'total_pages':len(reader.pages),'text_scope':'仅前 30 页，最多 18000 字；图片、扫描件及未抽取页面不在证据范围内'}

if __name__=='__main__':
    try:print(json.dumps(extract(sys.stdin.buffer.read(20_000_001),filing='--filing' in sys.argv[1:]),ensure_ascii=True))
    except Exception:print(json.dumps({'error':'PDF 文本无法提取，保留原文链接'}))
