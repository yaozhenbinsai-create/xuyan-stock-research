from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta

from fastapi import HTTPException
from foundation import database as store, storage_policy, pagination, record_store

SHANGHAI = store.SHANGHAI
DATASETS = {
    "daily": "日线行情", "quotes": "行情快照", "basics": "证券名录", "industry": "行业分类",
    "financials": "财务指标", "announcements": "公司公告", "minutes": "分钟行情",
    "fund_flows": "资金流向", "calendar": "交易日历", "actions": "复权资料", "concept": "概念成员", "area": "地区分类", "policies": "政策原件",
}
SOURCE_DATASETS = {
    "bs_calendar": "calendar", "bs_basics": "basics", "bs_universe": "basics", "bs_industry": "industry",
    "bs_daily_all": "daily", "bs_daily": "daily", "tx_daily": "daily", "tx_quotes": "quotes",
    "bs_minutes": "minutes", "bs_profit": "financials", "bs_balance": "financials",
    "bs_cashflow": "financials", "bs_growth": "financials", "cninfo_announcements": "announcements",
    "cninfo_text": "announcements", "cninfo_financials": "financials", "sina_flow_history": "fund_flows", "bs_actions": "actions", "official_policy": "policies",
}
ASHARE = re.compile(r"^(sh\.(?:60\d{4}|68\d{4})|sz\.(?:00\d{4}|30\d{4})|bj\.(?:43\d{4}|83\d{4}|87\d{4}|88\d{4}|92\d{4}))$")


def local_now():
    return datetime.now(SHANGHAI)


today = store.today
dumps = store.dumps
settings = store.setting
set_settings = store.set_setting


def day(value):
    try:
        result = date.fromisoformat(str(value))
        if not 1990 <= result.year <= 2100:
            raise ValueError()
        return result.isoformat()
    except (TypeError, ValueError):
        raise HTTPException(422, "日期须为 YYYY-MM-DD") from None


def symbol(value):
    value = str(value).lower().strip()
    compact = value.replace(".", "")
    if re.fullmatch(r"(?:sh|sz|bj)\d{6}", compact):
        code = compact[:2] + "." + compact[2:]
        if ASHARE.fullmatch(code):
            return code
    if re.fullmatch(r"\d{6}", value):
        with store.db(readonly=True) as c:
            found = c.execute("SELECT DISTINCT symbol FROM heads WHERE dataset='basics' AND symbol IN (%s,%s,%s)",
                              tuple(m + "." + value for m in ("sh", "sz", "bj"))).fetchall()
        if len(found) == 1:
            return found[0][0]
    raise HTTPException(422, "请输入有效的 A 股完整代码，例如 sh.600000")


def validate_record(dataset, value):
    if dataset not in DATASETS or not isinstance(value, dict):
        raise ValueError("不支持的数据类型")
    day(value.get("day"))
    code = value.get("symbol", "")
    if dataset == "calendar":
        if code != "*" or type(value.get("trading")) is not bool:
            raise ValueError("交易日历缺少交易状态")
    elif dataset == "policies":
        if code != '*' or not all(value.get(field) for field in ('published','title','text','url','key','document_sha256')):
            raise ValueError('政策原件缺少官方出处或正文')
    elif not ASHARE.fullmatch(code):
        raise ValueError("非 A 股证券或证券代码无效")
    if dataset in ("daily", "quotes", "minutes"):
        for k in ("open", "high", "low", "close", "volume", "amount"):
            n = value.get(k)
            if n is not None and (isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or n < 0):
                raise ValueError("行情字段无效：" + k)
        suspended = dataset == "daily" and value.get("suspended") is True
        if value.get("close") is None and not suspended:
            raise ValueError("缺少收盘价 / 报价")
        lo, hi = value.get("low"), value.get("high")
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("最低价高于最高价")
        if dataset in ("daily", "minutes") and not suspended:
            numbers = [value.get(k) for k in ("open", "high", "low", "close")]
            if any(x is None or x <= 0 for x in numbers):
                raise ValueError("OHLC 缺失或非正数")
            if not lo <= min(numbers[0], numbers[3]) <= max(numbers[0], numbers[3]) <= hi:
                raise ValueError("OHLC 大小关系异常")
        if value.get("adjust", "raw") != "raw":
            raise ValueError("暂只接收明确的未复权原始行情")
    if dataset == "financials":
        day(value.get("published"))
        day(value.get("report_date"))
        if value["published"] < value["report_date"]:
            raise ValueError("财务披露早于报告期")
    if dataset == "announcements" and (not value.get("title") or not value.get("key")):
        raise ValueError("公告缺少标题或公告编号")
    if dataset == "basics" and not value.get("name"):
        raise ValueError("证券名称缺失")
    if dataset in ("industry", "concept", "area") and not value.get("group"):
        raise ValueError("分类名称缺失")
    if dataset == "fund_flows":
        if value.get("unit") != "CNY" or not value.get("basis") or value.get("net_main") is None:
            raise ValueError("资金单位或统计口径缺失")
        ratio = value.get("ratio_main")
        if ratio is not None and (not isinstance(ratio, (int, float)) or not math.isfinite(ratio) or abs(ratio) > 1.001):
            raise ValueError("资金比例口径无效")
    if dataset == "quotes":
        stamp = value.get("source_time", "")
        if str(stamp)[:10] != value["day"]:
            raise ValueError("快照日期与来源时点不符")
    return value


def fingerprint(dataset, source, value):
    # Retrieval times are provenance, not a new market observation. Vendor
    # time, raw vendor fields, disclosure dates, and text remain in the hash.
    return storage_policy.content_hash(dataset, value)


def pin_pending_financial_original(c, fact_id, value):
    # Historical comparative filings can be older than the ordinary 90-day
    # announcement window when first acquired. Protect the exact filing until
    # typed derivation takes over, using the same 30-day recovery window as jobs.
    if value['day'] >= (local_now().date()-timedelta(days=storage_policy.POLICY['days']['announcements'])).isoformat():return
    from research.originals import filing_title_period
    period=filing_title_period(value.get('title',''))
    if not period:return
    year,label=period
    if value.get('text'):
        from research.filing_financials import _parsing_fact
        parsed,_=_parsing_fact({**value,'id':fact_id})
        if parsed:
            year,label=filing_title_period(parsed['title'])
        else:return
    endings={'第一季度':'03-31','一季度':'03-31','半年度':'06-30',
             '第三季度':'09-30','三季度':'09-30','年度':'12-31'}
    # An index may omit the fiscal year. Retain it until the PDF's explicit
    # heading resolves that year; never infer the year from publication day.
    owner=value['symbol']+':'+(year+'-'+endings[label] if year else 'unresolved:'+str(fact_id))
    c.execute('INSERT IGNORE INTO evidence_refs(owner_type,owner_id,fact_id,created) VALUES(%s,%s,%s,%s)',
              ('financial_pending',owner,fact_id,store.now()))


def has_cumulative_cny_revenue(fields):
    if fields.get('currency')!='CNY' or fields.get('period_basis')!='cumulative':return False
    try:return math.isfinite(float(fields.get('operating_revenue')))
    except (TypeError,ValueError):return False


def insert_fact(c, dataset, source, value, *, source_version="", observed=None, original_id=None, active=True):
    validate_record(dataset, value)
    if SOURCE_DATASETS.get(source) != dataset:
        raise ValueError("来源与数据类型不符")
    stamp = observed or store.now()
    key = str(value.get("key") or "")
    v = storage_policy.normalize(dataset, {**value, "key": key})
    digest = fingerprint(dataset, source, v)
    result = c.execute('INSERT INTO facts(dataset,day,symbol,record_key,source,source_version,payload_bytes,fingerprint,first_seen,last_seen,original_id,last_original_id) '
                       'VALUES(%s,%s,%s,%s,%s,%s,%s,UNHEX(%s),%s,%s,%s,%s) ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id),'
                       'last_seen=GREATEST(last_seen,VALUES(last_seen)),acquisitions=acquisitions+1,last_original_id=VALUES(last_original_id)',
                       (dataset, v["day"], v["symbol"], key, source, source_version, len(dumps(v).encode()), digest, stamp, stamp, original_id, original_id))
    inserted, fact_id = result.rowcount == 1, result.lastrowid
    if inserted:
        record_store.save(c, fact_id, dataset, v)
    if not inserted:
        original = c.execute('SELECT source,source_version,first_seen,last_seen FROM facts WHERE id=%s', (fact_id,)).fetchone()
        if original["source"] != source:
            for sid, sv, first, last in [(original["source"], original["source_version"], original["first_seen"], original["last_seen"]), (source, source_version, stamp, stamp)]:
                c.execute('INSERT INTO fact_origins VALUES(%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE last_seen=GREATEST(last_seen,VALUES(last_seen))', (fact_id, sid, sv, first, last))
    if active:
        head_id=fact_id
        if dataset=='announcements' and source=='cninfo_announcements' and not v.get('text'):
            current=c.execute('SELECT fact_id FROM heads '
                              'WHERE dataset=%s AND day=%s AND symbol=%s AND record_key=%s FOR UPDATE',
                              (dataset,v['day'],v['symbol'],key)).fetchone()
            if current:
                # Lock only the mutable head. Facts are immutable and can be
                # read without another row lock while an extraction is saved.
                original=record_store.load(c,current[0])
                if original.get('text') and all(original.get(field)==v.get(field) for field in
                                                ('day','symbol','key','title','published','url')):
                    # The index observation is still recorded above. Only an
                    # unchanged index may retain the richer extracted head;
                    # original payloads and their first_seen are never rewritten.
                    head_id=current[0]
        head = c.execute('INSERT INTO heads VALUES(%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE fact_id=VALUES(fact_id)', (dataset, v["day"], v["symbol"], key, head_id))
        if head.rowcount:
            c.heads_changed=True
    if dataset=='announcements' and source in ('cninfo_announcements','cninfo_text'):
        pin_pending_financial_original(c,fact_id,v)
    return fact_id, inserted


PUBLICATION_BATCH_SIZE = 200


def _write_batches(c, sql, rows):
    # Bound each database round trip without committing any part of a publish.
    affected = 0
    for start in range(0, len(rows), PUBLICATION_BATCH_SIZE):
        affected += c.executemany(sql, rows[start:start+PUBLICATION_BATCH_SIZE]).rowcount
    return affected


def _publish_bars(c, dataset, values, source, automatic):
    """Write one market-data batch atomically, retaining every acquisition."""
    holds = set()
    if automatic:
        days = sorted({value['day'] for value in values})
        for start in range(0, len(days), 500):
            batch = days[start:start+500]
            holds.update((row[0], row[1]) for row in c.execute(
                'SELECT day,symbol FROM collection_holds WHERE dataset=%s AND day IN ('
                + ','.join(['%s']*len(batch)) + ')', (dataset, *batch)))
    selected = [value for value in values if (value['day'], value['symbol']) not in holds
                and (value['day'], '*') not in holds]
    held = len(values) - len(selected)
    eligible = []
    for value in selected:
        validate_record(dataset, value)
        value = storage_policy.normalize(dataset, {**value, 'key': str(value.get('key') or '')})
        eligible.append((value, fingerprint(dataset, source, value),
                         len(dumps(value).encode()), store.now()))
    if not eligible:
        return 0, 0, held, 0
    if SOURCE_DATASETS.get(source) != dataset:
        raise ValueError("来源与数据类型不符")
    # Sort immutable catalogue locks; keep the original observation order for heads.
    affected = _write_batches(c,
        'INSERT INTO facts(dataset,day,symbol,record_key,source,source_version,payload_bytes,fingerprint,first_seen,last_seen,original_id,last_original_id) '
        'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE '
        'last_seen=GREATEST(last_seen,VALUES(last_seen)),acquisitions=acquisitions+1,last_original_id=VALUES(last_original_id)',
        [(dataset, value['day'], value['symbol'], value['key'], source, '', size, bytes.fromhex(digest), stamp, stamp, None, None)
         for value, digest, size, stamp in sorted(eligible, key=lambda item: item[1])])
    # Each new row affects one row; every duplicate increments acquisitions and
    # affects two. This stays correct when another publisher wins a unique key.
    inserted_count = 2*len(eligible) - affected
    hashes = sorted({digest for _, digest, _, _ in eligible})
    table = record_store.DATASET_TABLES[dataset]
    facts = {}
    for start in range(0, len(hashes), 500):
        batch = hashes[start:start+500]
        for row in c.execute(
                f'SELECT f.id,f.fingerprint,f.source,f.source_version,f.first_seen,f.last_seen,t.fact_id AS typed_id '
                f'FROM facts f LEFT JOIN `{table}` t ON t.fact_id=f.id WHERE f.fingerprint IN ('
                + ','.join(['UNHEX(%s)']*len(batch)) + ')', tuple(batch)):
            facts[bytes(row['fingerprint']).hex()] = dict(row)
    new = {digest for digest, row in facts.items() if row['typed_id'] is None}
    if len(facts) != len(hashes) or len(new) != inserted_count:
        raise RuntimeError('行情事实与业务表不一致，整批未提交')
    # Existing typed rows are immutable, including historical null/presence shape.
    payloads = {}
    for value, digest, size, stamp in eligible:
        if digest in new and digest not in payloads:
            payloads[digest] = (value, size)
    record_store.save_many(c, [(facts[digest]['id'], dataset, payloads[digest][0]) for digest in sorted(new)])
    origins = []
    for value, digest, size, stamp in eligible:
        row = facts[digest]
        if row['source'] != source:
            origins.extend([(row['id'], row['source'], row['source_version'], row['first_seen'], row['last_seen']),
                            (row['id'], source, '', stamp, stamp)])
    if origins:
        _write_batches(c, 'INSERT INTO fact_origins VALUES(%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE '
                       'last_seen=GREATEST(last_seen,VALUES(last_seen))', sorted(origins))
    heads = _write_batches(c, 'INSERT INTO heads VALUES(%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE fact_id=VALUES(fact_id)',
                          [(dataset, value['day'], value['symbol'], value['key'], facts[digest]['id'])
                           for value, digest, _, _ in sorted(eligible,
                               key=lambda item: (item[0]['day'], item[0]['symbol'], item[0]['key']))])
    if heads:
        c.heads_changed = True
    return inserted_count, len(eligible)-inserted_count, held, sum(size for _, size in payloads.values())


def publish(dataset, values, source, *, automatic=True):
    from foundation import retention
    retention.ensure_capacity(dataset)
    added = skipped = held = 0
    added_bytes = 0
    with store.db() as c:
        if dataset in ('daily', 'quotes', 'minutes'):
            added, skipped, held, added_bytes = _publish_bars(c, dataset, values, source, automatic)
        else:
            for v in values:
                if automatic and c.execute("SELECT 1 FROM collection_holds WHERE dataset=%s AND day=%s AND symbol IN (%s, '*')",
                                          (dataset, v["day"], v["symbol"])).fetchone():
                    held += 1
                    continue
                fact_id, inserted = insert_fact(c, dataset, source, v)
                if source=='official_policy' and dataset=='policies':
                    terms=list(dict.fromkeys(term.strip() for term in v.get('_terms',[])
                              if isinstance(term,str) and 2<=len(term.strip())<=24 and term.strip() in v.get('text','')))[:6]
                    setting=f'policy_terms:{fact_id}'
                    if store.setting(setting,[],conn=c)!=terms:
                        store.set_setting(setting,terms,conn=c)
                        bump_generation(c,'policy_terms')
                if source=='cninfo_financials':
                    original_id=(v.get('fields') or {}).get('filing_fact_id')
                    original=c.execute("SELECT symbol,dataset FROM facts WHERE id=%s",(original_id,)).fetchone() if type(original_id) is int else None
                    if not original or original['dataset']!='announcements' or original['symbol']!=v['symbol']:
                        raise ValueError('财报结构化金额缺少同一股票的真实原文事实')
                    c.execute('INSERT IGNORE INTO evidence_refs(owner_type,owner_id,fact_id,created) VALUES(%s,%s,%s,%s)',
                              ('financial_filing',str(fact_id),original_id,store.now()))
                    report_date=v.get('report_date')
                    owner=v['symbol']+':'+report_date
                    if has_cumulative_cny_revenue(v.get('fields') or {}):
                        c.execute("DELETE FROM evidence_refs WHERE owner_type='financial_pending' AND owner_id=%s",(owner,))
                    else:
                        c.execute("DELETE FROM evidence_refs WHERE owner_type='financial_pending' AND owner_id=%s AND fact_id=%s",
                                  (owner,original_id))
                added += int(inserted)
                if inserted:
                    added_bytes += len(dumps(storage_policy.normalize(dataset, v)).encode())
                skipped += int(not inserted)
        changed=getattr(c,'heads_changed',False)
        if changed:
            bump_generation(c, dataset)
        if added:
            retention.account(c, dataset, added_bytes, added)
    if changed:
        refresh_stats(dataset)
    return {"received": len(values), "added": added, "unchanged": skipped, "held": held}


def refresh_stats(dataset=None):
    with store.db(isolation='REPEATABLE READ',timeout=300) as c:
        for ds in ([dataset] if dataset else DATASETS):
            generation=store.setting('generation_'+ds,0,conn=c)
            r = c.execute('SELECT count(*),min(day),max(day),count(DISTINCT symbol) FROM heads WHERE dataset=%s', (ds,)).fetchone()
            versions = c.execute('SELECT count(*) FROM facts WHERE dataset=%s', (ds,)).fetchone()[0]
            c.execute("INSERT INTO asset_stats(dataset,record_count,first_day,last_day,symbol_count,version_count,generation) "
                      "VALUES(%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                      +','.join(f'{field}=IF(VALUES(generation)>=generation,VALUES({field}),{field})'
                                for field in ('record_count','first_day','last_day','symbol_count','version_count'))
                      +',generation=GREATEST(generation,VALUES(generation))',
                      (ds,r[0],r[1],r[2],r[3],versions,generation))


def stat_value(row):
    return {'dataset':row['dataset'],'name':DATASETS[row['dataset']],'count':row['record_count'],
            'first':row['first_day'],'last':row['last_day'],'symbols':row['symbol_count'],
            'versions':row['version_count'],'generation':row['generation']}


def stats():
    with store.db(readonly=True) as c:
        found = {r['dataset']: stat_value(r) for r in c.execute('SELECT * FROM asset_stats')}
    return [found.get(k, {"dataset": k, "name": v, "count": 0, "versions": 0, "symbols": 0, "first": None, "last": None}) for k, v in DATASETS.items()]


def bump_generation(c, dataset):
    c.execute("INSERT INTO settings VALUES(%s,'1') ON DUPLICATE KEY UPDATE data=CAST(CAST(data AS UNSIGNED)+1 AS CHAR)", ("generation_" + dataset,))


def rows(dataset, start=None, end=None, stock=None, limit=50, offset=0, cursor=None):
    if dataset not in DATASETS:
        raise HTTPException(422, "数据类型无效")
    if start and end and day(start) > day(end):
        raise HTTPException(422, "开始日期不能晚于结束日期")
    if type(limit) is not int or not 1 <= limit <= 10001 or type(offset) is not int or not 0 <= offset <= 10000:
        raise HTTPException(422, "分页范围无效；深页请使用下一页游标")
    clauses, params = ["h.dataset=%s"], [dataset]
    if start:
        clauses.append("h.day>=%s"); params.append(day(start))
    if end:
        clauses.append("h.day<=%s"); params.append(day(end))
    if stock:
        clauses.append("h.symbol=%s"); params.append(symbol(stock))
    digest = pagination.filter_hash(dataset, start, end, stock, limit)
    token = pagination.decode(cursor, digest) if cursor else None
    if token and offset:
        raise HTTPException(422, "游标与偏移量不能同时使用")
    where = " AND ".join(clauses)
    with store.db(readonly=True, isolation="REPEATABLE READ") as c:
        generation = store.setting("generation_" + dataset, 0, conn=c)
        if token and token["generation"] != generation:
            raise HTTPException(409, "资料已更新，请刷新列表以查看最新数据")
        cached_row=c.execute('SELECT * FROM asset_stats WHERE dataset=%s',(dataset,)).fetchone() if not token and not stock else None
        cached=stat_value(cached_row) if cached_row else {}
        whole_range=not cached.get('count') or ((not start or start<=cached['first']) and (not end or end>=cached['last'])) if cached else False
        if token:
            count=token['total']
        elif cached and cached.get('generation')==generation and whole_range:
            count=cached['count']
        else:
            count=c.execute('SELECT count(*) FROM heads h WHERE '+where,params).fetchone()[0]
        position = token["position"] if token else offset
        if token:
            d, s, k = token["last"]
            where += ' AND (h.day<%s OR (h.day=%s AND h.symbol>%s) OR (h.day=%s AND h.symbol=%s AND h.record_key>%s))'
            params += [d, d, s, d, s, k]
        index = "head_symbol" if stock else "head_page"
        sql = ('SELECT f.* FROM (SELECT h.fact_id,h.day,h.symbol,h.record_key FROM heads h FORCE INDEX (' + index + ') WHERE '
               + where + ' ORDER BY h.day DESC,h.symbol,h.record_key LIMIT %s OFFSET %s) page '
               'JOIN facts f ON f.id=page.fact_id ORDER BY page.day DESC,page.symbol,page.record_key')
        result = record_store.hydrate(c, c.execute(sql, params + [limit + 1, offset if not token else 0]).fetchall())
    has_more, result = len(result) > limit, result[:limit]
    next_cursor = None
    if has_more and result:
        last = result[-1]
        next_cursor = pagination.encode({"filter": digest, "generation": generation, "total": count,
                                         "position": position + len(result), "last": [last["day"], last["symbol"], last["record_key"]]})
    return {"total": count, "offset": position, "has_more": has_more, "next_cursor": next_cursor,
            "items": [{**r["payload"], "_id": r["id"], "_source": r["source"], "_first_seen": r["first_seen"], "_last_seen": r["last_seen"]} for r in result]}


def calendar():
    with store.db(readonly=True) as c:
        return {r[0]: bool(r[1]) for r in c.execute("SELECT h.day,t.trading FROM heads h JOIN calendar_records t ON t.fact_id=h.fact_id WHERE h.dataset='calendar'")}


def sources():
    with store.db(readonly=True) as c:
        rows = [json.loads(r[0]) for r in c.execute('SELECT data FROM sources ORDER BY id')]
    for default in (ACTIONS_SOURCE, FILING_FINANCIAL_SOURCE, OFFICIAL_POLICY_SOURCE):
        if not any(row['id']==default['id'] for row in rows):
            rows.append(default.copy())
    return rows


ACTIONS_SOURCE = {
    'id':'bs_actions','name':'BaoStock 复权因子','provider':'baostock','operation':'actions','dataset':'actions',
    'enabled':False,'automatic':False,'implemented':True,'min_interval':3,'timeout':25,'health':'unverified',
    'coverage':'指定股票的除权除息日及复权因子查询。空结果仅保留查询覆盖，不登记公司行动。',
    'message':'接口已接入，当前实例尚未实测',
}

FILING_FINANCIAL_SOURCE = {
    'id':'cninfo_financials','name':'巨潮财报原文结构化金额','provider':'cninfo',
    'operation':'filing_financials','dataset':'financials','enabled':True,'automatic':True,
    'implemented':True,'min_interval':3,'timeout':30,'health':'unknown',
    'coverage':'从已保存财报正文提取明确累计期间、币种和单位的营业收入、归母净利润及经营现金流，保留原文页段；不替换供应商净利润口径。',
    'message':'等待按已保存原文提取；缺少可核金额时保留缺口',
}

OFFICIAL_POLICY_SOURCE = {
    'id':'official_policy','name':'政府及中央部门政策原文','provider':'official','operation':'policies','dataset':'policies',
    'enabled':True,'automatic':True,'implemented':True,'min_interval':3,'timeout':15,'health':'unknown',
    'coverage':'由系统维护官方栏目白名单，发现并获取政策原文，保留真实发布日期、URL、正文段落及哈希。术语命中不等于企业适用结论。',
    'message':'等待官方栏目及原文定时读取',
}


def source(sid):
    found = next((s for s in sources() if s["id"] == sid), None)
    if not found:
        raise HTTPException(404, "数据源不存在")
    return found


def save_source(v):
    with store.db() as c:
        c.execute('INSERT INTO sources VALUES(%s,%s) ON DUPLICATE KEY UPDATE data=VALUES(data)', (v["id"], dumps(v)))


def patch_source(sid, changes):
    with store.db() as c:
        row = c.execute('SELECT data FROM sources WHERE id=%s FOR UPDATE', (sid,)).fetchone()
        default = next((value for value in (ACTIONS_SOURCE,FILING_FINANCIAL_SOURCE,OFFICIAL_POLICY_SOURCE) if value['id']==sid),None)
        if not row and default:
            c.execute('INSERT IGNORE INTO sources VALUES(%s,%s)', (sid,dumps(default)))
            row = c.execute('SELECT data FROM sources WHERE id=%s FOR UPDATE', (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "数据源不存在")
        v = {**json.loads(row[0]), **changes}
        c.execute('UPDATE sources SET data=%s WHERE id=%s', (dumps(v), sid))
    return v
