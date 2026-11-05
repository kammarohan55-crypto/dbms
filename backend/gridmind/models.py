"""Normalized relational ownership. Operational state belongs to the event store."""
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import String, DateTime, Float, Integer, JSON, ForeignKey, ForeignKeyConstraint, UniqueConstraint, CheckConstraint, Index, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid():
    return str(uuid4())


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class DataSource(Base):
    __tablename__ = 'data_sources'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(150), unique=True)
    provider: Mapped[str] = mapped_column(String(150))
    source_type: Mapped[str] = mapped_column(String(40), default='historical_csv')
    frequency: Mapped[str] = mapped_column(String(40), default='daily')
    units: Mapped[str] = mapped_column(String(30))
    url: Mapped[str] = mapped_column(Text, default='')
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(40), default='available')
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Region(Base):
    __tablename__ = 'regions'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    timezone: Mapped[str] = mapped_column(String(60), default='Asia/Kolkata')
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)


class Observation(Base):
    __tablename__ = 'energy_observations'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_id: Mapped[str] = mapped_column(ForeignKey('data_sources.id'))
    region_id: Mapped[str] = mapped_column(ForeignKey('regions.id'))
    series_key: Mapped[str] = mapped_column(String(100), default='regional')
    observed_at: Mapped[datetime] = mapped_column(DateTime)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    metric: Mapped[str] = mapped_column(String(60))
    value: Mapped[float] = mapped_column(Float(53))
    unit: Mapped[str] = mapped_column(String(30))
    quality_status: Mapped[str] = mapped_column(String(40), default='valid')
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        UniqueConstraint('source_id', 'region_id', 'series_key', 'metric', 'observed_at', name='uq_observation_identity'),
        CheckConstraint('value >= 0', name='ck_observation_nonnegative'),
        Index('ix_observation_region_time', 'region_id', 'observed_at'),
        Index('ix_observation_metric_time', 'metric', 'observed_at'),
    )


class Asset(Base):
    __tablename__ = 'microgrid_assets'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    asset_type: Mapped[str] = mapped_column(String(40))
    capacity_kw: Mapped[float] = mapped_column(Float(53))
    capacity_kwh: Mapped[float] = mapped_column(Float(53), default=0)
    min_soc: Mapped[float] = mapped_column(Float(53), default=.15)
    max_soc: Mapped[float] = mapped_column(Float(53), default=.95)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (
        CheckConstraint('capacity_kw > 0 AND capacity_kwh >= 0', name='ck_asset_capacity'),
        CheckConstraint('min_soc >= 0 AND max_soc <= 1 AND min_soc < max_soc', name='ck_asset_soc'),
        CheckConstraint("asset_type IN ('battery','solar','wind','household','industrial','critical','ev')", name='ck_asset_type'),
        CheckConstraint("asset_type != 'battery' OR capacity_kwh > 0", name='ck_battery_energy'),
    )


class ModelRecord(Base):
    __tablename__ = 'model_registry'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[str] = mapped_column(String(100), unique=True)
    target: Mapped[str] = mapped_column(String(60))
    source_id: Mapped[str] = mapped_column(ForeignKey('data_sources.id'))
    region_id: Mapped[str] = mapped_column(ForeignKey('regions.id'))
    algorithm: Mapped[str] = mapped_column(String(80))
    metrics: Mapped[dict] = mapped_column(JSON)
    metadata_json: Mapped[dict] = mapped_column(JSON)
    artifact_path: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SimulationRun(Base):
    __tablename__ = 'simulation_runs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    comparison_id: Mapped[str] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(100))
    policy: Mapped[str] = mapped_column(String(20))
    mode: Mapped[str] = mapped_column(String(30), default='historical')
    source_id: Mapped[str] = mapped_column(ForeignKey('data_sources.id'))
    model_id: Mapped[str | None] = mapped_column(ForeignKey('model_registry.id'), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default='queued', index=True)
    config: Mapped[dict] = mapped_column(JSON)
    input_manifest: Mapped[dict] = mapped_column(JSON)
    total_steps: Mapped[int] = mapped_column(Integer)
    completed_steps: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint("policy IN ('baseline','gridmind')", name='ck_run_policy'),
        CheckConstraint("status IN ('queued','running','paused','completed','failed','cancelled','interrupted')", name='ck_run_status'),
        CheckConstraint('completed_steps >= 0 AND completed_steps <= total_steps', name='ck_run_progress'),
    )


class Forecast(Base):
    __tablename__ = 'forecasts'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    model_id: Mapped[str] = mapped_column(ForeignKey('model_registry.id'))
    run_id: Mapped[str | None] = mapped_column(ForeignKey('simulation_runs.id'), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    target_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    value: Mapped[float] = mapped_column(Float(53))
    lower: Mapped[float | None] = mapped_column(Float(53), nullable=True)
    upper: Mapped[float | None] = mapped_column(Float(53), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint('model_id', 'run_id', 'target_at', name='uq_run_forecast'),)


class Participant(Base):
    __tablename__ = 'market_participants'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    key: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    asset_id: Mapped[str | None] = mapped_column(ForeignKey('microgrid_assets.id'), nullable=True)


class MarketOrder(Base):
    __tablename__ = 'market_orders'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey('simulation_runs.id'), index=True)
    step: Mapped[int] = mapped_column(Integer)
    participant_id: Mapped[str] = mapped_column(ForeignKey('market_participants.id'))
    side: Mapped[str] = mapped_column(String(4))
    quantity_kwh: Mapped[float] = mapped_column(Float(53))
    remaining_kwh: Mapped[float] = mapped_column(Float(53))
    limit_price: Mapped[float] = mapped_column(Float(53))
    status: Mapped[str] = mapped_column(String(20), default='open')
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        UniqueConstraint('run_id','step','participant_id','side', name='uq_market_order'),
        UniqueConstraint('id','run_id','step', name='uq_order_scope'),
        CheckConstraint("side IN ('buy','sell')", name='ck_order_side'),
        CheckConstraint('quantity_kwh > 0 AND limit_price >= 0', name='ck_order_quantity_price'),
        CheckConstraint('remaining_kwh >= 0 AND remaining_kwh <= quantity_kwh', name='ck_order_remaining'),
        Index('ix_order_book','run_id','step','side','limit_price'),
    )


class Trade(Base):
    __tablename__ = 'market_trades'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey('simulation_runs.id'), index=True)
    step: Mapped[int] = mapped_column(Integer)
    buy_order_id: Mapped[str] = mapped_column(String(36))
    sell_order_id: Mapped[str] = mapped_column(String(36))
    quantity_kwh: Mapped[float] = mapped_column(Float(53))
    price: Mapped[float] = mapped_column(Float(53))
    settlement_status: Mapped[str] = mapped_column(String(20), default='settled')
    __table_args__ = (
        ForeignKeyConstraint(['buy_order_id','run_id','step'], ['market_orders.id','market_orders.run_id','market_orders.step'], name='fk_trade_buy_scope'),
        ForeignKeyConstraint(['sell_order_id','run_id','step'], ['market_orders.id','market_orders.run_id','market_orders.step'], name='fk_trade_sell_scope'),
        UniqueConstraint('buy_order_id','sell_order_id', name='uq_trade_pair'),
        CheckConstraint('quantity_kwh > 0 AND price >= 0', name='ck_trade_quantity_price'),
        CheckConstraint('buy_order_id != sell_order_id', name='ck_trade_distinct_orders'),
    )


class MetricDefinition(Base):
    __tablename__ = 'metric_definitions'
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    unit: Mapped[str] = mapped_column(String(30))
    description: Mapped[str] = mapped_column(Text)


class RunMetric(Base):
    __tablename__ = 'run_metrics'
    run_id: Mapped[str] = mapped_column(ForeignKey('simulation_runs.id'), primary_key=True)
    name: Mapped[str] = mapped_column(ForeignKey('metric_definitions.name'), primary_key=True)
    value: Mapped[float] = mapped_column(Float(53))


class SchemaMigration(Base):
    __tablename__ = 'schema_migrations'
    version: Mapped[str] = mapped_column(String(50), primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    description: Mapped[str] = mapped_column(String(250))


class AuditLog(Base):
    __tablename__ = 'audit_logs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    action: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    detail: Mapped[dict] = mapped_column(JSON)
