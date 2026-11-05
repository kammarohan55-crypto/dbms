"""Normalized schema, direct-SQL safeguards, and evidence-backed upgrades.

Optional MySQL cases use only fixture-owned rows; no shared tables are dropped.
Legacy schema and raw-file recovery experiments use separate temporary SQLite.
"""
from datetime import datetime, timedelta
import hashlib
import struct
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from gridmind.database import initialize, make_engine
from gridmind.market import settle_step
from gridmind.migrations import VERSION, install_trade_guards, restore_raw_precision, schema_problems, seed_metrics, upgrade
from gridmind.models import AuditLog, DataSource, MarketOrder, MetricDefinition, Observation, Participant, Region, RunMetric, SchemaMigration, SimulationRun, Trade
from test_storage import database, proposed_orders


@pytest.fixture
def guarded_database(database):
    with database.engine.begin() as connection:
        seed_metrics(connection)
        install_trade_guards(connection)
    return database


@pytest.fixture
def isolated_sqlite(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'upgrade.db').as_posix()}")
    initialize(engine)
    factory = sessionmaker(engine,expire_on_commit=False)
    with factory.begin() as session:
        source = DataSource(name='Isolated upgrade source',provider='Explicit test fixture',units='kWh')
        region = Region(name='Isolated upgrade region',timezone='UTC')
        session.add_all([source,region])
        session.flush()
        run = SimulationRun(comparison_id=str(uuid4()),name='upgrade fixture',policy='baseline',
            source_id=source.id,config={},input_manifest={},total_steps=4)
        session.add(run)
        session.flush()
        identifiers = source.id,region.id,run.id
    yield engine,factory,identifiers
    engine.dispose()


def test_unknown_metric_rejected_and_unit_resolved_through_definition(guarded_database):
    db = guarded_database
    with pytest.raises(DBAPIError):
        with db.engine.begin() as connection:
            connection.execute(text('INSERT INTO run_metrics(run_id,name,value) VALUES (:run,:name,:value)'),
                {'run':db.run_id,'name':'undefined_'+str(uuid4())[:8],'value':1})
    with db.factory.begin() as session:
        session.add(RunMetric(run_id=db.run_id,name='operating_cost',value=123.456789012345))
    with db.engine.connect() as connection:
        row = connection.execute(text('SELECT metric,value,unit FROM v_run_comparison WHERE run_id=:run'),{'run':db.run_id}).one()
        assert row.metric == 'operating_cost' and row.unit == 'INR'
        assert row.value == 123.456789012345
    assert 'unit' not in {column['name'] for column in inspect(db.engine).get_columns('run_metrics')}
    with db.factory() as session:
        assert session.get(MetricDefinition,'operating_cost').unit == 'INR'


def test_fractional_quantities_round_trip_without_float32_loss(guarded_database):
    db = guarded_database
    quantity = 0.123456789012345
    with db.factory.begin() as session:
        session.add(Observation(source_id=db.source_id,region_id=db.region_id,metric='precision_probe',
            observed_at=datetime(2024,1,1),value=171.7,unit='GWh'))
        session.add(RunMetric(run_id=db.run_id,name='p2p_volume_kwh',value=quantity))
    settle_step(db.factory,db.run_id,0,[
        {'participant':'solar_coop','side':'sell','quantity_kwh':quantity,'limit_price':4.123456789012345},
        {'participant':'community','side':'buy','quantity_kwh':quantity,'limit_price':8.123456789012345},
    ],available_kwh=quantity,permitted_buy_kwh=quantity)
    with db.factory() as session:
        assert session.scalar(select(Observation.value).where(Observation.source_id==db.source_id)) == 171.7
        assert session.get(RunMetric,(db.run_id,'p2p_volume_kwh')).value == quantity
        trade = session.scalar(select(Trade).where(Trade.run_id==db.run_id))
        assert trade.quantity_kwh == quantity
        assert trade.price == (4.123456789012345 + 8.123456789012345) / 2
        orders = list(session.scalars(select(MarketOrder).where(MarketOrder.run_id==db.run_id)))
        assert all(order.quantity_kwh==quantity and order.remaining_kwh==0 for order in orders)
    if db.engine.dialect.name == 'mysql':
        for table,column in [('energy_observations','value'),('run_metrics','value'),('market_orders','quantity_kwh'),('market_trades','quantity_kwh')]:
            actual_type = next(item['type'] for item in inspect(db.engine).get_columns(table) if item['name']==column)
            assert str(actual_type).upper().startswith(('DOUBLE','FLOAT(53'))


@pytest.fixture
def order_book(guarded_database):
    db = guarded_database
    other_run_id = str(uuid4())
    with db.factory.begin() as session:
        participants = {item.key:item.id for item in session.scalars(select(Participant))}
        session.add(SimulationRun(id=other_run_id,comparison_id=str(uuid4()),name='Other isolated scope',
            policy='baseline',source_id=db.source_id,config={},input_manifest={},total_steps=4))
        session.flush()
        definitions = {
            'buy':('community','buy',6,8,0), 'other_buy':('critical','buy',8,9,0),
            'sell':('solar_coop','sell',10,4,0), 'other_sell':('critical','sell',10,4,0),
            'next_sell':('solar_coop','sell',10,4,1),
        }
        ids = {}
        for key,(participant,side,quantity,price,step) in definitions.items():
            order = MarketOrder(id=str(uuid4()),run_id=db.run_id,step=step,participant_id=participants[participant],
                side=side,quantity_kwh=quantity,remaining_kwh=quantity,limit_price=price)
            ids[key] = order.id
            session.add(order)
    try:
        yield db,ids,other_run_id
    finally:
        with db.factory.begin() as session:
            session.execute(delete(AuditLog).where(AuditLog.entity_id==other_run_id))
            session.execute(delete(SimulationRun).where(SimulationRun.id==other_run_id))


INSERT_TRADE = text('''INSERT INTO market_trades
    (id,run_id,step,buy_order_id,sell_order_id,quantity_kwh,price,settlement_status)
    VALUES (:id,:run_id,:step,:buy_order_id,:sell_order_id,:quantity_kwh,:price,'settled')''')
UPDATE_TRADE = text('''UPDATE market_trades SET run_id=:run_id,step=:step,buy_order_id=:buy_order_id,
    sell_order_id=:sell_order_id,quantity_kwh=:quantity_kwh,price=:price WHERE id=:id''')


@pytest.mark.parametrize('operation',['insert','update'])
def test_direct_sql_trade_guards_reject_invalid_scope_side_price_and_allocation(order_book,operation):
    db, ids, other_run_id = order_book
    base = {'id':str(uuid4()),'run_id':db.run_id,'step':0,'buy_order_id':ids['buy'],
            'sell_order_id':ids['sell'],'quantity_kwh':4.0,'price':6.0}
    if operation == 'update':
        with db.engine.begin() as connection:
            connection.execute(INSERT_TRADE,base)
    cases = [
        {'run_id':other_run_id}, {'step':1}, {'sell_order_id':ids['next_sell']},
        {'buy_order_id':ids['sell'],'sell_order_id':ids['other_buy']},
        {'buy_order_id':ids['other_buy'],'sell_order_id':ids['other_sell']},
        {'price':3.99}, {'price':8.01}, {'quantity_kwh':6.01},
        {'buy_order_id':ids['other_buy'],'quantity_kwh':10.01},
    ]
    for invalid in cases:
        with pytest.raises(DBAPIError):
            with db.engine.begin() as connection:
                connection.execute(INSERT_TRADE if operation=='insert' else UPDATE_TRADE,{**base,**invalid})
    with db.engine.begin() as connection:
        if operation == 'insert':
            connection.execute(INSERT_TRADE,base)
        else:
            connection.execute(UPDATE_TRADE,{**base,'price':7.0})
    with db.factory() as session:
        persisted = session.get(Trade,base['id'])
        assert persisted.run_id == db.run_id and persisted.step == 0 and persisted.quantity_kwh == 4
        assert persisted.price == (6 if operation=='insert' else 7)


@pytest.mark.parametrize('operation',['insert','update'])
def test_direct_sql_trade_guards_enforce_cumulative_allocations(order_book,operation):
    db, ids, _ = order_book
    existing = {'id':str(uuid4()),'run_id':db.run_id,'step':0,'buy_order_id':ids['buy'],
                'sell_order_id':ids['sell'],'quantity_kwh':4.0,'price':6.0}
    candidates = [
        {**existing,'id':str(uuid4()),'sell_order_id':ids['other_sell'],'quantity_kwh':3.0},
        {**existing,'id':str(uuid4()),'buy_order_id':ids['other_buy'],'quantity_kwh':7.0},
    ]
    with db.engine.begin() as connection:
        connection.execute(INSERT_TRADE,existing)
    for candidate in candidates:
        if operation == 'update':
            with db.engine.begin() as connection:
                connection.execute(INSERT_TRADE,{**candidate,'quantity_kwh':1.0})
        with pytest.raises(DBAPIError):
            with db.engine.begin() as connection:
                connection.execute(INSERT_TRADE if operation=='insert' else UPDATE_TRADE,candidate)
        if operation == 'update':
            with db.engine.begin() as connection:
                connection.execute(text('DELETE FROM market_trades WHERE id=:id'),candidate)
    with db.factory() as session:
        assert session.scalar(select(func.sum(Trade.quantity_kwh)).where(Trade.run_id==db.run_id)) == 4


def test_legacy_sqlite_upgrade_preserves_ids_metrics_units_and_retries(isolated_sqlite):
    engine,factory,(source_id,region_id,run_id) = isolated_sqlite
    observation_id = str(uuid4())
    with factory.begin() as session:
        session.add(Observation(id=observation_id,source_id=source_id,region_id=region_id,metric='original_series',
            observed_at=datetime(2024,1,1),value=171.7,unit='GWh'))
    # Only this disposable SQLite DB is given a historical schema shape.
    with engine.begin() as connection:
        connection.execute(text('DROP VIEW v_run_comparison'))
        connection.execute(text('DROP TABLE run_metrics'))
        connection.execute(text('''CREATE TABLE run_metrics (
            run_id VARCHAR(36) NOT NULL,name VARCHAR(80) NOT NULL,value FLOAT NOT NULL,unit VARCHAR(30) NOT NULL,
            PRIMARY KEY(run_id,name),FOREIGN KEY(run_id) REFERENCES simulation_runs(id))'''))
        connection.execute(text('INSERT INTO run_metrics(run_id,name,value,unit) VALUES (:run,:name,:value,:unit)'),
            [{'run':run_id,'name':'operating_cost','value':12.3456789012345,'unit':'INR'},
             {'run':run_id,'name':'legacy_custom_probe','value':0.123456789012345,'unit':'widgets'}])
    assert schema_problems(engine)
    for _ in range(2):
        result = upgrade(engine)
        assert result['data_preserved'] and result['version'] == VERSION
        assert schema_problems(engine) == []
        with factory() as session:
            assert session.get(SimulationRun,run_id).source_id == source_id
            observation = session.get(Observation,observation_id)
            assert observation.region_id == region_id and observation.value == 171.7 and observation.unit == 'GWh'
            assert session.get(RunMetric,(run_id,'legacy_custom_probe')).value == 0.123456789012345
            assert session.get(MetricDefinition,'legacy_custom_probe').unit == 'widgets'
            assert session.scalar(select(func.count()).select_from(SchemaMigration).where(SchemaMigration.version==VERSION)) == 1
        with engine.connect() as connection:
            assert not connection.exec_driver_sql('PRAGMA foreign_key_check').all()
            units = dict(connection.execute(text('SELECT metric,unit FROM v_run_comparison WHERE run_id=:run'),{'run':run_id}).all())
            assert units == {'operating_cost':'INR','legacy_custom_probe':'widgets'}
    with factory.begin() as session:
        session.get(SimulationRun,run_id).status = 'paused'


def test_precision_restore_requires_matching_checksum_identity_and_exact_float32(isolated_sqlite,tmp_path):
    engine,factory,(source_id,region_id,_) = isolated_sqlite
    original = 171.7
    rounded = struct.unpack('!f',struct.pack('!f',original))[0]
    with factory() as session:
        region_name = session.get(Region,region_id).name
    content = ('timestamp,value,unit,metric,region\n' + '\n'.join(
        f'{(datetime(2024,1,1)+timedelta(days=index)).isoformat()}Z,{original},kWh,load_kwh,{region_name}'
        for index in range(7)) + '\n').encode()
    raw_path = tmp_path / 'verified_measurements.csv'
    raw_path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    values = [rounded,rounded + 1e-10,rounded,rounded,original,rounded,rounded]
    ids = []
    with factory.begin() as session:
        session.get(DataSource,source_id).metadata_json = {'raw_file':str(raw_path),'raw_sha256':'incorrect-checksum'}
        for index,value in enumerate(values):
            observation = Observation(id=str(uuid4()),source_id=source_id,region_id=region_id,
                observed_at=datetime(2024,1,1)+timedelta(days=index),value=value,
                unit='GWh' if index==3 else 'kWh',series_key='different' if index==5 else 'regional',
                metric='other_metric' if index==6 else 'load_kwh',metadata_json={'raw_sha256':'wrong-evidence' if index==2 else digest})
            ids.append(observation.id)
            session.add(observation)
    assert restore_raw_precision(engine) == 0
    with factory.begin() as session:
        session.get(DataSource,source_id).metadata_json = {'raw_file':str(raw_path),'raw_sha256':digest}
    assert restore_raw_precision(engine) == 1
    assert restore_raw_precision(engine) == 0
    with factory() as session:
        observed = [session.get(Observation,identifier) for identifier in ids]
        assert observed[0].value == original
        assert observed[0].metadata_json['previous_float32_value'] == rounded
        assert [row.value for row in observed[1:]] == values[1:]
        audits = list(session.scalars(select(AuditLog).where(AuditLog.action=='raw_precision_recovery')))
        assert len(audits)==1 and audits[0].detail['observations']==1 and audits[0].detail['raw_sha256']==digest
    assert raw_path.read_bytes() == content


def test_market_retry_requires_the_same_order_payload(guarded_database):
    db = guarded_database
    proposals = proposed_orders()
    settle_step(db.factory,db.run_id,0,proposals,available_kwh=10,permitted_buy_kwh=10)
    settle_step(db.factory,db.run_id,0,proposals,available_kwh=10,permitted_buy_kwh=10)
    changed = [{**proposal,'limit_price':proposal['limit_price'] + 0.1} for proposal in proposals]
    with pytest.raises(ValueError,match='[Ii]dempotency|[Cc]onflict|[Dd]ifferent'):
        settle_step(db.factory,db.run_id,0,changed,available_kwh=10,permitted_buy_kwh=10)
    with db.factory() as session:
        assert session.scalar(select(func.count()).select_from(MarketOrder).where(MarketOrder.run_id==db.run_id)) == 3
        assert session.scalar(select(func.count()).select_from(Trade).where(Trade.run_id==db.run_id)) == 2


@pytest.mark.parametrize('invalid',[float('nan'),float('inf'),float('-inf')])
def test_market_rejects_nonfinite_available_or_buy_budgets(guarded_database,invalid):
    db = guarded_database
    for available,buy_limit in [(invalid,10),(10,invalid)]:
        with pytest.raises(ValueError):
            settle_step(db.factory,db.run_id,0,proposed_orders(),available_kwh=available,permitted_buy_kwh=buy_limit)
    with db.factory() as session:
        assert session.scalar(select(func.count()).select_from(MarketOrder).where(MarketOrder.run_id==db.run_id)) == 0
