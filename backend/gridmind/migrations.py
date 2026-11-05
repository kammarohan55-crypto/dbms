"""Inspectable, retryable schema upgrades. Existing user data is preserved.

MySQL DDL commits implicitly. Each phase inspects its state so an interrupted
upgrade can be retried; the version is recorded only after all checks succeed.
"""
from sqlalchemy import inspect, select, text, Float
from .models import Base, MetricDefinition, SchemaMigration, utcnow
from .metric_definitions import METRICS

VERSION = '002_normalized_integrity'
DESCRIPTION = 'Normalize metric definitions; double precision; scoped trade integrity; paused lifecycle'
VIEW_SQL = '''CREATE VIEW v_run_comparison AS
SELECT r.comparison_id,r.id AS run_id,r.policy,r.status,m.name AS metric,m.value,d.unit
FROM simulation_runs r JOIN run_metrics m ON m.run_id=r.id
JOIN metric_definitions d ON d.name=m.name'''


def schema_problems(engine):
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if 'run_metrics' not in table_names:
        return []
    problems = []
    if any(column['name']=='unit' for column in inspector.get_columns('run_metrics')):
        problems.append('metric units still duplicated in results')
    if not any(fk['referred_table']=='metric_definitions' for fk in inspector.get_foreign_keys('run_metrics')):
        problems.append('metric definition reference missing')
    if not any(c['name']=='uq_order_scope' for c in inspector.get_unique_constraints('market_orders')):
        problems.append('order scope constraint missing')
    fks = {fk['name'] for fk in inspector.get_foreign_keys('market_trades')}
    if not {'fk_trade_buy_scope','fk_trade_sell_scope'} <= fks:
        problems.append('trade run/step references missing')
    checks = inspector.get_check_constraints('simulation_runs')
    if not any(c['name']=='ck_run_status' and 'paused' in c['sqltext'] for c in checks):
        problems.append('paused status not supported')
    if engine.dialect.name=='mysql':
        for table in Base.metadata.sorted_tables:
            if table.name not in table_names:
                problems.append(f'{table.name} missing')
                continue
            floats = {c.name for c in table.columns if isinstance(c.type,Float)}
            if any(c['name'] in floats and not str(c['type']).upper().startswith(('DOUBLE','FLOAT(53'))
                   for c in inspector.get_columns(table.name)):
                problems.append(f'{table.name} still uses single precision')
    return problems


def seed_metrics(connection):
    existing = dict(connection.execute(select(MetricDefinition.name,MetricDefinition.unit)).all())
    for name,(unit,description) in METRICS.items():
        if name in existing:
            if existing[name]!=unit:
                raise RuntimeError(f'Metric definition has inconsistent unit: {name}')
        else:
            connection.execute(MetricDefinition.__table__.insert().values(name=name,unit=unit,description=description))


def trade_guard_statement(operation, dialect):
    """DDL shared by runtime initialization and version-controlled SQL export."""
    if operation not in {'INSERT','UPDATE'}:
        raise ValueError('Unsupported trade guard operation')
    valid = '''EXISTS (SELECT 1 FROM market_orders b JOIN market_orders s ON s.id=NEW.sell_order_id
      WHERE b.id=NEW.buy_order_id AND b.side='buy' AND s.side='sell'
      AND b.run_id=NEW.run_id AND s.run_id=NEW.run_id AND b.step=NEW.step AND s.step=NEW.step
      AND b.participant_id<>s.participant_id AND NEW.price>=s.limit_price AND NEW.price<=b.limit_price
      AND NEW.quantity_kwh <= b.quantity_kwh - (SELECT COALESCE(SUM(t.quantity_kwh),0)
           FROM market_trades t WHERE t.buy_order_id=NEW.buy_order_id AND t.id<>NEW.id) + 0.0000001
      AND NEW.quantity_kwh <= s.quantity_kwh - (SELECT COALESCE(SUM(t.quantity_kwh),0)
           FROM market_trades t WHERE t.sell_order_id=NEW.sell_order_id AND t.id<>NEW.id) + 0.0000001)'''
    name = 'trg_trade_guard_'+operation.lower()
    if dialect=='mysql':
        return f"CREATE TRIGGER {name} BEFORE {operation} ON market_trades FOR EACH ROW BEGIN IF NOT ({valid}) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Invalid trade sides, price, scope, or allocation'; END IF; END"
    return f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {operation} ON market_trades FOR EACH ROW WHEN NOT ({valid}) BEGIN SELECT RAISE(ABORT,'Invalid trade sides, price, scope, or allocation'); END"


def install_trade_guards(connection):
    """Protect SQL clients too: sides, scope, prices, self-trades, total allocation."""
    for operation in ['INSERT','UPDATE']:
        name = 'trg_trade_guard_'+operation.lower()
        if connection.dialect.name=='mysql':
            found = connection.execute(text('SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME=:name'),{'name':name}).scalar()
            if not found:
                connection.execute(text(trade_guard_statement(operation,'mysql')))
        else:
            connection.execute(text(trade_guard_statement(operation,'sqlite')))


def record_version(connection):
    if not connection.execute(select(SchemaMigration.version).where(SchemaMigration.version==VERSION)).scalar():
        connection.execute(SchemaMigration.__table__.insert().values(version=VERSION,applied_at=utcnow(),description=DESCRIPTION))


def restore_raw_precision(engine):
    """Recover float32-rounded observations only from matching raw-file evidence.

    A value is replaced only when it equals the exact float32 representation of
    the checksum-verified raw measurement. Different readings are never merged.
    """
    import hashlib
    from pathlib import Path
    import struct
    from sqlalchemy.orm import sessionmaker
    from .models import DataSource, Observation, Region, AuditLog
    from .public_data import fetch_karnataka, parse_csv
    from .ingestion import parse_time
    total = 0
    with sessionmaker(engine).begin() as session:
        for source in session.scalars(select(DataSource)):
            metadata = {**source.metadata_json,**source.metadata_json.get('metadata',{})}
            filename = metadata.get('raw_file',metadata.get('raw_path'))
            digest = metadata.get('raw_sha256',metadata.get('sha256'))
            if not filename or not digest or not Path(filename).is_file():
                continue
            path = Path(filename)
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest()!=digest:
                continue
            if metadata.get('metric')=='supplied_energy_gwh' and path.name=='corrected_daily_energy_met_MU.csv':
                records,_ = fetch_karnataka(path.parent)
            else:
                zone = metadata.get('time_semantics','UTC').split('interpreted in ')[-1]
                try:
                    records,_ = parse_csv(content,region=metadata.get('geographic_scope','Custom'),timezone_name=zone)
                except (ValueError,KeyError):
                    continue
            raw = {}
            for record in records:
                region_name = record.get('region',metadata.get('region','Custom'))
                series = record.get('series_key',region_name.split('/',1)[1] if region_name.startswith('London/') else 'regional')
                raw[(region_name,series,record['metric'],parse_time(record['observed_at']))] = record
            recovered = 0
            for observation,region_name in session.execute(select(Observation,Region.name).join(Region).where(Observation.source_id==source.id)):
                record = raw.get((region_name,observation.series_key,observation.metric,observation.observed_at))
                evidence = observation.metadata_json.get('raw_sha256')
                if not record or evidence!=digest or record['unit']!=observation.unit:
                    continue
                original = float(record['value'])
                old = observation.value
                if old!=original and old==struct.unpack('!f',struct.pack('!f',original))[0]:
                    observation.value = original
                    observation.metadata_json = {**observation.metadata_json,'precision_recovered_from':'float32','previous_float32_value':old}
                    recovered += 1
            if recovered:
                session.add(AuditLog(action='raw_precision_recovery',entity_id=source.id,detail={'observations':recovered,'raw_sha256':digest,'reason':'Restore exact raw measurements after FLOAT to DOUBLE schema upgrade.'}))
            total += recovered
    return total


def upgrade(engine):
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    legacy_unit = any(c['name']=='unit' for c in inspector.get_columns('run_metrics'))
    with engine.begin() as connection:
        # Units become facts about a metric, not repeated facts about each run.
        seed_metrics(connection)
        if legacy_unit:
            units = connection.execute(text('SELECT DISTINCT name,unit FROM run_metrics')).all()
            known = dict(connection.execute(select(MetricDefinition.name,MetricDefinition.unit)).all())
            for name,unit in units:
                if name in known and known[name]!=unit:
                    raise RuntimeError(f'Cannot normalize conflicting metric units: {name}')
                if name not in known:
                    connection.execute(MetricDefinition.__table__.insert().values(name=name,unit=unit,description='Preserved legacy metric.'))
                    known[name] = unit
        connection.execute(text('DROP VIEW IF EXISTS v_run_comparison'))

    if engine.dialect.name=='mysql':
        with engine.begin() as connection:
            if legacy_unit:
                connection.execute(text('ALTER TABLE run_metrics DROP COLUMN unit'))
            inspector = inspect(connection)
            if not any(fk['referred_table']=='metric_definitions' for fk in inspector.get_foreign_keys('run_metrics')):
                connection.execute(text('ALTER TABLE run_metrics ADD CONSTRAINT fk_result_metric FOREIGN KEY (name) REFERENCES metric_definitions(name)'))
            for table in Base.metadata.sorted_tables:
                floats = {c.name for c in table.columns if isinstance(c.type,Float)}
                for column in inspect(connection).get_columns(table.name):
                    if column['name'] in floats and not str(column['type']).upper().startswith(('DOUBLE','FLOAT(53')):
                        nullability = 'NULL' if column['nullable'] else 'NOT NULL'
                        connection.execute(text(f"ALTER TABLE `{table.name}` MODIFY COLUMN `{column['name']}` DOUBLE {nullability}"))
            checks = inspect(connection).get_check_constraints('simulation_runs')
            if not any(c['name']=='ck_run_status' and 'paused' in c['sqltext'] for c in checks):
                connection.execute(text("ALTER TABLE simulation_runs DROP CHECK ck_run_status, ADD CONSTRAINT ck_run_status CHECK (status IN ('queued','running','paused','completed','failed','cancelled','interrupted'))"))
            if not any(c['name']=='uq_order_scope' for c in inspect(connection).get_unique_constraints('market_orders')):
                connection.execute(text('ALTER TABLE market_orders ADD CONSTRAINT uq_order_scope UNIQUE (id,run_id,step)'))
            for side in ['buy','sell']:
                name = f'fk_trade_{side}_scope'
                if not any(fk['name']==name for fk in inspect(connection).get_foreign_keys('market_trades')):
                    connection.execute(text(f'ALTER TABLE market_trades ADD CONSTRAINT {name} FOREIGN KEY ({side}_order_id,run_id,step) REFERENCES market_orders(id,run_id,step)'))
    elif schema_problems(engine):
        # SQLite is a development adapter. Rebuild only affected relations; rows
        # and application IDs are copied exactly, without resetting the database.
        from sqlalchemy.schema import CreateTable
        tables = ['run_metrics','market_orders','market_trades','simulation_runs']
        # SQLite requires a separate connection with FK checks disabled before BEGIN.
        with engine.connect() as connection:
            connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
            connection.commit()
            try:
                with connection.begin():
                    # Existing guards reference order tables. SQLite validates
                    # trigger SQL during table rename, so restore these guards
                    # only after every related table has its final name.
                    connection.execute(text('DROP TRIGGER IF EXISTS trg_trade_guard_insert'))
                    connection.execute(text('DROP TRIGGER IF EXISTS trg_trade_guard_update'))
                    for name in tables:
                        table = Base.metadata.tables[name]
                        ddl = str(CreateTable(table).compile(dialect=engine.dialect))
                        ddl = ddl.replace(f'CREATE TABLE {name}',f'CREATE TABLE {name}_upgrade',1)
                        connection.execute(text(ddl))
                        columns = ','.join(f'"{column.name}"' for column in table.columns)
                        connection.execute(text(f'INSERT INTO {name}_upgrade ({columns}) SELECT {columns} FROM {name}'))
                    for name in tables:
                        connection.execute(text(f'DROP TABLE {name}'))
                        connection.execute(text(f'ALTER TABLE {name}_upgrade RENAME TO {name}'))
                    from sqlalchemy.schema import CreateIndex
                    for name in tables:
                        for index in Base.metadata.tables[name].indexes:
                            connection.execute(CreateIndex(index))
                    if connection.exec_driver_sql('PRAGMA foreign_key_check').all():
                        raise RuntimeError('Migration would violate foreign keys')
            finally:
                connection.exec_driver_sql('PRAGMA foreign_keys=ON')
                connection.commit()
    problems = schema_problems(engine)
    if problems:
        raise RuntimeError('Migration incomplete: '+ '; '.join(problems))
    with engine.begin() as connection:
        connection.execute(text(VIEW_SQL))
        install_trade_guards(connection)
        record_version(connection)
    recovered = restore_raw_precision(engine)
    return {'version':VERSION,'status':'applied','data_preserved':True,'raw_precision_recovered':recovered}
