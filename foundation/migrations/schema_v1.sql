-- Backup compatibility only. Never apply this schema to the running database.

-- Archived foundation/schema.sql SHA256 996a2386d07428dabfbb9047cb46e3636a1ed21488021f7dcdd7712333cb4256
-- Owned project schema, applied by the migration account, never the web account.
CREATE TABLE IF NOT EXISTS schema_versions(version INT PRIMARY KEY, applied_at VARCHAR(32) NOT NULL) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS facts(
 id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
 dataset VARCHAR(24) CHARACTER SET ascii NOT NULL, day CHAR(10) CHARACTER SET ascii NOT NULL,
 symbol VARCHAR(12) CHARACTER SET ascii NOT NULL, record_key VARCHAR(160) NOT NULL DEFAULT '',
 source VARCHAR(48) CHARACTER SET ascii NOT NULL, source_version VARCHAR(64) NOT NULL DEFAULT '',
 payload MEDIUMTEXT NOT NULL, fingerprint CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 first_seen VARCHAR(32) CHARACTER SET ascii NOT NULL, last_seen VARCHAR(32) CHARACTER SET ascii NOT NULL,
 acquisitions INT UNSIGNED NOT NULL DEFAULT 1, original_id BIGINT, last_original_id BIGINT,
 UNIQUE KEY fact_fingerprint(fingerprint), KEY fact_identity(dataset,day,symbol,record_key), KEY fact_retention(dataset,day,id), KEY fact_dataset_id(dataset,id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS heads(
 dataset VARCHAR(24) CHARACTER SET ascii NOT NULL, day CHAR(10) CHARACTER SET ascii NOT NULL,
 symbol VARCHAR(12) CHARACTER SET ascii NOT NULL, record_key VARCHAR(160) NOT NULL DEFAULT '', fact_id BIGINT UNSIGNED NOT NULL,
 PRIMARY KEY(dataset,day,symbol,record_key), KEY head_page(dataset,day DESC,symbol,record_key),
 KEY head_symbol(dataset,symbol,day DESC,record_key), CONSTRAINT head_fact FOREIGN KEY(fact_id) REFERENCES facts(id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS sources(id VARCHAR(48) CHARACTER SET ascii PRIMARY KEY, data MEDIUMTEXT NOT NULL) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS fact_origins(
 fact_id BIGINT UNSIGNED NOT NULL, source VARCHAR(48) CHARACTER SET ascii NOT NULL,
 source_version VARCHAR(64) NOT NULL DEFAULT '', first_seen VARCHAR(32) CHARACTER SET ascii NOT NULL,
 last_seen VARCHAR(32) CHARACTER SET ascii NOT NULL, PRIMARY KEY(fact_id,source),
 CONSTRAINT origin_fact FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS schedules(id VARCHAR(48) CHARACTER SET ascii PRIMARY KEY, data MEDIUMTEXT NOT NULL) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS settings(`key` VARCHAR(100) CHARACTER SET ascii PRIMARY KEY, data MEDIUMTEXT NOT NULL) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS models(id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT, name VARCHAR(120) NOT NULL UNIQUE, data MEDIUMTEXT NOT NULL, secret BLOB) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS plans(id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT, name VARCHAR(120) NOT NULL UNIQUE, data MEDIUMTEXT NOT NULL) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS agents(id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT, name VARCHAR(120) NOT NULL UNIQUE, data MEDIUMTEXT NOT NULL) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS collection_jobs(
 id CHAR(16) CHARACTER SET ascii PRIMARY KEY, identity CHAR(64) CHARACTER SET ascii NOT NULL,
 status VARCHAR(20) CHARACTER SET ascii NOT NULL, created VARCHAR(32) CHARACTER SET ascii NOT NULL, data MEDIUMTEXT NOT NULL,
 active_identity CHAR(64) CHARACTER SET ascii GENERATED ALWAYS AS (CASE WHEN status IN ('queued','running','waiting','paused') THEN identity ELSE NULL END) STORED,
 UNIQUE KEY active_job_identity(active_identity), KEY job_status(status,created), KEY job_page(created DESC,id DESC)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS collection_events(
 id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT, job_id CHAR(16) CHARACTER SET ascii NOT NULL,
 created VARCHAR(32) CHARACTER SET ascii NOT NULL, data TEXT NOT NULL, KEY event_job(job_id,id DESC), KEY event_expiry(created,id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS collection_units(
 source VARCHAR(48) CHARACTER SET ascii NOT NULL, scope VARCHAR(512) NOT NULL,
 checked VARCHAR(32) CHARACTER SET ascii NOT NULL, received INT UNSIGNED NOT NULL, PRIMARY KEY(source,scope)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS audit(
 id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT, created VARCHAR(32) CHARACTER SET ascii NOT NULL,
 action VARCHAR(100) NOT NULL, data TEXT NOT NULL, KEY audit_expiry(created,id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS asset_stats(dataset VARCHAR(24) CHARACTER SET ascii PRIMARY KEY, data TEXT NOT NULL) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS dataset_usage(dataset VARCHAR(24) CHARACTER SET ascii PRIMARY KEY, payload_bytes BIGINT NOT NULL, records BIGINT NOT NULL) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS collection_holds(
 dataset VARCHAR(24) CHARACTER SET ascii NOT NULL, day CHAR(10) CHARACTER SET ascii NOT NULL,
 symbol VARCHAR(12) CHARACTER SET ascii NOT NULL, PRIMARY KEY(dataset,day,symbol)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS classification_scopes(id BIGINT PRIMARY KEY, data TEXT NOT NULL) ENGINE=InnoDB;
-- Pin only facts actually cited by a durable decision, never a whole batch by default.
CREATE TABLE IF NOT EXISTS evidence_refs(
 owner_type VARCHAR(24) CHARACTER SET ascii NOT NULL, owner_id VARCHAR(64) CHARACTER SET ascii NOT NULL,
 fact_id BIGINT UNSIGNED NOT NULL, created VARCHAR(32) CHARACTER SET ascii NOT NULL,
 PRIMARY KEY(owner_type,owner_id,fact_id), KEY evidence_fact(fact_id),
 CONSTRAINT evidence_fact FOREIGN KEY(fact_id) REFERENCES facts(id)
) ENGINE=InnoDB;

-- Archived research/schema.sql SHA256 0fae6fa0ce0cfb6d82c21adb4e4f39a22a92e78e7b05f898a4de86308e064a8c
CREATE TABLE IF NOT EXISTS research_runs(
 id CHAR(16) CHARACTER SET ascii PRIMARY KEY, request_key CHAR(64) CHARACTER SET ascii NOT NULL UNIQUE,
 status VARCHAR(24) CHARACTER SET ascii NOT NULL, stage VARCHAR(40) NOT NULL,
 created VARCHAR(32) CHARACTER SET ascii NOT NULL, updated VARCHAR(32) CHARACTER SET ascii NOT NULL,
 market_day CHAR(10) CHARACTER SET ascii NOT NULL, kind VARCHAR(16) CHARACTER SET ascii NOT NULL,
 data MEDIUMTEXT NOT NULL, report MEDIUMTEXT,
 active_slot TINYINT GENERATED ALWAYS AS (CASE WHEN status IN ('queued','running','waiting') THEN 1 ELSE NULL END) STORED,
 UNIQUE KEY one_active_research(active_slot), KEY research_page(created DESC,id DESC)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS research_calls(
 id CHAR(16) CHARACTER SET ascii PRIMARY KEY, run_id CHAR(16) CHARACTER SET ascii NOT NULL,
 model_id BIGINT UNSIGNED NOT NULL, started VARCHAR(32) CHARACTER SET ascii NOT NULL, finished VARCHAR(32) CHARACTER SET ascii,
 status VARCHAR(24) CHARACTER SET ascii NOT NULL, data TEXT NOT NULL,
 KEY call_run(run_id,started), KEY call_page(started DESC,id DESC)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS research_cycles(
 id CHAR(16) CHARACTER SET ascii PRIMARY KEY, identity_key CHAR(64) CHARACTER SET ascii NOT NULL UNIQUE,
 run_id CHAR(16) CHARACTER SET ascii NOT NULL, symbol VARCHAR(12) CHARACTER SET ascii NOT NULL,
 market_day CHAR(10) CHARACTER SET ascii NOT NULL, created VARCHAR(32) CHARACTER SET ascii NOT NULL,
 updated VARCHAR(32) CHARACTER SET ascii NOT NULL, state VARCHAR(24) CHARACTER SET ascii NOT NULL,
 formal BOOLEAN NOT NULL, data MEDIUMTEXT NOT NULL,
 KEY cycle_page(created DESC,id DESC), KEY cycle_state(state,updated), KEY cycle_symbol(symbol,market_day)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS research_events(
 id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT, event_key CHAR(64) CHARACTER SET ascii NOT NULL UNIQUE,
 cycle_id CHAR(16) CHARACTER SET ascii NOT NULL, run_id CHAR(16) CHARACTER SET ascii,
 created VARCHAR(32) CHARACTER SET ascii NOT NULL, kind VARCHAR(24) CHARACTER SET ascii NOT NULL, data TEXT NOT NULL,
 KEY cycle_events(cycle_id,id), KEY review_run(run_id,id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS research_outcomes(
 cycle_id CHAR(16) CHARACTER SET ascii NOT NULL, horizon VARCHAR(8) CHARACTER SET ascii NOT NULL,
 kind VARCHAR(16) CHARACTER SET ascii NOT NULL, status VARCHAR(24) CHARACTER SET ascii NOT NULL,
 updated VARCHAR(32) CHARACTER SET ascii NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY(cycle_id,horizon,kind), KEY outcome_status(kind,status)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS research_controls(
 id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
 run_id CHAR(16) CHARACTER SET ascii NOT NULL, symbol VARCHAR(12) CHARACTER SET ascii NOT NULL,
 cohort VARCHAR(16) CHARACTER SET ascii NOT NULL, horizon VARCHAR(8) CHARACTER SET ascii NOT NULL,
 start_at VARCHAR(32) CHARACTER SET ascii NOT NULL, endpoint CHAR(10) CHARACTER SET ascii NOT NULL,
 status VARCHAR(24) CHARACTER SET ascii NOT NULL, data TEXT NOT NULL,
 UNIQUE KEY control_sample(run_id,symbol,horizon), KEY control_due(status,start_at,id), KEY control_page(id DESC)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS portfolio_fills(
 id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
 mode VARCHAR(8) CHARACTER SET ascii NOT NULL, source_key CHAR(64) CHARACTER SET ascii NOT NULL,
 symbol VARCHAR(12) CHARACTER SET ascii NOT NULL, side VARCHAR(12) CHARACTER SET ascii NOT NULL,
 filled_at VARCHAR(32) CHARACTER SET ascii NOT NULL, quantity INT UNSIGNED NOT NULL,
 price_ticks BIGINT UNSIGNED NOT NULL, fees_fen BIGINT UNSIGNED, cycle_id CHAR(16) CHARACTER SET ascii,
 created VARCHAR(32) CHARACTER SET ascii NOT NULL, voided_at VARCHAR(32) CHARACTER SET ascii,
 data TEXT NOT NULL,
 UNIQUE KEY fill_identity(mode,source_key), KEY fill_symbol(mode,symbol,filled_at,id),
 KEY fill_page(mode,filled_at DESC,id DESC)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS research_notices(
 id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
 notice_key CHAR(64) CHARACTER SET ascii NOT NULL UNIQUE,
 run_id CHAR(16) CHARACTER SET ascii NOT NULL,
 kind VARCHAR(16) CHARACTER SET ascii NOT NULL,
 created VARCHAR(32) CHARACTER SET ascii NOT NULL,
 read_at VARCHAR(32) CHARACTER SET ascii,
 channel_state VARCHAR(20) CHARACTER SET ascii NOT NULL,
 target_hash CHAR(64) CHARACTER SET ascii,
 data TEXT NOT NULL,
 KEY notice_run(run_id,kind,id), KEY notice_unread(read_at,id)
) ENGINE=InnoDB;
CREATE TABLE IF NOT EXISTS research_screening(
 run_id CHAR(16) CHARACTER SET ascii NOT NULL,
 sequence_no INT UNSIGNED NOT NULL,
 symbol VARCHAR(12) CHARACTER SET ascii NOT NULL,
 name VARCHAR(120) NOT NULL,
 path_mask SMALLINT UNSIGNED NOT NULL,
 selected BOOLEAN NOT NULL,
 price_move DOUBLE, relative_volume DOUBLE,
 PRIMARY KEY(run_id,sequence_no), UNIQUE KEY screening_symbol(run_id,symbol),
 KEY screening_stage(run_id,selected,sequence_no)
) ENGINE=InnoDB;
