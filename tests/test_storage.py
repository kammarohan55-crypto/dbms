"""Relational transactions and operational-event integration checks.

Set GRIDMIND_TEST_MYSQL_URL to run the same relational checks on MySQL 8+.
Only records created by each test are removed; tables are never dropped.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from gridmind.database import initialize, make_engine
from gridmind.events import DynamoEventStore, LocalEventStore, event_record
from gridmind.market import settle_step
from gridmind.models import (
    Asset, AuditLog, Base, DataSource, Forecast, MarketOrder, Observation,
    Participant, Region, RunMetric, SimulationRun, Trade,
)
from gridmind.runner import RunService
from gridmind.simulation import Scenario


@dataclass
class DatabaseFixture:
    engine: object
    factory: object
    source_id: str
    region_id: str
    run_id: str


@pytest.fixture(params=["sqlite", "mysql"])
def database(request, tmp_path):
    url = f"sqlite:///{(tmp_path / 'relational.db').as_posix()}"
    if request.param == "mysql":
        url = os.environ.get("GRIDMIND_TEST_MYSQL_URL")
        if not url:
            pytest.skip("Set GRIDMIND_TEST_MYSQL_URL for MySQL constraint/transaction tests")
    engine = make_engine(url)
    if request.param == "sqlite":
        initialize(engine)
    else:
        # Avoid initialize's global interrupted-run recovery against a shared DB.
        Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    source_id, region_id, run_id = (str(uuid4()) for _ in range(3))
    created_participants = []
    with factory.begin() as session:
        for key in ("community", "solar_coop", "critical"):
            if not session.scalar(select(Participant).where(Participant.key == key)):
                participant = Participant(id=str(uuid4()), key=key, name=f"Integration test {key}")
                session.add(participant)
                created_participants.append(participant.id)
        session.add(DataSource(id=source_id, name=f"Test source {source_id}", provider="test fixture",
                               units="MU", source_type="historical_csv"))
        session.add(Region(id=region_id, name=f"Test region {region_id}"))
        session.flush()
        session.add(SimulationRun(id=run_id, comparison_id=str(uuid4()), name="normal", policy="gridmind",
                                  source_id=source_id, config=Scenario(steps=4).model_dump(),
                                  input_manifest={"purpose": "integration fixture"}, total_steps=4))
    try:
        yield DatabaseFixture(engine, factory, source_id, region_id, run_id)
    finally:
        with factory.begin() as session:
            for model in (Trade, MarketOrder, RunMetric, Forecast):
                session.execute(delete(model).where(model.run_id == run_id))
            session.execute(delete(AuditLog).where(AuditLog.entity_id == run_id))
            session.execute(delete(SimulationRun).where(SimulationRun.id == run_id))
            session.execute(delete(Observation).where(Observation.source_id == source_id))
            session.execute(delete(DataSource).where(DataSource.id == source_id))
            session.execute(delete(Region).where(Region.id == region_id))
            if created_participants:
                session.execute(delete(Participant).where(Participant.id.in_(created_participants)))
        engine.dispose()


def proposed_orders():
    return [
        {"participant": "solar_coop", "side": "sell", "quantity_kwh": 10.0, "limit_price": 4.0},
        {"participant": "community", "side": "buy", "quantity_kwh": 6.0, "limit_price": 8.0},
        {"participant": "critical", "side": "buy", "quantity_kwh": 4.0, "limit_price": 9.0},
    ]


def count(factory, model):
    with factory() as session:
        return session.scalar(select(func.count()).select_from(model))


def count_run(factory, model, run_id):
    with factory() as session:
        return session.scalar(select(func.count()).select_from(model).where(model.run_id == run_id))


def test_foreign_key_and_observation_check_constraints(database):
    for source_id, value in ((str(uuid4()), 1), (database.source_id, -1)):
        with pytest.raises((IntegrityError, OperationalError)) as rejected:
            with database.factory.begin() as session:
                session.add(Observation(source_id=source_id, region_id=database.region_id,
                                        observed_at=datetime(2024, 1, 1), metric="demand", value=value, unit="MU"))
        if isinstance(rejected.value, OperationalError):
            assert database.engine.dialect.name == "mysql" and rejected.value.orig.args[0] == 3819
    with database.factory() as session:
        assert not list(session.scalars(select(Observation).where(Observation.source_id == database.source_id)))


def test_asset_constraints_are_enforced_in_database(database):
    for fields in ({"capacity_kw": -1}, {"capacity_kw": 10, "min_soc": 0.8, "max_soc": 0.2},
                   {"capacity_kw": 10, "capacity_kwh": 0}):
        with pytest.raises((IntegrityError, OperationalError)) as rejected:
            with database.factory.begin() as session:
                session.add(Asset(name=f"Invalid asset {uuid4()}", asset_type="battery", **fields))
        if isinstance(rejected.value, OperationalError):
            assert database.engine.dialect.name == "mysql" and rejected.value.orig.args[0] == 3819


def test_duplicate_observation_identity_is_rejected(database):
    values = dict(source_id=database.source_id, region_id=database.region_id,
                  observed_at=datetime(2024, 1, 1), metric="demand", value=123, unit="MU")
    with database.factory.begin() as session:
        session.add(Observation(**values))
    with pytest.raises(IntegrityError):
        with database.factory.begin() as session:
            session.add(Observation(**values))


def test_market_fault_rolls_back_orders_and_trades_then_retries_idempotently(database):
    def injected_failure():
        raise RuntimeError("Injected failure after matching, before commit")
    with pytest.raises(RuntimeError, match="Injected failure"):
        settle_step(database.factory, database.run_id, 0, proposed_orders(), available_kwh=10,
                    permitted_buy_kwh=10, before_commit=injected_failure)
    assert count_run(database.factory, MarketOrder, database.run_id) == 0
    assert count_run(database.factory, Trade, database.run_id) == 0
    for _ in range(2):
        settle_step(database.factory, database.run_id, 0, proposed_orders(), available_kwh=10, permitted_buy_kwh=10)
    assert count_run(database.factory, MarketOrder, database.run_id) == 3
    assert count_run(database.factory, Trade, database.run_id) == 2
    with database.factory() as session:
        orders = list(session.scalars(select(MarketOrder).where(MarketOrder.run_id == database.run_id)))
        assert all(order.remaining_kwh == 0 and order.status == "filled" for order in orders)
        joined = list(session.execute(select(Trade, Participant.key).join(MarketOrder, Trade.buy_order_id == MarketOrder.id)
                                     .join(Participant, MarketOrder.participant_id == Participant.id)
                                     .where(Trade.run_id == database.run_id)))
        assert {participant: (trade.quantity_kwh, trade.price) for trade, participant in joined} == {
            "critical": (4.0, 6.5), "community": (6.0, 6.0)}


def test_market_limits_prevent_unavailable_energy_and_unknown_participants(database):
    for available, buy_limit in ((9, 10), (10, 9)):
        with pytest.raises(ValueError, match="exceed"):
            settle_step(database.factory, database.run_id, 0, proposed_orders(),
                        available_kwh=available, permitted_buy_kwh=buy_limit)
    orders = proposed_orders()
    orders[1]["participant"] = "unknown_participant"
    with pytest.raises(ValueError, match="Unknown participant"):
        settle_step(database.factory, database.run_id, 0, orders, available_kwh=10, permitted_buy_kwh=10)
    assert count_run(database.factory, MarketOrder, database.run_id) == 0


def historical_inputs(database):
    return [{"id": str(uuid4()), "source_id": database.source_id,
             "observed_at": (datetime(2024, 1, 1) + timedelta(days=index)).isoformat() + "Z",
             "value": 200 + index, "unit": "MU"} for index in range(4)]


def test_run_success_persists_state_decisions_metrics_and_settled_trades(database, tmp_path):
    events = LocalEventStore(tmp_path / "events")
    service = RunService(database.factory, events)
    try:
        service.execute(database.run_id, historical_inputs(database))
    finally:
        service.shutdown()
    with database.factory() as session:
        run = session.get(SimulationRun, database.run_id)
        assert run.status == "completed"
        assert run.completed_steps == run.total_steps == 4
        assert run.finished_at is not None
        assert session.scalar(select(func.count()).select_from(RunMetric).where(RunMetric.run_id == run.id)) >= 10
        assert session.scalar(select(func.count()).select_from(Trade).where(Trade.run_id == run.id)) > 0
    assert len(events.query(database.run_id, event_type="STATE")) == 4
    assert len(events.query(database.run_id, step=0, event_type="AGENT_DECISION")) == 6
    assert len(events.query(database.run_id, event_type="SAFETY_CHECK")) == 4


def test_event_failure_keeps_partial_work_visible_and_run_failed(database, tmp_path):
    events = LocalEventStore(tmp_path / "events")
    class FailingStore:
        writes = 0
        def put(self, record):
            self.writes += 1
            if self.writes == 2:
                raise OSError("Injected event journal failure")
            events.put(record)
    service = RunService(database.factory, FailingStore())
    try:
        service.execute(database.run_id, historical_inputs(database))
    finally:
        service.shutdown()
    with database.factory() as session:
        run = session.get(SimulationRun, database.run_id)
        assert run.status == "failed"
        assert "event journal failure" in run.error
        assert run.completed_steps == 0
        assert run.finished_at is not None
    assert count_run(database.factory, RunMetric, database.run_id) == 0
    # SQL market settlement and event writes are intentionally not a distributed transaction.
    assert count_run(database.factory, Trade, database.run_id) > 0
    assert len(events.query(database.run_id)) == 1


def test_local_event_idempotence_conflict_queries_and_pagination(tmp_path):
    store = LocalEventStore(tmp_path / "events")
    run_id = str(uuid4())
    for step in range(3):
        for kind in ("STATE", "AGENT_DECISION", "SAFETY_CHECK"):
            record = event_record(run_id, step, kind, "asset", "2024-01-01T00:00:00Z", {"value": step + 0.25})
            store.put(record)
            store.put(record)
    all_records = store.query(run_id)
    assert len(all_records) == 9
    assert len(store.query(run_id, step=1)) == 3
    assert len(store.query(run_id, event_type="STATE")) == 3
    first = store.query(run_id, limit=4)
    following = store.query(run_id, after=first[-1]["SK"], limit=5)
    assert first + following == all_records
    conflict = {**all_records[0], "payload": {"value": 99}}
    with pytest.raises(ValueError, match="Idempotency conflict"):
        store.put(conflict)
    assert store.query(run_id) == all_records
    assert store.ready()
    with pytest.raises(ValueError):
        store.query("../../outside")


def test_dynamo_throttle_retry_and_conditional_idempotence_without_aws(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    from boto3.dynamodb.types import TypeSerializer
    from botocore.exceptions import ClientError
    from botocore.stub import ANY, Stubber
    import gridmind.events as module

    resource = boto3.resource("dynamodb", region_name="ap-south-1", aws_access_key_id="test",
                              aws_secret_access_key="test", endpoint_url="http://127.0.0.1:9")
    store = DynamoEventStore.__new__(DynamoEventStore)
    store.table = resource.Table("GridMindSimulationEvents")
    client = store.table.meta.client
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    run_id = str(uuid4())
    record = event_record(run_id, 0, "STATE", "asset", "2024-01-01T00:00:00Z", {"power": 12.25})
    params = {"TableName": "GridMindSimulationEvents", "Item": ANY,
              "ConditionExpression": "attribute_not_exists(PK) AND attribute_not_exists(SK)"}
    serializer = TypeSerializer()
    from decimal import Decimal
    normalized = json.loads(json.dumps(record), parse_float=Decimal)
    typed_item = {key: serializer.serialize(value) for key, value in normalized.items()}
    with Stubber(client) as stub:
        stub.add_client_error("put_item", service_error_code="ProvisionedThroughputExceededException", expected_params=params)
        stub.add_response("put_item", {}, expected_params=params)
        store.put(record)
        stub.add_client_error("put_item", service_error_code="ConditionalCheckFailedException", expected_params=params)
        stub.add_response("get_item", {"Item": typed_item}, expected_params={
            "TableName": "GridMindSimulationEvents", "Key": ANY, "ConsistentRead": True})
        store.put(record)
        stub.add_client_error("put_item", service_error_code="ConditionalCheckFailedException", expected_params=params)
        conflicting = {**normalized, "payload": {"power": Decimal("99.0")}}
        stub.add_response("get_item", {"Item": {key: serializer.serialize(value) for key, value in conflicting.items()}},
                          expected_params={"TableName": "GridMindSimulationEvents", "Key": ANY, "ConsistentRead": True})
        with pytest.raises(ValueError, match="Idempotency conflict"):
            store.put(record)
        for _ in range(4):
            stub.add_client_error("put_item", service_error_code="ProvisionedThroughputExceededException", expected_params=params)
        with pytest.raises(ClientError):
            store.put(record)
        stub.assert_no_pending_responses()

