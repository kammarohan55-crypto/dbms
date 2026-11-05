"""Forecast registry identity, repeat requests, and leakage-safe replay."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from gridmind.database import initialize, make_engine
from gridmind.forecast_service import observation_series, predict, replay_forecasts, train
from gridmind.ingestion import import_records, parse_time
from gridmind.models import Forecast, ModelRecord, Observation


def readings(count=220, series_key='meter-A'):
    start = datetime(2022,1,1,tzinfo=timezone.utc)
    return [{'observed_at':(start + timedelta(days=index)).isoformat(), 'value':float(50 + index % 7),
             'region':'Forecast fixture','metric':'load_kwh','unit':'kWh','series_key':series_key,'quality':'observed'}
            for index in range(count)]


@pytest.fixture
def store(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'forecast.db').as_posix()}")
    initialize(engine)
    factory = sessionmaker(engine,expire_on_commit=False)
    source = import_records(factory,readings(),{'provider':'Explicit test fixture'},'Fixture source','Forecast fixture')
    yield factory, source['source_id'], tmp_path / 'models'
    engine.dispose()


def test_explicit_series_selection_survives_registry_and_native_artifact(store):
    factory, source_id, directory = store
    import_records(factory,readings(series_key='meter-B'),{'provider':'Explicit test fixture'},'Fixture source','Forecast fixture')
    with factory() as session:
        with pytest.raises(ValueError,match='Multiple observed series'):
            observation_series(session,source_id)
        selected = observation_series(session,source_id,metric='load_kwh',series_key='meter-B')
    model = train(factory,directory,source_id=source_id,region_id=selected[0]['region_id'],metric='load_kwh',series_key='meter-B')
    assert model['metadata_json']['series_key'] == 'meter-B'
    native = json.loads(Path(model['artifact_path']).read_text())
    assert native == model['metadata_json']
    assert native['source_id'] == source_id
    assert native['region_id'] == selected[0]['region_id']
    assert len(predict(factory,model['id'],2)) == 2
    with pytest.raises(ValueError,match='No suitable'):
        train(factory,directory,source_id=source_id,series_key='missing-meter')


def test_forecast_requests_reuse_ids_and_extend_horizon_without_duplicates(store):
    factory, source_id, directory = store
    model = train(factory,directory,source_id=source_id)
    first = predict(factory,model['id'],3)
    assert predict(factory,model['id'],3) == first
    extended = predict(factory,model['id'],5)
    assert extended[:3] == first
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Forecast)) == 5
        history = observation_series(session,source_id)
    newest = history[-1]
    with factory.begin() as session:
        session.add(Observation(source_id=source_id,region_id=newest['region_id'],series_key=newest['series_key'],
            metric=newest['metric'],unit=newest['unit'],value=54,
            observed_at=parse_time(newest['observed_at']) + timedelta(days=1),quality_status='observed'))
    updated = predict(factory,model['id'],2)
    assert updated[0]['target_at'] == first[1]['target_at']
    assert updated[0]['id'] != first[1]['id']
    assert updated[0]['metadata_json']['input_sha256'] != first[1]['metadata_json']['input_sha256']
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Forecast)) == 7


def test_concurrent_identical_forecast_requests_store_one_set(store):
    factory, source_id, directory = store
    model = train(factory,directory,source_id=source_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _:predict(factory,model['id'],3),range(2)))
    assert results[0] == results[1]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Forecast)) == 3


def test_concurrent_different_horizons_share_overlapping_rows(store):
    factory, source_id, directory = store
    model = train(factory,directory,source_id=source_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda horizon:predict(factory,model['id'],horizon),[3,5]))
    assert results[0] == results[1][:3]
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Forecast)) == 5


def test_replay_respects_requested_steps_and_calibrated_cutoff(store):
    factory, source_id, directory = store
    model = train(factory,directory,source_id=source_id)
    with factory() as session:
        history = observation_series(session,source_id)
        replay, model_id, values, forecasts = replay_forecasts(session,history,model['id'],steps=3)
        assert model_id == model['id']
        assert len(replay) == len(values) == len(forecasts) == 3
        assert all(parse_time(row['observed_at']) > parse_time(model['metadata_json']['validation_end']) for row in replay)
        assert [row['actual'] for row in forecasts] == [row['value'] for row in replay]
        assert [row['value'] for row in forecasts] == values
        assert len(replay_forecasts(session,history,model['id'],steps=1)[0]) == 1
        changed = [{**row,'value':row['value'] + 1} for row in history]
        with pytest.raises(ValueError,match='changed'):
            replay_forecasts(session,changed,model['id'],steps=3)
        wrong = [{**row,'series_key':'wrong-meter'} for row in history]
        with pytest.raises(ValueError,match='does not match'):
            replay_forecasts(session,wrong,model['id'],steps=3)


def test_replay_can_use_336_real_heldout_labels(store):
    factory, source_id, directory = store
    import_records(factory,readings(count=2400),{'provider':'Explicit test fixture'},'Fixture source','Forecast fixture')
    model = train(factory,directory,source_id=source_id)
    with factory() as session:
        history = observation_series(session,source_id)
        replay, _, values, forecasts = replay_forecasts(session,history,model['id'])
        assert len(replay) == len(values) == len(forecasts) == 336
        assert replay[0]['observed_at'] == model['metadata_json']['test_predictions'][0]['target_at'].replace('+00:00','Z')


def test_legacy_replay_allows_bounded_float32_precision_restoration(store):
    factory, source_id, directory = store
    model = train(factory,directory,source_id=source_id)
    with factory.begin() as session:
        record = session.get(ModelRecord,model['id'])
        metadata = json.loads(json.dumps(record.metadata_json))
        metadata['artifact_schema'] = 1
        for prediction in metadata['test_predictions']:
            prediction['actual'] += prediction['actual'] * 4e-8
        record.metadata_json = metadata
    with factory() as session:
        history = observation_series(session,source_id)
        assert len(replay_forecasts(session,history,model['id'],steps=3)[0]) == 3
    with factory.begin() as session:
        record = session.get(ModelRecord,model['id'])
        metadata = json.loads(json.dumps(record.metadata_json))
        metadata['artifact_schema'] = 2
        record.metadata_json = metadata
    with factory() as session:
        with pytest.raises(ValueError,match='changed'):
            replay_forecasts(session,history,model['id'],steps=3)


def test_replay_without_model_is_bounded_and_does_not_fabricate_forecasts(store):
    factory, source_id, _ = store
    with factory() as session:
        history = observation_series(session,source_id)
        replay, model_id, values, forecasts = replay_forecasts(session,history,steps=7)
        assert replay == history[-7:]
        assert model_id is None and values is None and forecasts == []
        with pytest.raises(ValueError,match='steps'):
            replay_forecasts(session,history,steps=337)
        with pytest.raises(ValueError,match='actual observations'):
            replay_forecasts(session,[])
    with pytest.raises(ValueError,match='Model not found'):
        predict(factory,'missing',2)
