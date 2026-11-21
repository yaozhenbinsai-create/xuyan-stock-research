"""Bounded discovery of policy originals from verified government columns.

Discovery is frozen into the durable job's next-page parameters. Each unit
reads at most three originals; failures remain in the receipt, while valid
documents retain the existing policy parser's paragraph anchors and hash.
"""
from __future__ import annotations

from datetime import date, timedelta
from hashlib import sha256
from html import unescape
import math
import re
import time
from threading import Lock
from urllib.parse import urljoin, urlsplit

from fastapi import HTTPException

from foundation import assets, database as store, sources
from research import originals


FEEDS = {
    'gov': {'url':'https://www.gov.cn/zhengce/', 'name':'中国政府网政策栏目'},
    'mee': {'url':'https://www.mee.gov.cn/zcwj/', 'name':'生态环境部政策文件栏目'},
}
MAX_ORIGINALS = 80
PAGE_SIZE = 3
MIN_INTERVAL = 2.0
MAX_BYTES = 1_000_000
TERMS = ('人工智能','集成电路','半导体','智能制造','机器人','算力','数字经济',
         '新能源汽车','新能源','储能','氢能','光伏','风电','核能','电力',
         '碳排放','绿色低碳','钢铁','有色金属','水泥','石油化工','化学工业',
         '化工行业','化工企业','化工产业','化工园区','化工产品','化学物质','化学品','稀土','生物医药',
         '污染防治','资源循环','节能','环保')
_ANCHOR = re.compile(r'<a\b[^>]*?href=["\x27]([^"\x27]+)["\x27][^>]*>(.*?)</a>', re.I|re.S)
_DATE = re.compile(r'(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})(?:日)?')
_ARTICLE_PATHS = {
    'www.gov.cn': re.compile(r'^/zhengce/(?:content/)?\d{6}/content_\d+\.htm$'),
    'www.mee.gov.cn': re.compile(r'^/xxgk2018/xxgk/[^/]+/\d{6}/t\d{8}_\d+\.html$'),
}
_request_times = {}
_pace_lock = Lock()


class PolicyBatch(list):
    def __init__(self, values=(), *, diagnostics=(), checked=False, page_info=None):
        super().__init__(values)
        self.diagnostics = list(diagnostics)
        self.checked = checked
        self.page_info = page_info


def _failure(message, errors):
    kind = next((item['kind'] for item in errors if item.get('kind') in ('network','timeout','rate_limit')),
                errors[0].get('kind', 'upstream'))
    detail = '；'.join(item['url']+': '+item['message'] for item in errors)[:700]
    error = sources.SourceError(message+'；'+detail, kind)
    error.diagnostics = errors
    return error


class _Pacer:
    def wait(self, url):
        host = urlsplit(url).hostname
        # Keep spacing across durable page jobs in the same worker as well.
        with _pace_lock:
            remaining = MIN_INTERVAL - (time.monotonic()-_request_times.get(host, -math.inf))
            if remaining > 0:
                time.sleep(remaining)
            _request_times[host] = time.monotonic()


def article_url(value, base=None):
    """Only canonical article paths on the two inspected HTTPS origins."""
    if not isinstance(value, str):
        return None
    value = urljoin(base, unescape(value)) if base else value
    try:
        canonical = originals.official_url(value)
        parts = urlsplit(canonical)
        pattern = _ARTICLE_PATHS.get(parts.hostname)
        return canonical if pattern and pattern.fullmatch(parts.path) else None
    except (ValueError, HTTPException):
        return None


def _day(value):
    match = _DATE.search(unescape(re.sub(r'<[^>]+>', ' ', value)))
    if not match:
        return None
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return None


def discover(html, feed, start, end):
    base = FEEDS[feed]['url']
    found = {}
    recognized = 0
    for match in _ANCHOR.finditer(html):
        url = article_url(match[1], base)
        if not url:
            continue
        recognized += 1
        suffix = html[match.end():match.end()+220]
        suffix = suffix.split('<a', 1)[0]
        listed = _day(suffix)
        if listed and not start <= listed <= end:
            continue
        found.setdefault(url, listed or '')
    if not recognized:
        raise sources.SourceError(FEEDS[feed]['name']+'未发现可识别的原文链接，保留来源结构异常', 'schema')
    return [url for url, _ in sorted(found.items(), key=lambda item:item[1], reverse=True)]


def _read(url, timeout, pacer):
    pacer.wait(url)
    response = sources.http('GET', url, timeout=min(12, timeout), trust_env=False)
    if 'html' not in response.headers.get('content-type', '').lower():
        raise sources.SourceError('官方栏目或原件未返回 HTML 正文', 'schema')
    if len(response.content) > MAX_BYTES:
        raise sources.SourceError('官方页面超过1MB读取上限', 'schema')
    return response.content.decode(response.encoding or 'utf-8', errors='replace')


def parse_article(html, url):
    canonical = article_url(url)
    if canonical is None:
        raise sources.SourceError('原文链接不在已核实的官方政策路径内', 'unsupported')
    # These are presentation wrappers observed on the two official sites. The
    # paragraph/content parser and publication metadata remain the existing one.
    if re.search(r'<h1\b', html, re.I):
        html = re.sub(r'<title\b[^>]*>.*?</title>', '', html, flags=re.I|re.S)
    elif urlsplit(canonical).hostname == 'www.gov.cn':
        # GOV regulation pages use the browser title as fallback, with their
        # navigation category and site name appended after the document title.
        html = re.sub(r'(?:_[^_<]+)?_中国政府网(?=\s*</title>)', '', html, flags=re.I)
    if urlsplit(canonical).hostname == 'www.mee.gov.cn':
        html = re.sub(r'\bCustom_UnionStyle\b', 'article-content', html)
    try:
        value = originals.parse_policy(html)
    except HTTPException as exc:
        raise sources.SourceError(str(exc.detail), 'parse') from None
    if value['published'] > store.today():
        raise sources.SourceError('原文发布日期晚于当前日期，未登记未来政策', 'schema')
    value.update(day=value['published'], symbol='*', key=sha256(canonical.encode()).hexdigest(), url=canonical)
    value['_terms'] = [term for term in TERMS if term in value['text']][:6]
    return value


def collect(params, timeout=15):
    end = assets.day(params.get('end') or store.today())
    start = assets.day(params.get('start') or (date.fromisoformat(end)-timedelta(days=29)).isoformat())
    if start > end or end > store.today():
        raise sources.SourceError('政策采集日期范围无效', 'schema')
    page = params.get('page', 1)
    if type(page) is not int or not 1 <= page <= math.ceil(MAX_ORIGINALS/PAGE_SIZE):
        raise sources.SourceError('政策原文分页无效', 'schema')
    pacer = _Pacer()
    diagnostics = []
    supplied = params.get('policy_snapshot')
    if page > 1 and supplied is None:
        raise sources.SourceError('政策后续页缺少已冻结的原文快照', 'schema')
    if supplied is None:
        urls = []
        for feed, config in FEEDS.items():
            try:
                candidates = discover(_read(config['url'], timeout, pacer), feed, start, end)
                urls.extend(candidates)
                diagnostics.append({'status':'checked','url':config['url'],'candidates':len(candidates)})
            except sources.SourceError as exc:
                diagnostics.append({'status':'error','url':config['url'],'kind':exc.kind,'message':str(exc)})
        if not any(item['status']=='checked' for item in diagnostics):
            raise _failure('官方政策栏目均未读取成功，未登记空成功', diagnostics)
        urls = list(dict.fromkeys(urls))
        if len(urls) > MAX_ORIGINALS:
            diagnostics.append({'status':'limited','message':'本次栏目原件超过80篇，保留读取边界',
                                'remaining':len(urls)-MAX_ORIGINALS})
            urls = urls[:MAX_ORIGINALS]
    else:
        if (not isinstance(supplied, list) or not supplied or len(supplied)>MAX_ORIGINALS
                or any(article_url(url)!=url for url in supplied)):
            raise sources.SourceError('政策分页快照包含非官方原文或范围无效', 'schema')
        urls = list(dict.fromkeys(supplied))
    if not urls:
        return PolicyBatch(diagnostics=[*diagnostics, {'status':'checked_no_new','message':'已核对栏目，范围内没有原文'}], checked=True)
    pages = math.ceil(len(urls)/PAGE_SIZE)
    if page > pages:
        raise sources.SourceError('政策分页超出已冻结原文范围', 'schema')
    selected = urls[(page-1)*PAGE_SIZE:page*PAGE_SIZE]
    records = []
    for url in selected:
        try:
            record = parse_article(_read(url, timeout, pacer), url)
            if start <= record['published'] <= end:
                records.append(record)
            else:
                diagnostics.append({'status':'outside_range','url':url,'published':record['published']})
        except sources.SourceError as exc:
            diagnostics.append({'status':'error','url':url,'kind':exc.kind,'message':str(exc)})
    errors = [item for item in diagnostics if item['status']=='error' and item['url'] in selected]
    if not records and len(errors)==len(selected):
        raise _failure('本批官方政策原件均未读取成功，保留真实失败', errors)
    info = {'page':page, 'pages':pages, 'total':len(urls), 'next':page+1 if page<pages else None,
            'next_params':{'policy_snapshot':urls}}
    return PolicyBatch(records, diagnostics=diagnostics, checked=True, page_info=info)
