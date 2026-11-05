"""Reject ambiguous evidence and keep bounded live imports traceable."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from gridmind.config import Settings
from gridmind.database import initialize, make_engine
from gridmind.ingestion import ingest_csv
from gridmind.models import DataSource, Observation
from gridmind.public_data import parse_csv
import gridmind.live_context as live


@pytest.fixture
def storage(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'ingestion.db').as_posix()}")
    initialize(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    config = Settings(_env_file=None, live_context_url='https://user:private@example.invalid/energy?token=private',
                      live_context_name='Measured feed', raw_directory=str(tmp_path / 'raw'))
    yield factory, config
    engine.dispose()


def feed(monkeypatch, payload, *, status=200):
    content = json.dumps(payload).encode()
    @contextmanager
    def response(*args, **kwargs):
        yield httpx.Response(status, content=content, request=httpx.Request('GET','https://example.invalid/energy'))
    monkeypatch.setattr(live.httpx,'stream',response)
    return content


def row(value=10, **extra):
    return {'timestamp':'2024-01-01T00:00:00Z','value':value,'unit':'kWh','metric':'demand',**extra}


@pytest.mark.parametrize('values',[(1,2),(2,1)])
def test_conflicting_rows_abort_csv_before_any_sql_change(storage, values):
    factory, config = storage
    content = ('timestamp,value,unit,metric\n'
               f'2024-01-01,{values[0]},kWh,demand\n2024-01-01,{values[1]},kWh,demand\n').encode()
    with pytest.raises(ValueError,match='Conflicting duplicate'):
        ingest_csv(factory,content,config.path(config.raw_directory),'Conflicting feed','Test','UTC')
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Observation)) == 0
        assert session.scalar(select(func.count()).select_from(DataSource)) == 0


def test_canonical_meter_series_keep_independent_identities_and_quality_labels():
    content = ('timestamp,value,unit,metric,region,series_key,value_kind\n'
               '2024-01-01,1,kWh,demand,Test,A,observed\n'
               '2024-01-01,2,kWh,demand,Test,B,observed\n'
               '2024-01-02,3,kWh,demand,Test,A,predicted\n'
               '2024-01-03,4,kWh,demand,Test,A,simulated\n')
    rows, quality = parse_csv(content)
    assert len(rows) == 2 and len({item['id'] for item in rows}) == 2
    assert {item['series_key'] for item in rows} == {'A','B'}
    assert quality['rejected'] == 2
    assert all('Only observed measurements' in error['reason'] for error in quality['errors'])


def test_live_series_provenance_is_committed_with_observations_and_credentials_removed(storage, monkeypatch):
    factory, config = storage
    content = feed(monkeypatch,{'observations':[row(series_key='A'),row(20,household_id='B')]})
    result = live.refresh(factory,config)
    assert result['inserted'] == 2
    assert result['mode'] == 'observed live-context data'
    with factory() as session:
        source = session.get(DataSource,result['source_id'])
        assert source.source_type == 'live_api' and source.provider == config.live_context_name
        assert source.url == 'https://example.invalid/energy'
        observations = list(session.scalars(select(Observation)))
        assert {item.series_key for item in observations} == {'A','B'}
        for item in observations:
            provenance = item.metadata_json
            assert provenance['raw_json_sha256'] == hashlib.sha256(content).hexdigest()
            assert Path(provenance['raw_json']).read_bytes() == content
            assert provenance['source_url'] == source.url
            assert provenance['acquired_at']
        assert 'private' not in json.dumps(source.metadata_json)
    assert live.refresh(factory,config)['duplicates'] == 2


def test_over_limit_live_record_count_never_imports_a_silent_subset(storage, monkeypatch):
    factory, config = storage
    monkeypatch.setattr(live,'MAX_FEED_ROWS',1)
    feed(monkeypatch,[row(),row(20,timestamp='2024-01-02')])
    with pytest.raises(ValueError,match='no measurements were invented'):
        live.refresh(factory,config)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Observation)) == 0


def test_original_live_evidence_failure_never_commits_measurements(storage, monkeypatch):
    factory, config = storage
    feed(monkeypatch,[row()])
    original_write = Path.write_bytes
    def fail_raw(path, content):
        if path.name.startswith('live-'):
            raise OSError('Injected full disk')
        return original_write(path,content)
    monkeypatch.setattr(Path,'write_bytes',fail_raw)
    with pytest.raises(ValueError,match='OSError'):
        live.refresh(factory,config)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Observation)) == 0


@pytest.mark.parametrize('status',[401,429,503])
def test_provider_auth_rate_limit_and_outage_never_create_observations(storage, monkeypatch, status):
    factory, config = storage
    feed(monkeypatch,[row()],status=status)
    with pytest.raises(ValueError,match='HTTPStatusError'):
        live.refresh(factory,config)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(Observation)) == 0


@pytest.mark.parametrize('oversized_header',[False,True])
def test_download_stops_at_bound_without_reading_entire_response(monkeypatch, oversized_header):
    consumed = []
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(3):
                consumed.append(index)
                yield b'aaaa'
    @contextmanager
    def response(*args, **kwargs):
        yield httpx.Response(200,stream=Chunks(),headers={'Content-Length':'12'} if oversized_header else {},
                             request=httpx.Request('GET','https://example.invalid'))
    monkeypatch.setattr(live.httpx,'stream',response)
    monkeypatch.setattr(live,'MAX_FEED_BYTES',5)
    with pytest.raises(ValueError,match='bounded import size'):
        live._download('https://example.invalid',{})
    assert consumed == ([] if oversized_header else [0,1])


def test_download_total_deadline_rejects_slow_trickle(monkeypatch):
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b'a'
            yield b'b'
    @contextmanager
    def response(*args, **kwargs):
        yield httpx.Response(200,stream=Chunks(),request=httpx.Request('GET','https://example.invalid'))
    times = iter([0,1,21])
    monkeypatch.setattr(live.httpx,'stream',response)
    monkeypatch.setattr(live.time,'monotonic',lambda:next(times))
    with pytest.raises(ValueError,match='total download time limit'):
        live._download('https://example.invalid',{})
