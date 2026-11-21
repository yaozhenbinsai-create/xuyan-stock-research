"""Bounded retention, capacity visibility, and reference-aware fact removal."""
from datetime import timedelta
from pathlib import Path
import threading
import time
from fastapi import HTTPException
from foundation import assets, database as store, deployment, storage_policy

_lock = threading.Lock()
_cache = {}
P = storage_policy.POLICY
TERMINAL = ("completed", "partial", "cancelled", "failed", "interrupted")


def status(*, fresh=False):
    cache_key = store.connection_options()["database"]
    cached = _cache.get(cache_key)
    if not fresh and cached and time.monotonic() - cached[0] < 60:
        return cached[1]
    with store.db(readonly=True) as c:
        c.execute("SET SESSION information_schema_stats_expiry=0")  # MySQL 8 caches table stats for 24h by default
        tables = [dict(r) for r in c.execute("SELECT TABLE_NAME AS name,TABLE_ROWS AS estimated_rows,DATA_LENGTH+INDEX_LENGTH AS bytes FROM information_schema.tables WHERE table_schema=DATABASE() ORDER BY DATA_LENGTH+INDEX_LENGTH DESC")]
        usage = {r[0]: {"payload_bytes": r[1], "records": r[2]} for r in c.execute("SELECT dataset,payload_bytes,records FROM dataset_usage")}
    size = sum(int(t["bytes"] or 0) for t in tables)
    backup_dir = deployment.backups_path()
    backups = [{"file": p.name, "bytes": p.stat().st_size} for p in backup_dir.glob("stock-mysql-*.jsonl.gz") if p.is_file() and not p.is_symlink()]
    result = {"engine": "mysql", "database_bytes": size, "usage": usage, "tables": tables,
              "backups": backups, "backup_bytes": sum(b["bytes"] for b in backups), "policy": P,
              "level": "full" if size >= P["hard_database_bytes"] else "warning" if size >= P["soft_database_bytes"] else "normal",
              "last_cleanup": store.setting("last_retention_cleanup")}
    instance=deployment.server_path()
    result['instance_physical_bytes']=sum(p.stat().st_size for p in instance.rglob('*') if p.is_file() and not p.is_symlink()) if instance.is_dir() else None
    result['size_basis']='information_schema table/index estimate; instance_physical_bytes includes allocated pages, redo and undo'
    _cache[cache_key] = (time.monotonic(), result)
    return result


def ensure_capacity(dataset):
    v = status()
    if v["level"] == "full":
        raise HTTPException(409, "数据库已达到容量保护线，请先检查容量；已有基础事实保持完整")
    optional = sum(x["payload_bytes"] for k, x in v["usage"].items() if k not in P["permanent"])
    if dataset not in P["permanent"] and optional >= P["noncore_budget_bytes"]:
        raise HTTPException(409, "短期资料已达到保存预算，请先清理到期资料")


def account(c, dataset, byte_count, records):
    c.execute("INSERT INTO dataset_usage VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE payload_bytes=GREATEST(0,CAST(payload_bytes AS SIGNED)+VALUES(payload_bytes)),records=GREATEST(0,CAST(records AS SIGNED)+VALUES(records))", (dataset, byte_count, records))


def _prune_table(table, date_column, days, maximum, extra="1=1"):
    cutoff = (assets.local_now() - timedelta(days=days)).isoformat(timespec="seconds")
    removed = 0
    with store.db() as c:
        # Existence and identifiers are fixed by this module, never HTTP input.
        boundary = c.execute(f"SELECT id FROM {table} WHERE {extra} ORDER BY id DESC LIMIT 1 OFFSET %s", (maximum-1,)).fetchone()
        clause = f"({date_column}<%s" + (" OR id<%s)" if boundary else ")")
        params = (cutoff, boundary[0]) if boundary else (cutoff,)
        removed = c.execute(f"DELETE FROM {table} WHERE ({extra}) AND {clause} LIMIT 5000", params).rowcount
    return removed


def cleanup():
    if not _lock.acquire(blocking=False):
        return {"status": "already_running"}
    try:
        receipt = {"at": store.now(), "expired_facts": 0, "protected_facts": 0}
        pending_cutoff=(assets.local_now()-timedelta(days=P['terminal_jobs_days'])).isoformat(timespec='seconds')
        with store.db() as c:
            c.execute("DELETE FROM evidence_refs WHERE owner_type='financial_pending' AND created<%s",(pending_cutoff,))
        for dataset, days in P["days"].items():
            cutoff = (assets.local_now().date() - timedelta(days=days)).isoformat()
            # At most 5,000 facts per type per pass. No unbounded transaction.
            for _ in range(10):
                with store.db() as c:
                    rows = c.execute("SELECT f.id,f.payload_bytes AS bytes FROM facts f WHERE f.dataset=%s AND f.day<%s AND NOT EXISTS(SELECT 1 FROM evidence_refs e WHERE e.fact_id=f.id) ORDER BY f.day,f.id LIMIT 500 FOR UPDATE", (dataset, cutoff)).fetchall()
                    if not rows:
                        break
                    ids = tuple(r[0] for r in rows)
                    marks = ",".join(["%s"] * len(ids))
                    if dataset=='financials':
                        c.execute("DELETE FROM evidence_refs WHERE owner_type='financial_filing' AND owner_id IN ("+marks+")",
                                  tuple(str(identifier) for identifier in ids))
                    c.execute("DELETE FROM heads WHERE fact_id IN (" + marks + ")", ids)
                    c.execute("DELETE FROM facts WHERE id IN (" + marks + ")", ids)
                    if dataset == 'policies':
                        c.execute("DELETE FROM settings WHERE `key` IN ("+marks+")",tuple(f'policy_terms:{fid}' for fid in ids))
                        assets.bump_generation(c,'policy_terms')
                    account(c, dataset, -sum(r[1] for r in rows), -len(rows))
                    assets.bump_generation(c, dataset)
                    receipt["expired_facts"] += len(rows)
            assets.refresh_stats(dataset)
        receipt["events"] = _prune_table("collection_events", "created", P["events_days"], P["events_max_rows"])
        receipt["audit"] = _prune_table("audit", "created", P["audit_days"], P["audit_max_rows"])
        # Preserve failed/interrupted jobs so they can still be resumed for 30 days.
        terminal_sql = "status IN ('completed','partial','cancelled','failed','interrupted')"
        cutoff = (assets.local_now()-timedelta(days=P["terminal_jobs_days"])).isoformat(timespec="seconds")
        with store.db() as c:
            # created/id order is the same order used by the paged task list.
            boundary = c.execute("SELECT created,id FROM collection_jobs WHERE " + terminal_sql + " ORDER BY created DESC,id DESC LIMIT 1 OFFSET %s", (P["terminal_jobs_max_rows"]-1,)).fetchone()
            sql = "DELETE FROM collection_jobs WHERE " + terminal_sql + " AND (created<%s"
            params = [cutoff]
            if boundary:
                sql += " OR created<%s OR (created=%s AND id<%s)"
                params += [boundary[0], boundary[0], boundary[1]]
            receipt["jobs"] = c.execute(sql + ") LIMIT 1000", params).rowcount
            receipt["protected_facts"] = c.execute("SELECT count(DISTINCT fact_id) FROM evidence_refs").fetchone()[0]
            store.set_setting("last_retention_cleanup", receipt, conn=c)
        # Log rotation and backup rotation have independent physical limits.
        _cache.clear()
        return receipt
    finally:
        _lock.release()
