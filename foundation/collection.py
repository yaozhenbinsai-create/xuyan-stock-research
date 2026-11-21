"""A data-only durable collector. No dependency on the retired application."""
from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid

from fastapi import HTTPException
from foundation import assets, database as store, queue_state, record_store, sources

ACTIVE = ("queued", "running", "waiting", "paused")
WATCH_SCHEDULES = {"quotes", "minutes", "financials", "cashflow", "balance", "growth", "actions", "fund_flows", "originals", "filing_financials",
                   "comparison_index","comparison_originals","comparison_financials"}
SUPPLEMENT_SCHEDULES = (
    ('policy_morning','早间官方政策原文','official_policy','08:20'),
    ('policy_evening','晚间官方政策原文','official_policy','18:20'),
    ('comparison_index','补齐上年同期财报索引','cninfo_announcements','18:44'),
    ('cashflow','研究跟踪股票现金流','bs_cashflow','18:52'),
    ('actions','研究跟踪股票复权资料','bs_actions','18:54'),
    ('balance','研究跟踪股票偿债指标','bs_balance','18:56'),
    ('growth','研究跟踪股票成长指标','bs_growth','18:58'),
    ('fund_flows','研究跟踪股票资金流','sina_flow_history','19:05'),
    ('originals','研究跟踪股票公告正文','cninfo_text','19:10'),
    ('comparison_originals','补齐上年同期财报正文','cninfo_text','19:12'),
    ('filing_financials','财报原文金额入库','cninfo_financials','19:20'),
    ('comparison_financials','上年同期财报金额入库','cninfo_financials','19:25'),
)
QUOTE_BATCH_SIZE = 200
log = logging.getLogger(__name__)


def _record_worker_error(action, exc):
    # Database availability is exactly what failed in some worker cycles. A
    # failed audit must not terminate the collector or its clock thread.
    try:
        store.audit(action, {'type':type(exc).__name__})
    except Exception as audit_exc:
        log.error('%s；异常记录暂不可用（%s/%s）',action,type(exc).__name__,type(audit_exc).__name__)


def automatic_symbols(conn=None):
    """Keep the user's watchlist intact and also maintain actual research stocks."""
    if conn is None:
        with store.db(readonly=True) as c:
            return automatic_symbols(c)
    from research.reassessment import ACTIVE as cycle_states
    symbols = list(store.setting('watchlist', [], conn=conn))
    marks=','.join(['%s']*len(cycle_states))
    symbols.extend(row[0] for row in conn.execute(
        'SELECT DISTINCT symbol FROM research_cycles WHERE state IN ('+marks+') ORDER BY symbol LIMIT 6001',cycle_states))
    symbols.extend(row[0] for row in conn.execute(
        'SELECT c.symbol FROM (SELECT id,created FROM research_runs WHERE report IS NOT NULL '
        'AND withdrawn_at IS NULL ORDER BY created DESC,id DESC LIMIT 3) r '
        'JOIN research_candidates c ON c.run_id=r.id ORDER BY r.created DESC,r.id DESC,c.sequence_no'))
    return list(dict.fromkeys(symbols))


def comparison_window(day=None):
    """Use the prior matching quarter's real disclosure window, not today's date."""
    from research.preparation import financial_periods
    reference=date.fromisoformat(day or store.today())
    period=financial_periods(reference.isoformat())[1]
    year,quarter=int(period[:4]),int(period[-1])
    report_date=f'{year}-'+('03-31','06-30','09-30','12-31')[quarter-1]
    if quarter==4:
        start,end=f'{year+1}-01-01',f'{year+1}-04-30'
    else:
        start=(date.fromisoformat(report_date)+timedelta(days=1)).isoformat()
        end=f'{year}-'+{1:'04-30',2:'08-31',3:'10-31'}[quarter]
    # Some cross-listed companies publish Q3 in November (e.g. the retained
    # SMIC 2026 filing was disclosed on Nov 13). Keep the normal disclosure
    # window, and continue looking through the corresponding prior-year date.
    # The original selector still requires the exact fiscal year and quarter.
    prior_day=reference.replace(year=reference.year-1,
                                day=28 if reference.month==2 and reference.day==29 else reference.day)
    end=max(end,prior_day.isoformat())
    return report_date,start,end


def missing_comparison_symbols(symbols, report_date):
    with store.db(readonly=True) as c:
        rows=c.execute("SELECT DISTINCT h.symbol FROM heads h JOIN financial_reports f ON f.fact_id=h.fact_id "
                       "JOIN financial_values v ON v.fact_id=f.fact_id "
                       "JOIN financial_values currency ON currency.fact_id=f.fact_id AND currency.field_name='currency' AND currency.text_value='CNY' "
                       "JOIN financial_values basis ON basis.fact_id=f.fact_id AND basis.field_name='period_basis' AND basis.text_value='cumulative' "
                       "WHERE h.dataset='financials' "
                       "AND f.report_type='filing' AND f.report_date=%s AND v.field_name='operating_revenue' "
                       "AND (v.value_kind IN ('number','integer','negative_zero') OR "
                       "(v.value_kind='text' AND v.text_value IS NOT NULL AND v.text_value<>''))",(report_date,)).fetchall()
    present={row[0] for row in rows}
    return [symbol for symbol in symbols if symbol not in present]


def comparison_original_ids(symbols, start, end):
    from research import originals
    year,month=int(start[:4]),int(start[5:7])
    quarter=(month-1)//3 or 4
    year-=int(quarter==4)
    labels={'第一季度':1,'一季度':1,'半年度':2,'第三季度':3,'三季度':3,'年度':4}
    selected={}
    with store.db(readonly=True) as c:
        marks=','.join(['%s']*len(symbols))
        rows=c.execute("SELECT f.id,h.symbol FROM heads h JOIN facts f ON f.id=h.fact_id "
                       "JOIN announcements a ON a.fact_id=f.id WHERE h.dataset='announcements' "
                       "AND h.day BETWEEN %s AND %s AND h.symbol IN ("+marks+") "
                       "AND LOWER(a.url) LIKE 'https://static.cninfo.com.cn/%%.pdf' "
                       "ORDER BY h.day DESC,f.id DESC LIMIT 5001",(start,end,*symbols)).fetchall()
        if len(rows)>5000:raise HTTPException(409,'同期财报索引超出单批范围，请按股票分批处理')
        for row in record_store.hydrate(c,rows):
            value=row['payload'];title=value.get('title','');period=originals.filing_title_period(title)
            if value.get('text'):continue
            if not period or labels[period[1]]!=quarter:continue
            if period[0] and int(period[0])!=year:continue
            rank=int('摘要' in title)
            previous=selected.get(row['symbol'])
            if previous is None or rank<previous[0]:selected[row['symbol']]=(rank,row['id'])
    return [value[1] for value in selected.values()]


def ensure_foundation_schedules():
    """Register maintained supplement plans once; preserve saved switches/times."""
    with store.db() as c:
        c.execute('INSERT IGNORE INTO sources VALUES(%s,%s)',
                  ('cninfo_financials',store.dumps(assets.FILING_FINANCIAL_SOURCE)))
        c.execute('INSERT IGNORE INTO sources VALUES(%s,%s)',
                  ('official_policy',store.dumps(assets.OFFICIAL_POLICY_SOURCE)))
        for sid,name,source,at in SUPPLEMENT_SCHEDULES:
            value={'id':sid,'name':name,'source':source,'at':at,'kind':'collect',
                   'enabled':True,'trading_only':not sid.startswith('policy_'),'interval':0,'last_job':None,
                   'last_slot':None,'message':'等待定时补齐研究跟踪股票基础资料'}
            c.execute('INSERT IGNORE INTO schedules(id,data) VALUES(%s,%s)',(sid,store.dumps(value)))
            current=c.execute('SELECT data FROM schedules WHERE id=%s',(sid,)).fetchone()
            existing=json.loads(current[0])
            if existing.get('kind')=='collection':
                existing['kind']='collect'
                c.execute('UPDATE schedules SET data=%s WHERE id=%s',(store.dumps(existing),sid))
            queue_state.sync_schedule(c,sid)
        if store.setting('foundation_collection_policy_version',None,conn=c)!='20261120.1':
            # A current-day roster does not exist on weekends; refresh listing
            # details before the open while keeping the census' real date.
            for sid in ('basics','basic_details','daily'):
                row=c.execute('SELECT data FROM schedules WHERE id=%s FOR UPDATE',(sid,)).fetchone()
                if not row:continue
                value=json.loads(row[0])
                if sid=='basics':value['trading_only']=True
                elif sid=='basic_details' and value.get('at')=='16:30':value['at']='07:55'
                elif sid=='daily' and value.get('at')=='15:30':value['at']='17:30'
                c.execute('UPDATE schedules SET data=%s WHERE id=%s',(store.dumps(value),sid))
                queue_state.sync_schedule(c,sid,value)
            store.set_setting('foundation_collection_policy_version','20261120.1',conn=c)


def schedules():
    with store.db(readonly=True) as c:
        result = [queue_state.schedule_view(r) for r in c.execute('SELECT * FROM schedules')]
    watch = automatic_symbols()
    for s in result:
        s["needs_symbols"] = s["id"] in WATCH_SCHEDULES and not watch
        if s['id'] in WATCH_SCHEDULES:
            s['symbols_count']=len(watch)
            s['symbols_basis']='关注名单、有效跟踪周期及最近三份已签发报告候选的并集'
    return sorted(result, key=lambda s: (s.get("at", ""), s["id"]))


def save_schedule(sid, raw):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(raw.get("at", ""))):
        raise HTTPException(422, "采集时间须为 HH:MM")
    if type(raw.get("enabled")) is not bool or type(raw.get("trading_only")) is not bool:
        raise HTTPException(422, "启用和交易日开关须为布尔值")
    with store.db() as c:
        row = c.execute('SELECT data FROM schedules WHERE id=%s FOR UPDATE', (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "采集计划不存在")
        v = json.loads(row[0])
        v.update({k: raw[k] for k in ("at", "enabled", "trading_only")})
        c.execute('UPDATE schedules SET data=%s WHERE id=%s', (store.dumps(v), sid))
        queue_state.sync_schedule(c,sid,v)
        store.audit("修改每日采集计划", {"id": sid, **raw}, conn=c)
    return v


def list_jobs(limit=30):
    with store.db(readonly=True) as c:
        return [queue_state.job_view(r,include_items=False) for r in c.execute(
            'SELECT '+queue_state.JOB_SUMMARY_COLUMNS+' FROM collection_jobs ORDER BY created DESC,id DESC LIMIT %s',
            (max(1,min(int(limit),100)),))]


def get_job(jid):
    with store.db(readonly=True) as c:
        r = c.execute('SELECT * FROM collection_jobs WHERE id=%s', (jid,)).fetchone()
    if not r:
        raise HTTPException(404, "采集任务不存在")
    return queue_state.job_view(r)


def plan_items(src, start, end, symbols, schedule_id=None, *, targeted=False, records=None, financial_periods=None):
    op = src["operation"]
    p = {"start": start, "end": end, "symbols": symbols, "period": 5}
    if op == "calendar":
        return [{**p, "start": end[:4] + "-01-01", "end": end[:4] + "-12-31"}]
    if schedule_id in WATCH_SCHEDULES and not symbols:
        raise HTTPException(422, "此计划只采集关注股票，请先设置关注列表")
    if op == "quotes":
        if not symbols:
            with store.db(readonly=True) as c:
                symbols = [r[0] for r in c.execute("SELECT DISTINCT symbol FROM heads WHERE dataset='basics' ORDER BY symbol")]
            if not symbols:
                raise HTTPException(422, "证券名录为空，请先更新 A 股名录")
        batches = (len(symbols) + QUOTE_BATCH_SIZE - 1) // QUOTE_BATCH_SIZE
        # Distribute the sorted exchange ranges across batches so a cutoff
        # or provider slowdown cannot omit an entire exchange by position.
        return [{**p, "symbols": symbols[i::batches]} for i in range(batches)]
    if op in ("profit", "balance", "cashflow", "growth"):
        if not symbols:
            raise HTTPException(422, "财务资料请指定关注股票")
        # Query only quarters whose report date is no later than this range.
        quarters = []
        for y in range(int(start[:4]) - 1, int(end[:4]) + 1):
            for q, mmdd in enumerate(("03-31", "06-30", "09-30", "12-31"), 1):
                rd = f"{y}-{mmdd}"
                if rd <= end and rd >= f"{int(start[:4])-1}-01-01":
                    quarters.append((y, q))
        if financial_periods is not None:
            quarters = [(y,q) for y,q in quarters if f'{y}Q{q}' in financial_periods]
        latest = max(quarters) if quarters else None
        return [{**p, "symbol": s, "symbols": [s], "year": y, "quarter": q,
                 **({'financial_fallback_depth': 0} if targeted and financial_periods is not None and (y,q)==latest else {})}
                for y, q in reversed(quarters) for s in symbols]
    if op in ("daily", "minutes", "flow_history", "actions", "filing_financials"):
        if not symbols:
            raise HTTPException(422, "这个接口需要指定股票")
        return [{**p, "symbol": s, "symbols": [s]} for s in symbols]
    if op == "basics" and symbols:
        return [{**p, "symbol": s, "symbols": [s]} for s in symbols]
    if op in ("daily_all", "announcements"):
        if op == "announcements" and targeted and symbols:
            size=8 if schedule_id=='comparison_index' else 1
            return [{**p, "symbols": symbols[offset:offset+size]} for offset in range(0,len(symbols),size)]
        a, b = date.fromisoformat(start), date.fromisoformat(end)
        cal = assets.calendar()
        days = [(a+timedelta(days=i)).isoformat() for i in range((b-a).days+1)]
        if op == "daily_all":
            if any(d not in cal for d in days):
                raise HTTPException(422, "交易日历未覆盖该范围，请先更新日历")
            days = [d for d in days if cal[d]]
        return [{**p, "start": d, "end": d} for d in days]
    if op == "announcement_text":
        if not symbols:
            raise HTTPException(422, "公告正文请指定股票")
        if schedule_id in ('originals','comparison_originals') and records is None:
            return [{**p,'plan_originals':True}]
        if records is None and targeted:
            from research.preparation import _original_ids
            records=_original_ids(symbols,start,end)
            if not records:return []
        if records is not None:
            if not records:return []
            with store.db(readonly=True) as c:
                marks = ','.join(['%s']*len(records))
                found = {r[0]:r[1] for r in c.execute(
                    "SELECT fact_id,symbol FROM heads WHERE dataset='announcements' AND day BETWEEN %s AND %s "
                    "AND fact_id IN ("+marks+")", (start,end,*records))}
            return [{**p,"record_id":fid,"symbol":found[fid],"symbols":[found[fid]]}
                    for fid in records if found.get(fid) in symbols]
        out = []
        for s in symbols:
            rows = assets.rows("announcements", start, end, s, 100)["items"]
            out.extend({**p, "record_id": r["_id"], "symbol": s, "symbols": [s]} for r in rows if r.get("url") and not r.get("text"))
        return out
    return [p]


def submit(raw, *, trigger="manual", schedule_id=None, conn=None, expires_at=None, records=None, financial_periods=None,
           continue_after_expiry=False):
    start = assets.day(raw.get("start") or store.today())
    end = assets.day(raw.get("end") or start)
    if start > end or (date.fromisoformat(end)-date.fromisoformat(start)).days > 370 or end > store.today():
        raise HTTPException(422, "日期范围无效；最多一年，不能采集未来行情")
    symbols = raw.get("symbols", [])
    if not isinstance(symbols, list) or len(symbols) > 6000:
        raise HTTPException(422, "股票列表无效")
    symbols = sorted(set(assets.symbol(s) for s in symbols))
    src = assets.source(raw.get("source"))
    if src["operation"] == "quotes" and (start != store.today() or end != store.today()):
        raise HTTPException(422, "行情快照接口只获取当前报价；历史快照请在数据资产中查询")
    mode = raw.get("mode", "missing")
    if mode not in ("missing", "refresh"):
        raise HTTPException(422, "采集模式无效")
    if not src.get("enabled"):
        raise HTTPException(422, "来源当前停用，请先在数据源中启用")
    if trigger != "manual" and not src.get("automatic"):
        raise HTTPException(422, "来源未开启自动使用")
    if continue_after_expiry and (trigger != 'research_evidence' or src['operation'] not in
                                  ('announcements','announcement_text','actions','profit','cashflow','balance','growth')):
        raise HTTPException(422,'仅非行情研究补证可在等待时限后继续采集')
    if records is not None and (src["operation"]!="announcement_text" or not records or len(records)>16):
        raise HTTPException(422,"公告原文须使用本轮选定的事实编号")
    if financial_periods is not None and (src['operation'] not in ('profit','balance','cashflow','growth')
            or not isinstance(financial_periods,list) or not 1<=len(financial_periods)<=8
            or any(not isinstance(p,str) or not re.fullmatch(r'20\d{2}Q[1-4]',p) for p in financial_periods)):
        raise HTTPException(422,'财务补采须指定有效报告期')
    items = plan_items(src, start, end, symbols, schedule_id,
                       targeted=trigger=="research_evidence" or schedule_id in ('originals','comparison_index','comparison_originals'),
                       records=records, financial_periods=financial_periods)
    identity_fields=[src["id"], start, end, symbols, schedule_id, mode]
    if expires_at:
        identity_fields.append(expires_at)
    if records:
        identity_fields.append(records)
    if financial_periods is not None:
        identity_fields.append(financial_periods)
    if continue_after_expiry:
        identity_fields.append('continue_evidence')
    identity = hashlib.sha256(store.dumps(identity_fields).encode()).hexdigest()
    def persist(c):
        c.execute("INSERT IGNORE INTO settings VALUES('collection_submit_mutex','0')")
        c.execute("SELECT data FROM settings WHERE `key`='collection_submit_mutex' FOR UPDATE")
        old = c.execute("SELECT * FROM collection_jobs WHERE identity=%s AND status IN ('queued','running','waiting','paused')", (identity,)).fetchone()
        if old:
            return queue_state.job_view(old)
        j = {"id": uuid.uuid4().hex[:16], "source": src["id"], "name": src["name"], "dataset": src["dataset"], "start": start, "end": end,
             "symbols": symbols, "items": items, "done": 0, "total": len(items), "added": 0, "unchanged": 0, "received": 0,
             "failed": 0, "no_data": 0, "status": "queued", "created": store.now(), "updated": store.now(), "trigger": trigger,
             "schedule_id": schedule_id, "mode": mode, "reused": 0, "message": "等待采集", "identity": identity, "retry_at": 0, "retries": 0}
        if expires_at:
            j["expires_at"] = expires_at
        if continue_after_expiry:
            j['continue_after_expiry'] = True
        encoded = store.dumps(j)
        if len(encoded.encode()) > 524288 or len(items) > 5000:
            raise HTTPException(422, "采集范围过大，请缩小日期或股票范围后分批提交")
        if c.execute("SELECT count(*) FROM collection_jobs WHERE status IN ('queued','running','waiting','paused')").fetchone()[0] >= 20:
            raise HTTPException(409, "待处理采集任务已达上限，请先完成或停止已有任务")
        c.execute('INSERT INTO collection_jobs(id,identity,status,created,data) VALUES(%s,%s,%s,%s,%s)', (j["id"], identity, j["status"], j["created"], encoded))
        queue_state.sync_job(c,j['id'],j)
        store.audit("创建数据采集", {k: j[k] for k in ("id", "source", "start", "end", "trigger")}, conn=c)
        return j
    if conn is not None:
        return persist(conn)
    with store.db() as c:
        return persist(c)


def control(jid, action):
    with store.db() as c:
        r = c.execute('SELECT * FROM collection_jobs WHERE id=%s FOR UPDATE', (jid,)).fetchone()
        if not r:
            raise HTTPException(404, "采集任务不存在")
        j = queue_state.job_view(r)
        if action == "pause" and j["status"] in ("running", "queued", "waiting"):
            j.update(status="paused", message="用户暂停；已完成批次保留")
        elif action == "resume" and (j["status"] in ("paused", "failed", "interrupted") or
                                     j['status']=='partial' and j['done']<j['total'] and j.get('items')):
            if assets.source(j['source'])['operation']=='quotes' and j['start']!=store.today():
                raise HTTPException(409,'历史截点快照无法重新采集；已保留当时缺口')
            if queue_state.expired(j.get('expires_at')):
                if assets.source(j['source'])['operation']=='quotes':
                    raise HTTPException(409,'历史截点快照无法重新采集；已保留当时缺口')
                j['evidence_deadline_at']=j.pop('expires_at')
            j.update(status="queued", message="从已保存进度继续", retry_at=0, retries=0)
        elif action == "cancel" and j["status"] in ACTIVE:
            j.update(status="cancelled", message="用户停止；已获取资料保留")
        else:
            raise HTTPException(409, "当前状态不支持这个操作")
        j["updated"] = store.now()
        if j["status"] == "cancelled":
            j.pop("items",None)
        c.execute('UPDATE collection_jobs SET status=%s,data=%s WHERE id=%s', (j["status"], store.dumps(j), jid))
        queue_state.sync_job(c,jid,j)
        store.audit("采集任务 " + action, {"id": jid}, conn=c)
    return {k: v for k, v in j.items() if k != "items"}


def run_schedule(sid, *, manual=True):
    s = next((x for x in schedules() if x["id"] == sid), None)
    if not s:
        raise HTTPException(404, "采集计划不存在")
    if s["kind"] == "backup":
        return backup()
    watch = automatic_symbols() if sid in WATCH_SCHEDULES else []
    raw = {"source": s["source"], "start": store.today(), "end": store.today(), "symbols": watch}
    periods = None
    if sid in ('policy_morning','policy_evening'):
        raw['start']=(assets.local_now().date()-timedelta(days=30)).isoformat()
    elif sid in ('comparison_index','comparison_originals','comparison_financials'):
        report_date,start,end=comparison_window()
        watch=missing_comparison_symbols(watch,report_date)
        if not watch:return {'reused':True,'message':'上年同期营业收入原文金额已入库，复用已有资料'}
        raw.update(start=start,end=end,symbols=watch)
    elif sid in ('financials','cashflow','balance','growth'):
        from research.preparation import financial_periods
        raw['start']=(assets.local_now().date()-timedelta(days=365)).isoformat()
        periods=financial_periods(store.today())
    elif sid=='actions':raw['start']=(assets.local_now().date()-timedelta(days=365)).isoformat()
    elif sid=='fund_flows':raw['start']=(assets.local_now().date()-timedelta(days=30)).isoformat()
    elif sid in ('originals','filing_financials'):raw['start']=(assets.local_now().date()-timedelta(days=90)).isoformat()
    if sid == "daily_recheck":
        raw["start"] = (assets.local_now().date()-timedelta(days=5)).isoformat()
    return submit(raw, trigger="manual" if manual else "schedule", schedule_id=sid,financial_periods=periods)


def backup():
    from foundation.backups import create_backup
    return create_backup()


def market_daily_received(conn, day):
    scope = store.dumps({"start": day, "end": day, "symbols": [], "period": 5})
    row = conn.execute("SELECT received FROM collection_units WHERE source='bs_daily_all' AND scope=%s", (scope,)).fetchone()
    return row[0] if row else 0


class Engine:
    def __init__(self):
        self.stop_event = threading.Event()
        self.worker = self.clock = None
        self.last_provider = {}
        self.last_maintenance = 0

    def start(self):
        ensure_foundation_schedules()
        with store.db() as c:
            since=(assets.local_now().date()-timedelta(days=2)).isoformat()
            for r in c.execute("SELECT * FROM collection_jobs WHERE status='running' OR "
                               "(status='interrupted' AND `trigger`<>'manual' AND created>=%s)",(since,)).fetchall():
                j = queue_state.job_view(r)
                quote = assets.source(j['source'])['operation']=='quotes'
                historical_quote = quote and (j['start']!=store.today() or queue_state.expired(j.get('expires_at')))
                automatic = j['trigger']!='manual' and not historical_quote and (
                    not queue_state.expired(j.get('expires_at')) or j.get('continue_after_expiry'))
                j.update(status="queued" if automatic else "interrupted",retry_at=0,
                         message="服务恢复，按已保存进度继续自动采集" if automatic else "服务中断，进度已保留；可继续原任务")
                if historical_quote:
                    j.update(status='partial',message='历史截点已结束，保留已采快照和真实缺口，不以当前行情补写')
                j['updated']=store.now()
                c.execute('UPDATE collection_jobs SET status=%s,data=%s WHERE id=%s', (j["status"], store.dumps(j), j["id"]))
                queue_state.sync_job(c,j['id'],j)
        self.worker = threading.Thread(target=self.loop, daemon=True, name="foundation-collector")
        self.clock = threading.Thread(target=self.clock_loop, daemon=True, name="foundation-calendar")
        self.worker.start(); self.clock.start()

    def stop(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=2)
        if self.clock:
            self.clock.join(timeout=2)

    def clock_loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                _record_worker_error('采集时钟异常',exc)
            self.stop_event.wait(20)

    def tick(self):
        if time.monotonic() - self.last_maintenance >= 3600:
            from foundation import retention
            retention.cleanup()
            self.last_maintenance = time.monotonic()
        if store.setting("automation_paused", True):
            return
        now = assets.local_now(); cal = assets.calendar()
        for s in schedules():
            if not s.get("enabled") or s.get("needs_symbols") or now.strftime("%H:%M") < s["at"]:
                continue
            if s.get("trading_only") and cal.get(store.today()) is not True:
                continue
            interval = int(s.get("interval") or 0)
            if interval and not ("09:30" <= now.strftime("%H:%M") <= "11:30" or "13:00" <= now.strftime("%H:%M") <= "15:00"):
                continue
            slot = f"{store.today()}:{int(now.timestamp())//interval}" if interval else store.today()
            if s.get("last_slot") == slot:
                continue
            try:
                j = run_schedule(s["id"], manual=False)
                s.update(last_slot=slot, last_job=j.get("id"), message=j.get('message') or ("已提交当期采集" if "id" in j else "备份完成"))
            except HTTPException as exc:
                s.update(message=str(exc.detail))
            with store.db() as c:
                c.execute('UPDATE schedules SET data=%s WHERE id=%s', (store.dumps({k: v for k, v in s.items() if k not in {"needs_symbols","symbols_count","symbols_basis"}}), s["id"]))
                queue_state.sync_schedule(c,s['id'],s)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                j = self.claim()
                if j:
                    self.execute_unit(j)
                else:
                    self.stop_event.wait(2)
            except Exception as exc:
                _record_worker_error('采集线程异常',exc)
                self.stop_event.wait(3)

    def claim(self):
        paused = store.setting("automation_paused", True)
        with store.db() as c:
            r = c.execute("SELECT * FROM collection_jobs WHERE status IN ('queued','waiting') AND "
                          "(retry_at<=%s OR expires_at<=%s) "
                          "AND (%s=0 OR `trigger`='manual') "
                          "ORDER BY expires_at IS NULL,last_yield,created,id LIMIT 1 FOR UPDATE SKIP LOCKED",
                          (time.time(),queue_state.local_datetime(store.now()),int(paused))).fetchone()
            if not r:
                return None
            j = queue_state.job_view(r); j["status"] = "running"; j["message"] = "正在获取本批资料，已完成批次保持不变"
            c.execute("UPDATE collection_jobs SET status='running',data=%s WHERE id=%s", (store.dumps(j), j["id"]))
            queue_state.sync_job(c,j['id'],j)
            return j

    def persist(self, j, event=None):
        with store.db() as c:
            r = c.execute('SELECT status FROM collection_jobs WHERE id=%s FOR UPDATE', (j["id"],)).fetchone()
            if r and r[0] in ("paused", "cancelled"):
                # A pause/cancel during the network call must survive commit.
                j["status"] = r[0]
                j["message"] = "用户暂停；已完成批次保留" if r[0] == "paused" else "用户停止；已获取资料保留"
            j["updated"] = store.now(); j["last_yield"] = time.time()
            if j["status"] in ("completed", "cancelled") or j['status']=='partial' and j['done']>=j['total']:
                j = {k: v for k, v in j.items() if k != "items"}
            c.execute('UPDATE collection_jobs SET status=%s,data=%s WHERE id=%s', (j["status"], store.dumps(j), j["id"]))
            queue_state.sync_job(c,j['id'],j)
            if event:
                c.execute('INSERT INTO collection_events(job_id,created,data) VALUES(%s,%s,%s)', (j["id"], store.now(), store.dumps(event)))

    def pending_source_jobs(self, source, job, unit):
        with store.db(readonly=True) as c:
            rows=c.execute("SELECT id,data FROM collection_jobs WHERE source=%s "
                           "AND status IN ('queued','running','waiting') AND id<>%s "
                           "AND range_start<=%s AND range_end>=%s LIMIT 20",
                           (source,job['id'],unit['end'],unit['start'])).fetchall()
        selected=set(unit.get('symbols') or [unit.get('symbol')])
        return [row['id'] for row in rows if not json.loads(row['data']).get('symbols')
                or selected.intersection(json.loads(row['data'])['symbols'])]

    def incomplete_index_jobs(self, unit):
        with store.db(readonly=True) as c:
            rows=c.execute("SELECT id,status,data FROM collection_jobs WHERE source='cninfo_announcements' "
                           "AND range_end=%s AND created>=%s ORDER BY created DESC,id DESC LIMIT 20",
                           (unit['end'],unit['end']+'T00:00:00+08:00')).fetchall()
        unseen=set(unit['symbols']);gaps=[]
        for row in rows:
            selected=set(json.loads(row['data']).get('symbols',[]))
            relevant=unseen.intersection(selected) if selected else set(unseen)
            if not relevant:continue
            unseen.difference_update(relevant)
            if row['status']!='completed':gaps.append({'id':row['id'],'status':row['status']})
            if not unseen:break
        return gaps

    def pin_comparison_inputs(self, job, unit, values):
        if job.get('schedule_id') not in ('comparison_index','comparison_originals'):return
        year,month=int(unit['start'][:4]),int(unit['start'][5:7])
        quarter=(month-1)//3 or 4;year-=int(quarter==4)
        period=f'{year}-'+('03-31','06-30','09-30','12-31')[quarter-1]
        with store.db() as c:
            if job['schedule_id']=='comparison_index':
                identifiers=comparison_original_ids(unit['symbols'],unit['start'],unit['end'])
                rows=[c.execute('SELECT id,symbol FROM facts WHERE id=%s',(identifier,)).fetchone() for identifier in identifiers]
            else:
                rows=[c.execute("SELECT fact_id id,symbol FROM heads WHERE dataset='announcements' AND day=%s "
                                "AND symbol=%s AND record_key=%s",(value['day'],value['symbol'],value.get('key',''))).fetchone()
                      for value in values]
            for row in rows:
                if row:
                    c.execute('INSERT IGNORE INTO evidence_refs(owner_type,owner_id,fact_id,created) VALUES(%s,%s,%s,%s)',
                              ('financial_pending',row['symbol']+':'+period,row['id'],store.now()))

    def execute_unit(self, j):
        if queue_state.expired(j.get('expires_at')):
            if j.get('continue_after_expiry') and j['trigger']=='research_evidence':
                j['evidence_deadline_at']=j.pop('expires_at')
                j.update(status='queued',message='本轮补证等待已结束；剩余资料转常规队列继续，不追改已冻结报告')
                return self.persist(j)
            j.update(status="partial", message="已过本轮采集时限；已保存批次保留，剩余范围记缺口")
            return self.persist(j)
        if j["done"] >= j["total"]:
            j.update(status="completed", message="所选范围没有待采集批次")
            return self.persist(j)
        src = assets.source(j["source"]); p = j["items"][j["done"]]
        if src['operation']=='quotes' and j['start']!=store.today():
            j.update(status='partial',message='历史截点已结束，保留已采快照和真实缺口，不以当前行情补写')
            return self.persist(j)
        if not src.get("enabled") or (j["trigger"] != "manual" and not src.get("automatic")):
            j.update(status="paused", message="数据源已暂停，已完成批次保留")
            return self.persist(j)
        if p.get('plan_originals'):
            if self.pending_source_jobs('cninfo_announcements',j,p):
                j.update(status='waiting',retry_at=time.time()+30,
                         message='等待已提交的公告索引完成，再选择本期重点原文')
                return self.persist(j)
            from research.preparation import _original_ids
            records=(comparison_original_ids(p['symbols'],p['start'],p['end'])
                     if j.get('schedule_id')=='comparison_originals' else _original_ids(p['symbols'],p['start'],p['end']))
            units=plan_items(src,p['start'],p['end'],p['symbols'],j.get('schedule_id'),targeted=True,records=records)
            gaps=self.incomplete_index_jobs(p)
            if gaps:
                j['index_dependency_gaps']=gaps
                j['items'][j['done']:j['done']+1]=[{**p,'index_dependency_gaps':gaps},*units]
                j['done']+=1
                j['no_data']+=1
            else:j['items'][j['done']:j['done']+1]=units
            j.update(total=len(j['items']),retry_at=0,status='queued',
                     message='公告索引尚有未完成回执，已保留缺口并登记可用原文' if gaps else
                             '公告索引已就绪，已登记本期重点原文' if units else '所选范围没有待下载的已索引重点原文')
            if j['done']>=j['total']:j['status']='partial' if j['no_data'] else 'completed'
            return self.persist(j)
        if src['operation']=='filing_financials':
            pending=self.pending_source_jobs('cninfo_text',j,p)
            if pending:
                j.update(status='waiting',retry_at=time.time()+30,
                         message='等待同一股票已提交的公告正文完成，再提取可核财务金额')
                return self.persist(j)
        if (j['trigger']=='research_evidence' and j.get('mode','missing')=='missing'
                and src['operation'] in ('profit','cashflow','balance','growth')):
            report_date=f"{p['year']}-"+('03-31','06-30','09-30','12-31')[p['quarter']-1]
            with store.db(readonly=True) as c:
                saved=c.execute("SELECT f.id FROM heads h JOIN facts f ON f.id=h.fact_id JOIN financial_reports fr ON fr.fact_id=f.id "
                                "WHERE h.dataset='financials' AND h.symbol=%s AND f.source=%s "
                                "AND fr.report_date=%s "
                                "AND h.day<=%s LIMIT 1",(p['symbol'],src['id'],report_date,j['end'])).fetchone()
            if saved:
                j.update(done=j['done']+1,reused=j.get('reused',0)+1,retry_at=0,retries=0,status='queued',
                         message='本报告期已有真实披露事实，复用已存资料')
                if j['done']>=j['total']:j['status']='partial' if j['no_data'] else 'completed'
                return self.persist(j,{'scope':p,'status':'reused','fact_id':saved[0],'network_request':False})
        whole_market_daily = src["operation"] == "daily_all" and not p.get("symbols")
        if whole_market_daily and j.get("mode", "missing") == "missing":
            with store.db(readonly=True) as c:
                received = market_daily_received(c, p["end"])
                count = c.execute("SELECT count(*) FROM heads WHERE dataset='daily' AND day=%s", (p["end"],)).fetchone()[0] if received else 0
            if received and count >= received:
                j.update(done=j["done"]+1, reused=j.get("reused", 0)+1, status="queued", retry_at=0,
                         message="此日全市场日线已完整处理，直接复用已存资料")
                if j["done"] >= j["total"]:
                    j["status"] = "completed"
                return self.persist(j, {"scope": p, "status": "reused", "current_records": count, "network_request": False})
        if src["operation"] == "daily_all" and p["end"] == store.today() and assets.local_now().strftime("%H:%M") < "17:30":
            retry = assets.local_now().replace(hour=17, minute=30, second=0, microsecond=0).timestamp()
            j.update(status="waiting", retry_at=retry, message="等待 BaoStock 当日正式日线发布（17:30 后检查）")
            return self.persist(j)
        local_derivation=src['operation']=='filing_financials'
        pause = 0 if local_derivation else max(0, float(src.get("min_interval", 3)) - (time.monotonic()-self.last_provider.get(src["provider"], 0)))
        if self.stop_event.wait(pause):
            j.update(status="interrupted", message="服务停止，已完成批次保留")
            return self.persist(j)
        if queue_state.expired(j.get('expires_at')):
            if j.get('continue_after_expiry') and j['trigger']=='research_evidence':
                j['evidence_deadline_at']=j.pop('expires_at')
                j.update(status='queued',message='本轮补证等待已结束；剩余资料转常规队列继续，不追改已冻结报告')
                return self.persist(j)
            j.update(status="partial", message="已过本轮采集时限；已保存批次保留，剩余范围记缺口")
            return self.persist(j)
        try:
            started = time.monotonic()
            request=p
            if src['id']=='official_policy' and p.get('policy_snapshot_ref'):
                request={**p,'policy_snapshot':j['policy_snapshot']}
            values = sources.collect(src, request)
            diagnostics=getattr(values,'diagnostics',[])
            fetched=time.monotonic()
            if not local_derivation:self.last_provider[src["provider"]] = fetched
            fetch_ms = round((fetched - started) * 1000)
            page = getattr(values, "page_info", None)
            if j.get("expires_at") and src["operation"]=="quotes":
                values = [v for v in values if v["day"]==p["end"] and v.get("source_time") and v["source_time"]<=j["expires_at"]]
            if p.get("symbols") and src["dataset"] != "calendar":
                selected = set(p["symbols"])
                values = [v for v in values if v["symbol"] in selected]
                if not values and src["dataset"] != "announcements":
                    raise sources.SourceError("指定股票未在来源返回结果中出现", "empty")
            if page and page.get("next"):
                following = {**p, "page": page["next"]}
                if src['id']=='official_policy':
                    extra=dict(page.get('next_params') or {})
                    if 'policy_snapshot' in extra:
                        j['policy_snapshot']=extra.pop('policy_snapshot')
                        following['policy_snapshot_ref']=True
                    following.update(extra)
                if following not in j["items"]:
                    j["items"].insert(j["done"]+1, following); j["total"] = len(j["items"])
            # Pause before publication keeps this exact unpublished unit pending.
            if get_job(j["id"])["status"] in ("paused", "cancelled"):
                return self.persist(j)
            publish_started = time.monotonic()
            result = assets.publish(src["dataset"], values, src["id"], automatic=j["trigger"] != "manual")
            self.pin_comparison_inputs(j,p,values)
            publish_ms = round((time.monotonic() - publish_started) * 1000)
            if whole_market_daily:
                with store.db() as c:
                    c.execute('INSERT INTO collection_units VALUES(%s,%s,%s,%s) ON DUPLICATE KEY UPDATE checked=VALUES(checked),received=VALUES(received)',
                              (src["id"], store.dumps(p), store.now(), len(values)))
            for k in ("added", "unchanged", "received"):
                j[k] += result[k]
            if diagnostics:
                j['source_diagnostics']=diagnostics
                j['no_data']+=sum(item.get('status') in ('error','limited','failed','blocked','missing') for item in diagnostics)
            j.update(done=j["done"]+1, retries=0, retry_at=0, status="queued", message=f"已保存 {j['done']+1}/{j['total']} 批次")
            if j["done"] >= j["total"]:
                j.update(status="partial" if j["no_data"] else "completed", message="采集批次完成" + (f"；{j['no_data']} 项资料未取得，保留未知" if j["no_data"] else ""))
            assets.patch_source(src["id"], {"health": "ok", "last_success": store.now(), "message": f"获取 {len(values)} 条；新增版本 {result['added']}，重复 {result['unchanged']}"})
            self.persist(j, {"scope": p, "status": "saved", "fetch_ms": fetch_ms, "publish_ms": publish_ms,
                             "save_ms": round((time.monotonic() - started) * 1000) - fetch_ms, **result,
                             **({'source_diagnostics':diagnostics} if diagnostics else {})})
        except sources.SourceError as exc:
            if not local_derivation:self.last_provider[src["provider"]] = time.monotonic()
            # A just-ended quarter may not yet have been disclosed. Keep the
            # missing result, then ask for an earlier real period and its YoY
            # counterpart within the same original job, bounded to four quarters.
            depth=p.get('financial_fallback_depth')
            if (exc.kind=='empty' and j['trigger']=='research_evidence' and
                    src['operation'] in ('profit','cashflow','balance','growth') and depth is not None and depth<3):
                year,quarter=p['year'],p['quarter']-1
                if quarter==0:year,quarter=year-1,4
                fallback=[{**p,'year':year,'quarter':quarter,'financial_fallback_depth':depth+1},
                          {k:v for k,v in {**p,'year':year-1,'quarter':quarter}.items() if k!='financial_fallback_depth'}]
                existing={(x.get('symbol'),x.get('year'),x.get('quarter')) for x in j['items']}
                added=[x for x in fallback if (x.get('symbol'),x.get('year'),x.get('quarter')) not in existing]
                j['items'][j['done']+1:j['done']+1]=added
                j['total']=len(j['items'])
            if j["trigger"]=="research_evidence" and exc.kind in ("unsupported","stale","parse","empty"):
                j.update(done=j["done"]+1,no_data=j["no_data"]+1,
                         status="partial" if j["done"]+1>=j["total"] else "queued",message=str(exc))
            elif exc.kind == "empty" and (src["dataset"] == "financials" or j.get("expires_at") and src["operation"] == "quotes"):
                label = "本期财务" if src["dataset"] == "financials" else "本批有效快照"
                j.update(done=j["done"]+1, no_data=j["no_data"]+1, status="queued", message=f"{label}未取得，保留缺口")
                if j["done"] >= j["total"]:
                    j.update(status="partial")
            elif (exc.kind in ('network','timeout') and (src['provider']=='baostock' or j.get('schedule_id')=='basic_details')
                  and j['retries']>=3 and j.get('deferred_retries',0)<3
                  and assets.local_now().strftime('%H:%M')<'20:15'):
                j.update(status='waiting',deferred_retries=j.get('deferred_retries',0)+1,
                         retry_at=time.time()+900,message=str(exc)+'；十五分钟后重连原批次')
            elif exc.kind=='rate_limit' and j.get('retries',0)<3:
                j.update(status='waiting',retries=j.get('retries',0)+1,retry_at=time.time()+900,
                         message=str(exc)+'；十五分钟后继续原批次')
            elif exc.kind in ("network", "timeout") and j["retries"] < 3:
                j.update(status="waiting", retries=j["retries"]+1, retry_at=time.time()+60*(j["retries"]+1), message=str(exc))
            elif exc.kind == "empty" and src["operation"] in ("daily_all","universe") and p["end"] == store.today() and assets.local_now().strftime("%H:%M") < "20:30":
                now = assets.local_now()
                if src['operation']=='universe' and now.strftime('%H:%M')<'17:30':
                    retry_at = now.replace(hour=17,minute=30,second=0,microsecond=0).timestamp()
                    message = '当日证券名单暂未返回，17:30 后再核对'
                else:
                    retry_at = time.time()+300
                    label = '当日证券名单' if src['operation']=='universe' else '正式日线'
                    message = label+'暂未返回，五分钟后核对'
                j.update(status="waiting", retry_at=retry_at, message=message+'；未以空数据记为成功')
            else:
                j.update(status="failed", failed=j["failed"]+1, message=str(exc))
            assets.patch_source(src["id"], {"health": "restricted" if exc.kind == "restricted" else "error", "message": str(exc)})
            self.persist(j, {"scope": p, "status": j["status"], "kind": exc.kind, "message": str(exc)})
        except Exception as exc:
            j.update(status="failed", failed=j["failed"]+1, message="本地处理未完成；请查看任务记录后继续")
            event = {"scope": p, "status": "failed", "kind": type(exc).__name__}
            if isinstance(exc, store.DatabaseError) and exc.args and type(exc.args[0]) is int:
                event['database_error_code'] = exc.args[0]
            self.persist(j, event)


engine = Engine()
