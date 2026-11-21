from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
from pathlib import Path
import tempfile
import unittest
import uuid
from datetime import datetime
from unittest.mock import patch

import httpx
from authenticated_client import admin_client
from foundation import assets, collection, configuration, database as store, pagination, sources
from foundation.main import app
from tools.mysql_instance import create_isolated_database, drop_isolated_database

_TEST_SCHEMA = "stock_test_" + uuid.uuid4().hex[:12]


def setUpModule():
    create_isolated_database(_TEST_SCHEMA)


def tearDownModule():
    drop_isolated_database(_TEST_SCHEMA)


def bar(close=10, stamp=None):
    v = {"day": "2026-11-04", "symbol": "sh.600000", "key": "", "open": 10, "high": 12,
         "low": 9, "close": close, "amount": 1000, "volume": 100, "adjust": "raw", "raw": {"close": str(close)}}
    if stamp:
        v["observed_at"] = stamp
    return v


def source(sid="bs_daily_all", ds="daily", op="daily_all"):
    return {"id": sid, "name": "隔离测试来源", "dataset": ds, "operation": op, "provider": "baostock",
            "enabled": True, "automatic": True, "implemented": True, "validation": "sample_passed", "min_interval": 2, "timeout": 10}


def seed():
    store.initialize()
    assets.save_source(source())
    assets.save_source(source("bs_profit", "financials", "profit"))
    assets.publish("calendar", [{"day": "2026-11-04", "symbol": "*", "key": "", "trading": True}], "bs_calendar")
    assets.publish("daily", [bar()], "bs_daily_all")
    with store.db() as c:
        c.execute("INSERT INTO models VALUES(1,'测试文本',%s,NULL)", (store.dumps({"name": "测试文本", "enabled": True, "model_identifier": "isolated-model", "protocol": "openai", "base_url": "https://example.invalid/v1", "capability": "文本"}),))
        c.execute("INSERT INTO plans VALUES(1,'测试方案',%s)", (store.dumps({"name": "测试方案", "enabled": True, "plan_type": "文本分析", "model_ids": [1], "description": ""}),))


class IsolatedCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = _TEST_SCHEMA

    @classmethod
    def tearDownClass(cls):
        pass

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="stock-foundation-test-")
        self.env = patch.dict(os.environ, {"STOCK_DATA_ROOT": self.temp.name, "STOCK_DISABLE_WORKER": "1", "STOCK_MYSQL_DATABASE": self.database})
        self.env.start()
        # Cursor security stays real, with a key belonging only to this fixture.
        signer = patch.object(pagination, "_key", return_value=secrets.token_bytes(32))
        signer.start()
        self.addCleanup(signer.stop)
        from foundation.backups import TABLES
        from foundation import retention
        retention._cache.clear()
        with store.db(admin=True) as c:
            self.assertTrue(self.database.startswith("stock_test_"))
            for table in reversed(TABLES):
                if table != "schema_versions":
                    c.execute("DELETE FROM `" + table + "`")
        seed()

    def tearDown(self):
        self.env.stop(); self.temp.cleanup()


class AssetTests(IsolatedCase):
    def test_repeated_acquisition_collapses_and_revision_survives(self):
        assets.publish("daily", [bar(stamp="2026-11-05T10:00:00+08:00")], "bs_daily_all")
        assets.publish("daily", [bar(11)], "bs_daily_all")
        with store.db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM facts WHERE dataset='daily'").fetchone()[0], 2)
            self.assertEqual(c.execute("SELECT count(*) FROM heads WHERE dataset='daily'").fetchone()[0], 1)
        self.assertEqual(assets.rows("daily")["items"][0]["close"], 11)

    def test_rejects_non_ashare_and_bad_ohlc_atomically(self):
        for v in (bar() | {"symbol": "bj.200020"}, bar(13)):
            with self.assertRaises(ValueError):
                assets.publish("daily", [v], "bs_daily_all")
        self.assertEqual(assets.rows("daily")["total"], 1)

    def test_true_suspension_is_preserved_without_fake_price(self):
        suspended = bar() | {"symbol": "sz.000001", "suspended": True, "open": None, "high": None, "low": None, "close": None, "amount": 0, "volume": 0}
        assets.publish("daily", [suspended], "bs_daily_all")
        self.assertIsNone(assets.rows("daily", stock="sz.000001")["items"][0]["close"])

    def test_financial_comparison_history_and_disclosure_rules(self):
        v = {"day": "2025-08-28", "published": "2025-08-28", "report_date": "2025-06-30", "symbol": "sh.600000", "key": "profit:2025-06-30", "fields": {"netprofit": ""}}
        assets.publish("financials", [v], "bs_profit")
        self.assertEqual(assets.rows("financials")["items"][0]["fields"]["netprofit"], "")
        with self.assertRaises(ValueError):
            assets.publish("financials", [v | {"published": "2025-01-01"}], "bs_profit")


class ConfigurationTests(IsolatedCase):
    def test_blank_secret_retains_encrypted_key_and_api_does_not_expose_it(self):
        v = {"name": "测试文本", "enabled": True, "model_identifier": "isolated-model", "protocol": "openai", "base_url": "https://example.invalid/v1", "api_key": "isolated-test-secret", "timeout_seconds": 500, "reasoning_effort": "xhigh", "max_output_tokens": None}
        configuration.save("models", v, 1)
        with store.db() as c:
            secret = c.execute("SELECT secret FROM models WHERE id=1").fetchone()[0]
        v["api_key"] = ""; configuration.save("models", v, 1)
        with store.db() as c:
            self.assertEqual(c.execute("SELECT secret FROM models WHERE id=1").fetchone()[0], secret)
        with admin_client(app) as client:
            reply = client.get("/api/ai")
            self.assertNotIn("isolated-test-secret", reply.text)
            self.assertTrue(reply.json()["models"][0]["has_api_key"])
            self.assertTrue(reply.json()["execution_enabled"])
            self.assertFalse(reply.json()["automatic_research_enabled"])

    def test_model_key_is_only_visible_in_same_origin_editor_and_probe_uses_saved_endpoint(self):
        key = "isolated-probe-secret"
        base = "https://example.invalid/v1"
        configuration.save("models", {"name": "测试文本", "model_identifier": "isolated-model",
                           "base_url": base, "api_key": key}, 1)
        local = "http://127.0.0.1:8001"
        headers = {"origin": local, "sec-fetch-site": "same-origin"}
        path = "/api/ai/models/"
        upstream = []
        def respond(request):
            upstream.append((request.method, str(request.url)))
            self.assertEqual(request.headers["Authorization"], f"Bearer {key}")
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "isolated-model"}, {"id": key}]})
            body = json.loads(request.content)
            self.assertEqual(body["model"], "isolated-model")
            self.assertEqual(body["max_completion_tokens"], 256)
            return httpx.Response(200, json={"id": key, "choices": [{"message": {"content": "ok"},
                "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 2, "echo": key}})
        real, async_real = httpx.Client, httpx.AsyncClient
        with admin_client(app, base_url=local, client=("127.0.0.1", 50001)) as client, patch.object(
                httpx, "Client", side_effect=lambda **kw: real(**kw, transport=httpx.MockTransport(respond))), patch.object(
                httpx, "AsyncClient", side_effect=lambda **kw: async_real(**kw, transport=httpx.MockTransport(respond))):
            self.assertEqual(client.post(path+"1/key").status_code, 403)
            self.assertEqual(client.post(path+"1/key", headers={"origin": "https://elsewhere.invalid"}).status_code, 403)
            reply = client.post(path+"1/key", headers=headers)
            self.assertEqual(reply.json(), {"api_key": key})
            self.assertEqual(reply.headers["cache-control"], "no-store")
            self.assertEqual(client.get(path+"1/key").status_code, 405)
            self.assertEqual(client.post(path+"test", headers=headers, json={"model_id": 1,
                "base_url": "https://different.invalid/v1"}).status_code, 422)
            self.assertEqual(upstream, [])
            reply = client.post(path+"catalog", headers=headers, json={"model_id": 1})
            self.assertEqual(reply.json()["models"], ["isolated-model"])
            self.assertEqual(client.get("/api/v2/usage").json()["attempts"], 0)
            reply = client.post(path+"test", headers=headers, json={"model_id": 1})
            self.assertTrue(reply.json()["ok"])
            calls = client.get("/api/v2/usage/calls")
            self.assertNotIn(key, calls.text)
            [record] = calls.json()["items"]
            self.assertEqual(record["id"], reply.json()["call_id"])
            self.assertEqual(record["model_id"], 1)
            self.assertEqual(record["data"]["origin"], "connection_test")
            self.assertEqual(record["data"]["usage_normalized"]["total"], 10)
            self.assertEqual(client.get("/api/v2/research/runs").json()["items"], [])
            self.assertEqual(upstream, [("GET", base+"/models"), ("POST", base+"/chat/completions")])

    def test_plan_order_persists_and_broken_links_reject(self):
        backup = configuration.save("models", {"name": "备用", "model_identifier": "backup", "protocol": "codex"})
        configuration.save("plans", {"name": "测试方案", "model_ids": [backup["id"], 1]}, 1)
        self.assertEqual(configuration.list_items("plans")[0]["model_ids"], [backup["id"], 1])
        with self.assertRaises(Exception):
            configuration.save("plans", {"name": "坏引用", "model_ids": [999]})


class CollectionTests(IsolatedCase):
    def test_schedules_run_without_retired_migration_settings(self):
        store.set_setting('automation_paused', False)
        engine = collection.Engine()
        engine.last_maintenance = 1
        schedule = {'id': 'isolated', 'enabled': True, 'at': '00:00', 'trading_only': False}
        with patch.object(collection.time, 'monotonic', return_value=1), \
             patch.object(assets, 'local_now', return_value=datetime(2026, 11, 4, 16, tzinfo=store.SHANGHAI)), \
             patch.object(assets, 'calendar', return_value={}), \
             patch.object(collection, 'schedules', return_value=[schedule]), \
             patch.object(collection, 'run_schedule', return_value={'id': 'isolated-job'}) as run:
            engine.tick()
        run.assert_called_once_with('isolated', manual=False)

    def test_current_universe_not_published_waits_without_empty_success(self):
        assets.save_source(source('bs_universe','basics','universe'))
        with patch.object(store,'today',return_value='2026-11-06'),patch.object(assets,'local_now',return_value=datetime(2026,11,6,10,tzinfo=store.SHANGHAI)):
            j=collection.submit({'source':'bs_universe','start':'2026-11-06','end':'2026-11-06'})
            engine=collection.Engine();job=engine.claim()
            with patch.object(sources,'collect',side_effect=sources.SourceError('当日名单尚未返回','empty')):
                engine.execute_unit(job)
        saved=collection.get_job(j['id'])
        self.assertEqual(saved['status'],'waiting')
        self.assertEqual(saved['done'],0)
        self.assertEqual(saved['retry_at'],datetime(2026,11,6,17,30,tzinfo=store.SHANGHAI).timestamp())
        self.assertEqual(assets.rows('basics')['total'],0)

    def test_completed_daily_scope_reuses_data_without_new_network_request(self):
        raw = {"source": "bs_daily_all", "start": "2026-11-04", "end": "2026-11-04"}
        collection.submit(raw); engine = collection.Engine(); first = engine.claim()
        with patch.object(sources, "collect", return_value=[bar()]):
            engine.execute_unit(first)
        second = collection.submit(raw); claimed = engine.claim()
        with patch.object(sources, "collect") as network:
            engine.execute_unit(claimed)
            network.assert_not_called()
        self.assertEqual(collection.get_job(second["id"])["reused"], 1)
        self.assertEqual(assets.rows("daily")["total"], 1)

    def test_full_quote_batch_does_not_write_an_oversized_collection_scope(self):
        assets.save_source(source('tx_quotes','quotes','quotes'))
        store.set_setting('automation_paused',False)
        symbols=[f'sh.{600000+i}' for i in range(90)]+[f'sz.{1+i:06d}' for i in range(110)]
        day='2026-11-04'
        stamp=day+'T11:29:00+08:00'
        quotes=[{**bar(),'symbol':symbol,'key':'20261104112900','source_time':stamp} for symbol in symbols]
        roster=symbols[:90]+[f'sh.{600090+i}' for i in range(150)]+symbols[90:]
        batches=collection.plan_items(assets.source('tx_quotes'),day,day,roster)
        self.assertEqual(sorted(code for item in batches for code in item['symbols']),sorted(roster))
        self.assertTrue(all(len(item['symbols'])<=200 and {code[:2] for code in item['symbols']}=={'sh','sz'} for item in batches))
        engine=collection.Engine()
        with patch.object(store,'today',return_value=day),patch.object(store,'now',return_value=stamp):
            job=collection.submit({'source':'tx_quotes','start':day,'end':day,'symbols':symbols},
                                  trigger='research_snapshot',expires_at=day+'T11:30:00+08:00')
            with patch.object(sources,'collect',return_value=quotes):
                engine.execute_unit(engine.claim())
        saved=collection.get_job(job['id'])
        self.assertEqual((saved['status'],saved['done'],saved['received']),('completed',1,200))
        self.assertEqual(assets.rows('quotes')['total'],200)
        with store.db(readonly=True) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM collection_units WHERE source='tx_quotes'").fetchone()[0],0)

    def test_selected_stock_does_not_publish_unrequested_securities(self):
        collection.submit({"source": "bs_daily_all", "start": "2026-11-04", "end": "2026-11-04", "symbols": ["sh.600000"]})
        engine = collection.Engine(); j = engine.claim()
        with patch.object(sources, "collect", return_value=[bar(), bar() | {"symbol": "sz.000001"}]):
            engine.execute_unit(j)
        self.assertEqual(assets.rows("daily")["total"], 1)

    def test_deadline_research_evidence_preserves_company_action_receipts(self):
        assets.save_source(source('bs_actions','actions','actions'))
        store.set_setting('automation_paused',False)
        day='2026-11-04'
        receipt={'day':day,'symbol':'sh.600000','key':'coverage:2026-08-01','record_kind':'coverage',
                 'coverage_from':'2026-08-01','coverage_through':day,'event_count':0}
        worker=collection.Engine()
        with patch.object(store,'now',return_value=day+'T12:00:00+08:00'):
            job=collection.submit({'source':'bs_actions','start':'2026-08-01','end':day,'symbols':['sh.600000']},
                                  trigger='research_evidence',expires_at=day+'T12:01:40+08:00')
            with patch.object(sources,'collect',return_value=[receipt]):
                worker.execute_unit(worker.claim())
        self.assertEqual(collection.get_job(job['id'])['received'],1)
        self.assertEqual(assets.rows('actions')['items'][0]['event_count'],0)

    def test_same_active_request_is_one_durable_job(self):
        raw = {"source": "bs_daily_all", "start": "2026-11-04", "end": "2026-11-04"}
        a = collection.submit(raw); b = collection.submit(raw)
        self.assertEqual(a["id"], b["id"])

    def test_network_failure_keeps_exact_unit_and_never_records_success(self):
        j = collection.submit({"source": "bs_daily_all", "start": "2026-11-04", "end": "2026-11-04"})
        engine = collection.Engine(); j = engine.claim()
        with patch.object(sources, "collect", side_effect=sources.SourceError("隔离网络失败", "network")):
            engine.execute_unit(j)
        result = collection.get_job(j["id"])
        self.assertEqual(result["status"], "waiting"); self.assertEqual(result["done"], 0)
        self.assertEqual(result["added"], 0)
        collection.control(j["id"], "pause")
        collection.control(j["id"], "resume")
        self.assertEqual(collection.get_job(j["id"])["done"], 0)

    def test_pause_during_network_does_not_publish_or_lose_manual_stop(self):
        j = collection.submit({"source": "bs_daily_all", "start": "2026-11-04", "end": "2026-11-04"})
        engine = collection.Engine(); j = engine.claim()
        def fake_collect(*args):
            collection.control(j["id"], "pause")
            return [bar(11)]
        with patch.object(sources, "collect", side_effect=fake_collect):
            engine.execute_unit(j)
        self.assertEqual(collection.get_job(j["id"])["status"], "paused")
        self.assertEqual(assets.rows("daily")["items"][0]["close"], 10)

    def test_unpublished_financials_stay_unknown(self):
        j = collection.submit({"source": "bs_profit", "start": "2026-11-04", "end": "2026-11-04", "symbols": ["sh.600000"]})
        engine = collection.Engine(); j = engine.claim()
        with patch.object(sources, "collect", side_effect=sources.SourceError("未返回财务", "empty")):
            engine.execute_unit(j)
        self.assertEqual(assets.rows("financials")["total"], 0)
        self.assertEqual(collection.get_job(j["id"])["no_data"], 1)

    def test_schedule_waits_for_watchlist_and_calendar(self):
        src = assets.source("bs_profit")
        with self.assertRaises(Exception):
            collection.plan_items(src, "2026-11-04", "2026-11-04", [], "financials")
        with self.assertRaises(Exception):
            collection.submit({"source": "bs_daily_all", "start": "2026-11-03", "end": "2026-11-03"})


class ApiTests(IsolatedCase):
    def test_retired_migration_api_and_overview_payload_are_absent(self):
        with admin_client(app) as client:
            self.assertEqual(client.get('/api/migration').status_code, 404)
            self.assertNotIn('migration', client.get('/api/overview').json())
            self.assertNotIn('/api/migration', client.get('/openapi.json').json()['paths'])

    def test_read_filters_pagination_export_and_origin_boundary(self):
        with admin_client(app) as client:
            self.assertEqual(client.get("/api/records?dataset=daily&symbol=sh.600000").json()["total"], 1)
            self.assertEqual(client.get("/api/records?start=2026-11-05&end=2026-11-04").status_code, 422)
            self.assertEqual(client.get("/api/records?limit=999").status_code, 422)
            csv = client.get("/api/export?dataset=daily&start=2026-11-04&end=2026-11-04")
            self.assertEqual(csv.status_code, 200); self.assertIn("sh.600000", csv.text)
            self.assertEqual(client.post("/api/collection/automation", json={"paused": False}, headers={"Origin": "https://outside.invalid"}).status_code, 403)
            self.assertEqual(client.get("/").status_code, 200)


class StorageTests(IsolatedCase):
    def test_stale_aggregate_cannot_hide_or_invent_records(self):
        assets.publish('daily',[bar()|{'symbol':'sz.000001'}],'bs_daily_all')
        with store.db() as c:
            c.execute("UPDATE asset_stats SET record_count=99,generation=0 WHERE dataset='daily'")
        self.assertEqual(assets.rows('daily')['total'],2)
        assets.refresh_stats('daily')
        self.assertEqual(assets.rows('daily')['total'],2)

    def test_concurrent_reads_keep_consistent_results_without_connection_exhaustion(self):
        from concurrent.futures import ThreadPoolExecutor
        def read(_):
            result=assets.rows('daily',limit=1)
            return result['total'],result['items'][0]['symbol']
        with ThreadPoolExecutor(max_workers=12) as executor:
            results=list(executor.map(read,range(48)))
        self.assertEqual(results,[(1,'sh.600000')]*48)

    def test_cursor_navigation_tampering_filter_binding_and_update_invalidation(self):
        from fastapi import HTTPException
        assets.publish('daily',[bar()|{'symbol':s} for s in ['sz.000001','sz.000002','sh.600001']], 'bs_daily_all')
        first=assets.rows('daily',limit=2)
        second=assets.rows('daily',limit=2,cursor=first['next_cursor'])
        self.assertEqual(len(first['items']),2)
        self.assertEqual(len(second['items']),2)
        self.assertFalse({r['_id'] for r in first['items']} & {r['_id'] for r in second['items']})
        for cursor,stock in [(first['next_cursor']+'x',None),(first['next_cursor'],'sh.600000')]:
            with self.assertRaises(HTTPException) as error:assets.rows('daily',stock=stock,limit=2,cursor=cursor)
            self.assertEqual(error.exception.status_code,422)
        assets.publish('daily',[bar(11)],'bs_daily_all')
        with self.assertRaises(HTTPException) as error:assets.rows('daily',limit=2,cursor=first['next_cursor'])
        self.assertEqual(error.exception.status_code,409)

    def test_same_values_from_second_source_do_not_duplicate_and_keep_provenance(self):
        assets.publish('daily',[bar()|{'raw':{'redundant':'ignore'}}],'tx_daily')
        with store.db(readonly=True) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM facts WHERE dataset='daily'").fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM fact_origins').fetchone()[0],2)

    def test_expiry_protects_cited_evidence_and_never_deletes_core_daily(self):
        from foundation import retention
        old=bar()|{'day':'2020-01-02','source_time':'2020-01-02T14:00:00'}
        assets.publish('quotes',[old,old|{'symbol':'sz.000001'}],'tx_quotes')
        assets.publish('daily',[bar()|{'day':'2020-01-02'}],'bs_daily_all')
        with store.db() as c:
            fid=c.execute("SELECT id FROM facts WHERE dataset='quotes' AND symbol='sh.600000'").fetchone()[0]
            c.execute("INSERT INTO evidence_refs VALUES('report','isolated',%s,%s)",(fid,store.now()))
        result=retention.cleanup()
        self.assertEqual(result['expired_facts'],1)
        self.assertEqual(assets.rows('quotes')['total'],1)
        self.assertEqual(assets.rows('daily')['total'],2)

    def test_backup_restores_in_independent_mysql_schema(self):
        from foundation import backups
        backup=backups.create_backup()
        name='stock_restore_'+uuid.uuid4().hex[:10]
        try:
            restored=backups.restore_isolated(Path(self.temp.name)/'backups'/backup['file'],name)
            self.assertEqual(restored['counts']['facts'],2)
            with store.db(readonly=True,database=name) as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM heads WHERE dataset='daily'").fetchone()[0],1)
        finally:
            drop_isolated_database(name)




if __name__ == "__main__":
    unittest.main()
