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
