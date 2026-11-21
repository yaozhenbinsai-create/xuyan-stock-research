-- Typed business facts. Generated from foundation.record_store.schema_sql().
CREATE TABLE IF NOT EXISTS `calendar_records` (
 fact_id BIGINT UNSIGNED PRIMARY KEY,
 present_fields BIGINT UNSIGNED NOT NULL,
 integer_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 empty_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 negative_zero_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 `trading` BOOLEAN NULL,
 markets_null BOOLEAN NOT NULL DEFAULT 0,
 CONSTRAINT `calendar_records_fact` FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `security_details` (
 id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
 content_hash BINARY(32) NOT NULL UNIQUE,
 present_fields BIGINT UNSIGNED NOT NULL,
 integer_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 empty_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 negative_zero_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 `name` VARCHAR(512) NULL,
 `listed` DATE NULL,
 `delisted` DATE NULL,
 `status` VARCHAR(512) NULL,
 `board` VARCHAR(512) NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `classification_values` (
 id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
 content_hash BINARY(32) NOT NULL UNIQUE,
 present_fields BIGINT UNSIGNED NOT NULL,
 integer_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 empty_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 negative_zero_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 `group` VARCHAR(512) NULL,
 `taxonomy` VARCHAR(512) NULL,
 `source_effective_date` DATE NULL,
 `historical_effective_unknown` BOOLEAN NULL,
 `unclassified` BOOLEAN NULL
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `daily_bars` (
 fact_id BIGINT UNSIGNED PRIMARY KEY,
 present_fields BIGINT UNSIGNED NOT NULL,
 integer_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 empty_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 negative_zero_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 `open` DOUBLE NULL,
 `open_exact_integer` DECIMAL(65,0) NULL,
 `high` DOUBLE NULL,
 `high_exact_integer` DECIMAL(65,0) NULL,
 `low` DOUBLE NULL,
 `low_exact_integer` DECIMAL(65,0) NULL,
 `close` DOUBLE NULL,
 `close_exact_integer` DECIMAL(65,0) NULL,
 `preclose` DOUBLE NULL,
 `preclose_exact_integer` DECIMAL(65,0) NULL,
 `volume` DOUBLE NULL,
 `volume_exact_integer` DECIMAL(65,0) NULL,
 `amount` DOUBLE NULL,
 `amount_exact_integer` DECIMAL(65,0) NULL,
 `turnover` DOUBLE NULL,
 `turnover_exact_integer` DECIMAL(65,0) NULL,
 `pe` DOUBLE NULL,
 `pe_exact_integer` DECIMAL(65,0) NULL,
 `pb` DOUBLE NULL,
 `pb_exact_integer` DECIMAL(65,0) NULL,
 `suspended` BOOLEAN NULL,
 `is_st` BOOLEAN NULL,
 `adjust` VARCHAR(512) NULL,
 `volume_unit` VARCHAR(512) NULL,
 CONSTRAINT `daily_bars_fact` FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `quote_snapshots` (
 fact_id BIGINT UNSIGNED PRIMARY KEY,
 present_fields BIGINT UNSIGNED NOT NULL,
 integer_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 empty_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 negative_zero_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 `open` DOUBLE NULL,
 `open_exact_integer` DECIMAL(65,0) NULL,
 `high` DOUBLE NULL,
 `high_exact_integer` DECIMAL(65,0) NULL,
 `low` DOUBLE NULL,
 `low_exact_integer` DECIMAL(65,0) NULL,
 `close` DOUBLE NULL,
 `close_exact_integer` DECIMAL(65,0) NULL,
 `preclose` DOUBLE NULL,
 `preclose_exact_integer` DECIMAL(65,0) NULL,
 `volume` DOUBLE NULL,
 `volume_exact_integer` DECIMAL(65,0) NULL,
 `amount` DOUBLE NULL,
 `amount_exact_integer` DECIMAL(65,0) NULL,
 `turnover` DOUBLE NULL,
 `turnover_exact_integer` DECIMAL(65,0) NULL,
 `pe` DOUBLE NULL,
 `pe_exact_integer` DECIMAL(65,0) NULL,
 `pb` DOUBLE NULL,
 `pb_exact_integer` DECIMAL(65,0) NULL,
 `name` VARCHAR(512) NULL,
 `source_time` DATETIME(6) NULL,
 `source_time_style` TINYINT UNSIGNED NULL,
 `source_time_precision` TINYINT UNSIGNED NULL,
 `source_time_offset` VARCHAR(6) CHARACTER SET ascii NULL,
 `volume_unit` VARCHAR(512) NULL,
 CONSTRAINT `quote_snapshots_fact` FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE,
 KEY quote_time(source_time,fact_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `minute_bars` (
 fact_id BIGINT UNSIGNED PRIMARY KEY,
 present_fields BIGINT UNSIGNED NOT NULL,
 integer_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 empty_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 negative_zero_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 `open` DOUBLE NULL,
 `open_exact_integer` DECIMAL(65,0) NULL,
 `high` DOUBLE NULL,
 `high_exact_integer` DECIMAL(65,0) NULL,
 `low` DOUBLE NULL,
 `low_exact_integer` DECIMAL(65,0) NULL,
 `close` DOUBLE NULL,
 `close_exact_integer` DECIMAL(65,0) NULL,
 `volume` DOUBLE NULL,
 `volume_exact_integer` DECIMAL(65,0) NULL,
 `amount` DOUBLE NULL,
 `amount_exact_integer` DECIMAL(65,0) NULL,
 `adjust` VARCHAR(512) NULL,
 `source_time` DATETIME(6) NULL,
 `source_time_style` TINYINT UNSIGNED NULL,
 `source_time_precision` TINYINT UNSIGNED NULL,
 `source_time_offset` VARCHAR(6) CHARACTER SET ascii NULL,
 `period` BIGINT NULL,
 `closed` BOOLEAN NULL,
 `volume_unit` VARCHAR(512) NULL,
 CONSTRAINT `minute_bars_fact` FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE,
 KEY minute_time(source_time,fact_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `financial_reports` (
 fact_id BIGINT UNSIGNED PRIMARY KEY,
 present_fields BIGINT UNSIGNED NOT NULL,
 integer_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 empty_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 negative_zero_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 `report_type` VARCHAR(512) NULL,
 `report_date` DATE NULL,
 `published` DATE NULL,
 fields_null BOOLEAN NOT NULL DEFAULT 0,
 CONSTRAINT `financial_reports_fact` FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `announcements` (
 fact_id BIGINT UNSIGNED PRIMARY KEY,
 present_fields BIGINT UNSIGNED NOT NULL,
 integer_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 empty_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 negative_zero_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 `title` MEDIUMTEXT NULL,
 `published` DATETIME(6) NULL,
 `published_style` TINYINT UNSIGNED NULL,
 `published_precision` TINYINT UNSIGNED NULL,
 `published_offset` VARCHAR(6) CHARACTER SET ascii NULL,
 `url` MEDIUMTEXT NULL,
 `text` MEDIUMTEXT NULL,
 `text_extracted_at` DATETIME(6) NULL,
 `text_extracted_at_style` TINYINT UNSIGNED NULL,
 `text_extracted_at_precision` TINYINT UNSIGNED NULL,
 `text_extracted_at_offset` VARCHAR(6) CHARACTER SET ascii NULL,
 `text_sha256` CHAR(64) CHARACTER SET ascii NULL,
 `text_truncated` BOOLEAN NULL,
 `document_sha256` CHAR(64) CHARACTER SET ascii NULL,
 `text_pages` BIGINT NULL,
 `total_pages` BIGINT NULL,
 `text_scope` MEDIUMTEXT NULL,
 CONSTRAINT `announcements_fact` FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE,
 KEY announcement_published(published,fact_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `policy_documents` (
 fact_id BIGINT UNSIGNED PRIMARY KEY,
 present_fields BIGINT UNSIGNED NOT NULL,
 integer_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 empty_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 negative_zero_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 `title` MEDIUMTEXT NULL,
 `published` DATETIME(6) NULL,
 `published_style` TINYINT UNSIGNED NULL,
 `published_precision` TINYINT UNSIGNED NULL,
 `published_offset` VARCHAR(6) CHARACTER SET ascii NULL,
 `url` MEDIUMTEXT NULL,
 `text` MEDIUMTEXT NULL,
 `document_sha256` CHAR(64) CHARACTER SET ascii NULL,
 `effective_day` DATE NULL,
 `text_scope` MEDIUMTEXT NULL,
 CONSTRAINT `policy_documents_fact` FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `corporate_actions` (
 fact_id BIGINT UNSIGNED PRIMARY KEY,
 present_fields BIGINT UNSIGNED NOT NULL,
 integer_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 empty_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 negative_zero_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 `record_kind` VARCHAR(512) NULL,
 `coverage_from` DATE NULL,
 `coverage_through` DATE NULL,
 `event_count` BIGINT NULL,
 fields_null BOOLEAN NOT NULL DEFAULT 0,
 CONSTRAINT `corporate_actions_fact` FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `fund_flow_records` (
 fact_id BIGINT UNSIGNED PRIMARY KEY,
 present_fields BIGINT UNSIGNED NOT NULL,
 integer_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 empty_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 negative_zero_fields BIGINT UNSIGNED NOT NULL DEFAULT 0,
 `net_main` DOUBLE NULL,
 `net_main_exact_integer` DECIMAL(65,0) NULL,
 `net_large` DOUBLE NULL,
 `net_large_exact_integer` DECIMAL(65,0) NULL,
 `net_super` DOUBLE NULL,
 `net_super_exact_integer` DECIMAL(65,0) NULL,
 `net_medium` DOUBLE NULL,
 `net_medium_exact_integer` DECIMAL(65,0) NULL,
 `net_small` DOUBLE NULL,
 `net_small_exact_integer` DECIMAL(65,0) NULL,
 `ratio_main` DOUBLE NULL,
 `ratio_main_exact_integer` DECIMAL(65,0) NULL,
 `unit` VARCHAR(512) NULL,
 `basis` VARCHAR(512) NULL,
 `source_time` DATETIME(6) NULL,
 `source_time_style` TINYINT UNSIGNED NULL,
 `source_time_precision` TINYINT UNSIGNED NULL,
 `source_time_offset` VARCHAR(6) CHARACTER SET ascii NULL,
 `time_precision` VARCHAR(512) NULL,
 CONSTRAINT `fund_flow_records_fact` FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `security_records` (
 fact_id BIGINT UNSIGNED PRIMARY KEY,
 security_id BIGINT UNSIGNED NOT NULL,
 CONSTRAINT `security_records_fact` FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE,
 CONSTRAINT `security_records_detail` FOREIGN KEY(security_id) REFERENCES `security_details`(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `classification_records` (
 fact_id BIGINT UNSIGNED PRIMARY KEY,
 classification_id BIGINT UNSIGNED NOT NULL,
 CONSTRAINT `classification_records_fact` FOREIGN KEY(fact_id) REFERENCES facts(id) ON DELETE CASCADE,
 CONSTRAINT `classification_records_detail` FOREIGN KEY(classification_id) REFERENCES `classification_values`(id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS calendar_markets (
 fact_id BIGINT UNSIGNED NOT NULL,
 position SMALLINT UNSIGNED NOT NULL,
 market VARCHAR(32) NOT NULL,
 PRIMARY KEY(fact_id,position),
 CONSTRAINT calendar_market_record FOREIGN KEY(fact_id) REFERENCES calendar_records(fact_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `financial_values` (
 fact_id BIGINT UNSIGNED NOT NULL,
 field_name VARCHAR(128) NOT NULL,
 value_kind ENUM('null','text','number','integer','boolean','negative_zero') NOT NULL,
 text_value MEDIUMTEXT NULL,
 number_value DOUBLE NULL,
 integer_value DECIMAL(65,0) NULL,
 boolean_value BOOLEAN NULL,
 numeric_value DECIMAL(65,30) NULL,
 PRIMARY KEY(fact_id,field_name),
 CONSTRAINT `financial_values_record` FOREIGN KEY(fact_id) REFERENCES `financial_reports`(fact_id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `action_values` (
 fact_id BIGINT UNSIGNED NOT NULL,
 field_name VARCHAR(128) NOT NULL,
 value_kind ENUM('null','text','number','integer','boolean','negative_zero') NOT NULL,
 text_value MEDIUMTEXT NULL,
 number_value DOUBLE NULL,
 integer_value DECIMAL(65,0) NULL,
 boolean_value BOOLEAN NULL,
 numeric_value DECIMAL(65,30) NULL,
 PRIMARY KEY(fact_id,field_name),
 CONSTRAINT `action_values_record` FOREIGN KEY(fact_id) REFERENCES `corporate_actions`(fact_id) ON DELETE CASCADE
) ENGINE=InnoDB;
