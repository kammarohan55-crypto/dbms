-- GRIDMIND fresh schema snapshot: 002_normalized_integrity. MySQL >= 8.0.16; InnoDB. Generated from models.py.


CREATE TABLE audit_logs (
	id VARCHAR(36) NOT NULL, 
	action VARCHAR(80) NOT NULL, 
	entity_id VARCHAR(100) NOT NULL, 
	created_at DATETIME NOT NULL, 
	detail JSON NOT NULL, 
	PRIMARY KEY (id)
)

 ENGINE=InnoDB;


CREATE TABLE data_sources (
	id VARCHAR(36) NOT NULL, 
	name VARCHAR(150) NOT NULL, 
	provider VARCHAR(150) NOT NULL, 
	source_type VARCHAR(40) NOT NULL, 
	frequency VARCHAR(40) NOT NULL, 
	units VARCHAR(30) NOT NULL, 
	url TEXT NOT NULL, 
	metadata_json JSON NOT NULL, 
	status VARCHAR(40) NOT NULL, 
	acquired_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
)

 ENGINE=InnoDB;


CREATE TABLE metric_definitions (
	name VARCHAR(80) NOT NULL, 
	unit VARCHAR(30) NOT NULL, 
	description TEXT NOT NULL, 
	PRIMARY KEY (name)
)

 ENGINE=InnoDB;


CREATE TABLE microgrid_assets (
	id VARCHAR(36) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	asset_type VARCHAR(40) NOT NULL, 
	capacity_kw FLOAT(53) NOT NULL, 
	capacity_kwh FLOAT(53) NOT NULL, 
	min_soc FLOAT(53) NOT NULL, 
	max_soc FLOAT(53) NOT NULL, 
	metadata_json JSON NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_asset_capacity CHECK (capacity_kw > 0 AND capacity_kwh >= 0), 
	CONSTRAINT ck_asset_soc CHECK (min_soc >= 0 AND max_soc <= 1 AND min_soc < max_soc), 
	CONSTRAINT ck_asset_type CHECK (asset_type IN ('battery','solar','wind','household','industrial','critical','ev')), 
	CONSTRAINT ck_battery_energy CHECK (asset_type != 'battery' OR capacity_kwh > 0), 
	UNIQUE (name)
)

 ENGINE=InnoDB;


CREATE TABLE regions (
	id VARCHAR(36) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	timezone VARCHAR(60) NOT NULL, 
	metadata_json JSON NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (name)
)

 ENGINE=InnoDB;


CREATE TABLE schema_migrations (
	version VARCHAR(50) NOT NULL, 
	applied_at DATETIME NOT NULL, 
	description VARCHAR(250) NOT NULL, 
	PRIMARY KEY (version)
)

 ENGINE=InnoDB;


CREATE TABLE energy_observations (
	id VARCHAR(36) NOT NULL, 
	source_id VARCHAR(36) NOT NULL, 
	region_id VARCHAR(36) NOT NULL, 
	series_key VARCHAR(100) NOT NULL, 
	observed_at DATETIME NOT NULL, 
	acquired_at DATETIME NOT NULL, 
	metric VARCHAR(60) NOT NULL, 
	value FLOAT(53) NOT NULL, 
	unit VARCHAR(30) NOT NULL, 
	quality_status VARCHAR(40) NOT NULL, 
	metadata_json JSON NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_observation_identity UNIQUE (source_id, region_id, series_key, metric, observed_at), 
	CONSTRAINT ck_observation_nonnegative CHECK (value >= 0), 
	FOREIGN KEY(source_id) REFERENCES data_sources (id), 
	FOREIGN KEY(region_id) REFERENCES regions (id)
)

 ENGINE=InnoDB;

CREATE INDEX ix_observation_region_time ON energy_observations (region_id, observed_at);

CREATE INDEX ix_observation_metric_time ON energy_observations (metric, observed_at);


CREATE TABLE market_participants (
	id VARCHAR(36) NOT NULL, 
	`key` VARCHAR(40) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	asset_id VARCHAR(36), 
	PRIMARY KEY (id), 
	UNIQUE (`key`), 
	FOREIGN KEY(asset_id) REFERENCES microgrid_assets (id)
)

 ENGINE=InnoDB;


CREATE TABLE model_registry (
	id VARCHAR(36) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	version VARCHAR(100) NOT NULL, 
	target VARCHAR(60) NOT NULL, 
	source_id VARCHAR(36) NOT NULL, 
	region_id VARCHAR(36) NOT NULL, 
	algorithm VARCHAR(80) NOT NULL, 
	metrics JSON NOT NULL, 
	metadata_json JSON NOT NULL, 
	artifact_path VARCHAR(500) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (version), 
	FOREIGN KEY(source_id) REFERENCES data_sources (id), 
	FOREIGN KEY(region_id) REFERENCES regions (id)
)

 ENGINE=InnoDB;


CREATE TABLE simulation_runs (
	id VARCHAR(36) NOT NULL, 
	comparison_id VARCHAR(36) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	policy VARCHAR(20) NOT NULL, 
	mode VARCHAR(30) NOT NULL, 
	source_id VARCHAR(36) NOT NULL, 
	model_id VARCHAR(36), 
	status VARCHAR(30) NOT NULL, 
	config JSON NOT NULL, 
	input_manifest JSON NOT NULL, 
	total_steps INTEGER NOT NULL, 
	completed_steps INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	started_at DATETIME, 
	finished_at DATETIME, 
	error TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_run_policy CHECK (policy IN ('baseline','gridmind')), 
	CONSTRAINT ck_run_status CHECK (status IN ('queued','running','paused','completed','failed','cancelled','interrupted')), 
	CONSTRAINT ck_run_progress CHECK (completed_steps >= 0 AND completed_steps <= total_steps), 
	FOREIGN KEY(source_id) REFERENCES data_sources (id), 
	FOREIGN KEY(model_id) REFERENCES model_registry (id)
)

 ENGINE=InnoDB;

CREATE INDEX ix_simulation_runs_comparison_id ON simulation_runs (comparison_id);

CREATE INDEX ix_simulation_runs_status ON simulation_runs (status);


CREATE TABLE forecasts (
	id VARCHAR(36) NOT NULL, 
	model_id VARCHAR(36) NOT NULL, 
	run_id VARCHAR(36), 
	generated_at DATETIME NOT NULL, 
	target_at DATETIME NOT NULL, 
	value FLOAT(53) NOT NULL, 
	lower FLOAT(53), 
	upper FLOAT(53), 
	metadata_json JSON NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_run_forecast UNIQUE (model_id, run_id, target_at), 
	FOREIGN KEY(model_id) REFERENCES model_registry (id), 
	FOREIGN KEY(run_id) REFERENCES simulation_runs (id)
)

 ENGINE=InnoDB;

CREATE INDEX ix_forecasts_target_at ON forecasts (target_at);


CREATE TABLE market_orders (
	id VARCHAR(36) NOT NULL, 
	run_id VARCHAR(36) NOT NULL, 
	step INTEGER NOT NULL, 
	participant_id VARCHAR(36) NOT NULL, 
	side VARCHAR(4) NOT NULL, 
	quantity_kwh FLOAT(53) NOT NULL, 
	remaining_kwh FLOAT(53) NOT NULL, 
	limit_price FLOAT(53) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_market_order UNIQUE (run_id, step, participant_id, side), 
	CONSTRAINT uq_order_scope UNIQUE (id, run_id, step), 
	CONSTRAINT ck_order_side CHECK (side IN ('buy','sell')), 
	CONSTRAINT ck_order_quantity_price CHECK (quantity_kwh > 0 AND limit_price >= 0), 
	CONSTRAINT ck_order_remaining CHECK (remaining_kwh >= 0 AND remaining_kwh <= quantity_kwh), 
	FOREIGN KEY(run_id) REFERENCES simulation_runs (id), 
	FOREIGN KEY(participant_id) REFERENCES market_participants (id)
)

 ENGINE=InnoDB;

CREATE INDEX ix_market_orders_run_id ON market_orders (run_id);

CREATE INDEX ix_order_book ON market_orders (run_id, step, side, limit_price);


CREATE TABLE run_metrics (
	run_id VARCHAR(36) NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	value FLOAT(53) NOT NULL, 
	PRIMARY KEY (run_id, name), 
	FOREIGN KEY(run_id) REFERENCES simulation_runs (id), 
	FOREIGN KEY(name) REFERENCES metric_definitions (name)
)

 ENGINE=InnoDB;


CREATE TABLE market_trades (
	id VARCHAR(36) NOT NULL, 
	run_id VARCHAR(36) NOT NULL, 
	step INTEGER NOT NULL, 
	buy_order_id VARCHAR(36) NOT NULL, 
	sell_order_id VARCHAR(36) NOT NULL, 
	quantity_kwh FLOAT(53) NOT NULL, 
	price FLOAT(53) NOT NULL, 
	settlement_status VARCHAR(20) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT fk_trade_buy_scope FOREIGN KEY(buy_order_id, run_id, step) REFERENCES market_orders (id, run_id, step), 
	CONSTRAINT fk_trade_sell_scope FOREIGN KEY(sell_order_id, run_id, step) REFERENCES market_orders (id, run_id, step), 
	CONSTRAINT uq_trade_pair UNIQUE (buy_order_id, sell_order_id), 
	CONSTRAINT ck_trade_quantity_price CHECK (quantity_kwh > 0 AND price >= 0), 
	CONSTRAINT ck_trade_distinct_orders CHECK (buy_order_id != sell_order_id), 
	FOREIGN KEY(run_id) REFERENCES simulation_runs (id)
)

 ENGINE=InnoDB;

CREATE INDEX ix_market_trades_run_id ON market_trades (run_id);
