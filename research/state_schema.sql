-- Relational research query state. Apply idempotently through state_store.apply_schema.
ALTER TABLE `research_runs` ADD COLUMN `session` VARCHAR(16) CHARACTER SET ascii NOT NULL DEFAULT 'close', ADD COLUMN `mode` VARCHAR(32) CHARACTER SET ascii NULL, ADD COLUMN `published_at` DATETIME(6) NULL, ADD COLUMN `withdrawn_at` DATETIME(6) NULL, ADD COLUMN `study_horizon` VARCHAR(8) CHARACTER SET ascii NULL, ADD COLUMN `target_version` VARCHAR(64) CHARACTER SET ascii NULL, ADD COLUMN `has_forecast_target` BOOLEAN NOT NULL DEFAULT 0, ADD COLUMN `has_bundle` BOOLEAN NOT NULL DEFAULT 0, ADD COLUMN `market_coverage` DOUBLE NULL, ADD COLUMN `market_analysis` MEDIUMTEXT NULL, ADD COLUMN `decision_count` INT UNSIGNED NOT NULL DEFAULT 0, ADD COLUMN `intent` TEXT NULL, ADD COLUMN `title` VARCHAR(512) NULL, ADD COLUMN `has_validated_report` BOOLEAN NOT NULL DEFAULT 0, ADD COLUMN `stop_requested` BOOLEAN NOT NULL DEFAULT 0, ADD COLUMN `detail_compacted_at` VARCHAR(32) CHARACTER SET ascii NULL, ADD COLUMN `rotation_retry_not_before` DOUBLE NULL;

ALTER TABLE `research_cycles` ADD COLUMN `action` VARCHAR(16) CHARACTER SET ascii NULL, ADD COLUMN `session` VARCHAR(16) CHARACTER SET ascii NOT NULL DEFAULT 'close', ADD COLUMN `rise_type` VARCHAR(16) CHARACTER SET ascii NULL, ADD COLUMN `rise_policy_version` VARCHAR(64) CHARACTER SET ascii NULL, ADD COLUMN `method_version` VARCHAR(64) CHARACTER SET ascii NULL, ADD COLUMN `thesis_key` VARCHAR(64) CHARACTER SET ascii NOT NULL DEFAULT 'structure', ADD COLUMN `research_horizon` VARCHAR(8) CHARACTER SET ascii NOT NULL DEFAULT 'D3', ADD COLUMN `first_removed_at` DATETIME(6) NULL, ADD COLUMN `end_day` DATE NULL, ADD COLUMN `pool_review_day` DATE NULL, ADD COLUMN `pool_review_fact_id` BIGINT UNSIGNED NULL, ADD COLUMN `pool_review_outcome` VARCHAR(32) CHARACTER SET ascii NULL, ADD COLUMN `pool_review_reason` TEXT NULL;

ALTER TABLE `research_outcomes` ADD COLUMN `direction` VARCHAR(8) CHARACTER SET ascii NULL, ADD COLUMN `return_value` DOUBLE NULL, ADD COLUMN `excess_return` DOUBLE NULL;

ALTER TABLE `research_controls` ADD COLUMN `method_version` VARCHAR(64) CHARACTER SET ascii NULL, ADD COLUMN `return_value` DOUBLE NULL, ADD COLUMN `target_version` VARCHAR(64) CHARACTER SET ascii NULL, ADD COLUMN `reference_basis` VARCHAR(32) CHARACTER SET ascii NULL;

ALTER TABLE `research_events` ADD COLUMN `to_state` VARCHAR(24) CHARACTER SET ascii NULL;

ALTER TABLE `research_notices` ADD COLUMN `action` VARCHAR(24) CHARACTER SET ascii NULL, ADD COLUMN `symbol` VARCHAR(12) CHARACTER SET ascii NULL;

ALTER TABLE `research_calls` ADD COLUMN `attempt_no` INT UNSIGNED NOT NULL DEFAULT 0, ADD COLUMN `recoverable` BOOLEAN NOT NULL DEFAULT 0, ADD COLUMN `usage_state` VARCHAR(16) CHARACTER SET ascii NULL, ADD COLUMN `input_tokens` BIGINT UNSIGNED NULL, ADD COLUMN `output_tokens` BIGINT UNSIGNED NULL, ADD COLUMN `total_tokens` BIGINT UNSIGNED NULL, ADD COLUMN `cached_input_tokens` BIGINT UNSIGNED NULL, ADD COLUMN `reasoning_output_tokens` BIGINT UNSIGNED NULL;

CREATE TABLE IF NOT EXISTS research_decisions(
 run_id CHAR(16) CHARACTER SET ascii NOT NULL,
 sequence_no SMALLINT UNSIGNED NOT NULL,
 symbol VARCHAR(12) CHARACTER SET ascii NOT NULL,
 name VARCHAR(512) NOT NULL,
 action VARCHAR(16) CHARACTER SET ascii NOT NULL,
 cycle_id CHAR(16) CHARACTER SET ascii NULL,
 review_event BOOLEAN NOT NULL,
 rise_type VARCHAR(16) CHARACTER SET ascii NULL,
 rise_name VARCHAR(64) NULL,
 stars TINYINT UNSIGNED NULL,
 PRIMARY KEY(run_id,sequence_no),
 KEY decision_symbol(symbol,run_id), KEY decision_cycle(cycle_id),
 KEY decision_rise(rise_type,run_id),
 CONSTRAINT decision_run FOREIGN KEY(run_id) REFERENCES research_runs(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS research_cycle_contracts(
 cycle_id CHAR(16) CHARACTER SET ascii PRIMARY KEY,
 present_fields INT UNSIGNED NOT NULL, null_fields INT UNSIGNED NOT NULL,
 entry_fields SMALLINT UNSIGNED NOT NULL, session_fields SMALLINT UNSIGNED NOT NULL,
 structure_fields SMALLINT UNSIGNED NOT NULL, rise_fields SMALLINT UNSIGNED NOT NULL,
 last_review_fields SMALLINT UNSIGNED NOT NULL,
 entry_low DOUBLE NULL, entry_high DOUBLE NULL, invalidate_below DOUBLE NULL, entry_condition TEXT NULL,
 invalidation TEXT NULL, published_at DATETIME(6) NULL, adjustment_verified BOOLEAN NULL,
 reference_from DATETIME(6) NULL, action_valid_until DATETIME(6) NULL,
 structure_below DOUBLE NULL, rise_trigger_above DOUBLE NULL, rise_invalidation_price DOUBLE NULL,
 last_review_at DATETIME(6) NULL, evidence_through DATETIME(6) NULL,
 endpoint_count SMALLINT UNSIGNED NOT NULL, entry_peer_count SMALLINT UNSIGNED NOT NULL,
 removal_peer_count SMALLINT UNSIGNED NOT NULL,
 CONSTRAINT contract_cycle FOREIGN KEY(cycle_id) REFERENCES research_cycles(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS research_cycle_endpoints(
 cycle_id CHAR(16) CHARACTER SET ascii NOT NULL, horizon VARCHAR(8) CHARACTER SET ascii NOT NULL,
 sequence_no SMALLINT UNSIGNED NOT NULL, endpoint DATE NULL,
 PRIMARY KEY(cycle_id,horizon), KEY endpoint_day(endpoint,cycle_id),
 CONSTRAINT endpoint_cycle FOREIGN KEY(cycle_id) REFERENCES research_cycles(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS research_cycle_references(
 cycle_id CHAR(16) CHARACTER SET ascii NOT NULL, reference_kind VARCHAR(8) CHARACTER SET ascii NOT NULL,
 present_fields SMALLINT UNSIGNED NOT NULL,
 fact_id BIGINT UNSIGNED NULL, quoted_at DATETIME(6) NULL, price DOUBLE NULL,
 confirmed_at DATETIME(6) NULL, price_basis VARCHAR(64) CHARACTER SET ascii NULL,
 symbol VARCHAR(12) CHARACTER SET ascii NULL, conditions_confirmed BOOLEAN NULL,
 adjustment_verified BOOLEAN NULL,
 PRIMARY KEY(cycle_id,reference_kind),
 CONSTRAINT reference_cycle FOREIGN KEY(cycle_id) REFERENCES research_cycles(id) ON DELETE CASCADE,
 CONSTRAINT reference_fact FOREIGN KEY(fact_id) REFERENCES facts(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS research_cycle_peer_references(
 cycle_id CHAR(16) CHARACTER SET ascii NOT NULL, reference_kind VARCHAR(8) CHARACTER SET ascii NOT NULL,
 sequence_no SMALLINT UNSIGNED NOT NULL, present_fields SMALLINT UNSIGNED NOT NULL,
 symbol VARCHAR(12) CHARACTER SET ascii NULL, fact_id BIGINT UNSIGNED NULL,
 quoted_at DATETIME(6) NULL, price DOUBLE NULL,
 PRIMARY KEY(cycle_id,reference_kind,sequence_no),
 CONSTRAINT peer_reference_cycle FOREIGN KEY(cycle_id) REFERENCES research_cycles(id) ON DELETE CASCADE,
 CONSTRAINT peer_reference_fact FOREIGN KEY(fact_id) REFERENCES facts(id)
) ENGINE=InnoDB;
