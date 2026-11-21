"""Explicit persistence allowlists. Vendor responses must never be dumped verbatim."""
from __future__ import annotations
import hashlib
from foundation import database as store

MIGRATION_DATASETS = ("calendar", "basics", "industry", "daily")
FIELDS = {
 "calendar": {"trading", "markets"},
 "basics": {"name", "listed", "delisted", "status", "board"},
 "industry": {"group", "taxonomy", "source_effective_date", "historical_effective_unknown", "unclassified"},
 "daily": {"open", "high", "low", "close", "preclose", "volume", "amount", "turnover", "suspended", "is_st", "adjust", "volume_unit", "pe", "pb"},
 "quotes": {"name", "open", "high", "low", "close", "preclose", "volume", "amount", "turnover", "source_time", "volume_unit", "pe", "pb"},
 "minutes": {"open", "high", "low", "close", "volume", "amount", "adjust", "source_time", "period", "closed", "volume_unit"},
 "financials": {"report_type", "report_date", "published", "fields"},
 "announcements": {"title", "published", "url", "text", "text_extracted_at", "text_sha256", "text_truncated", "document_sha256", "text_pages", "total_pages", "text_scope"},
 "policies": {"title", "published", "url", "text", "document_sha256", "effective_day", "text_scope"},
 "fund_flows": {"net_main", "net_large", "net_super", "net_medium", "net_small", "ratio_main", "unit", "basis", "source_time", "time_precision"},
 "actions": {"fields", "record_kind", "coverage_from", "coverage_through", "event_count"}, "concept": {"group", "taxonomy", "source_effective_date"}, "area": {"group", "taxonomy", "source_effective_date"},
}
POLICY = {
 "version": "20261106.1", "permanent": ["calendar", "basics", "industry", "daily"],
 "days": {"quotes": 7, "minutes": 30, "fund_flows": 30, "announcements": 90, "financials": 730, "actions": 730, "concept": 90, "area": 90, "policies": 730},
 "events_days": 7, "events_max_rows": 10000, "terminal_jobs_days": 30, "terminal_jobs_max_rows": 1000,
 "audit_days": 90, "audit_max_rows": 20000, "backups_max_count": 2, "backups_max_bytes": 1536 * 1024**2,
 "log_max_bytes": 5 * 1024**2, "log_backups": 2, "soft_database_bytes": 3 * 1024**3,
 "hard_database_bytes": 4 * 1024**3, "noncore_budget_bytes": 256 * 1024**2,
 "max_record_bytes": 65536, "max_announcement_text_chars": 12000, "pin_rule": "only_explicit_durable_evidence_refs",
 "failed_research_detail_days": 30,
}


def normalize(dataset, value):
    if dataset not in FIELDS:
        raise ValueError("未登记数据的保存规则")
    out = {k: v for k, v in value.items() if k in FIELDS[dataset] or k in {"day", "symbol", "key"}}
    out["key"] = str(out.get("key") or "")
    for field in ("pe", "pb"):
        if out.get(field) is None:
            out.pop(field, None)
    if dataset == "daily":
        out.setdefault("adjust", "raw")
        out.setdefault("volume_unit", "shares")
    if dataset == "announcements" and out.get("text"):
        text = str(out["text"])
        already_bounded = out.get("text_truncated") is True and isinstance(out.get("text_sha256"),str) and len(out["text_sha256"])==64
        if not already_bounded:
            out["text_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        out["text"] = text[:POLICY["max_announcement_text_chars"]]
        out["text_truncated"] = already_bounded or len(text) > len(out["text"])
    if len(store.dumps(out).encode()) > POLICY["max_record_bytes"]:
        raise ValueError("单条资料超过保存上限，未写入原始响应")
    return out


def content_hash(dataset, value):
    content=normalize(dataset,value)
    content.pop('text_extracted_at',None)
    return hashlib.sha256(store.dumps([dataset, content]).encode()).hexdigest()
