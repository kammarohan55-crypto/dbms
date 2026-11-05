"""Durable HTTP controls, series selection and shutdown at real step boundaries."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import threading
import time

from fastapi.testclient import TestClient
import pytest

from gridmind.config import Settings
from gridmind.main import create_app
from gridmind.models import SimulationRun


def settings(tmp_path):
    return Settings(_env_file=None, database_url=f"sqlite:///{(tmp_path/'controls.db').as_posix()}",
                    event_backend='local', event_directory=str(tmp_path/'events'),
                    raw_directory=str(tmp_path/'raw'), model_directory=str(tmp_path/'models'), api_key='')


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(settings(tmp_path))) as test_client:
        rows = ['timestamp,value,unit,metric']
        rows.extend(f'{(datetime(2024,1,1)+timedelta(hours=i)).isoformat()}Z,{100+i},kWh,demand' for i in range(8))
        response = test_client.post('/api/ingestion/csv', files={'file': ('control.csv','\n'.join(rows).encode(),'text/csv')})
        assert response.status_code == 200, response.text
        yield test_client


def start(client, *, speed=0, steps=3, compare=False, **extra):
    response = client.post('/api/runs', json={'compare':compare,'scenario':{'steps':steps,'replay_speed':speed},**extra})
    assert response.status_code == 202, response.text
    return response.json()['runs']


def wait_for(client, run_id, predicate, timeout=5):
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result = client.get(f'/api/runs/{run_id}').json()
        if predicate(result):
            return result
        time.sleep(.01)
    pytest.fail(f'Run did not reach expected state: {result}')


def test_pause_acknowledges_full_step_and_resume_preserves_unique_history(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = client.app.state.store.put

    def blocked(record):
        if record['step'] == 0 and record['event_type'] == 'STATE':
            entered.set()
            assert release.wait(5)
        original(record)

    monkeypatch.setattr(client.app.state.store, 'put', blocked)
    run_id = start(client)[0]['id']
    assert entered.wait(2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.post, f'/api/runs/{run_id}/pause')
        try:
            # Pause cannot acknowledge midway through a market/event/progress step.
            time.sleep(.05)
            assert not pending.done()
        finally:
            release.set()
        paused_response = pending.result(timeout=3)
    assert paused_response.status_code == 200, paused_response.text
    paused = paused_response.json()
    assert paused['status'] == 'paused' and paused['completed_steps'] == 1
    assert client.post(f'/api/runs/{run_id}/pause').json()['status'] == 'paused'
    time.sleep(.25)
    assert client.get(f'/api/runs/{run_id}').json()['completed_steps'] == 1
    assert client.get(f'/api/runs/{run_id}/metrics').json() == {}
    assert client.post(f'/api/runs/{run_id}/resume').json()['status'] == 'running'
    finished = wait_for(client,run_id,lambda r:r['status']=='completed')
    assert finished['completed_steps'] == 3
    states = client.get(f'/api/runs/{run_id}/events',params={'event_type':'STATE'}).json()
    assert [event['step'] for event in states] == [0,1,2]
    assert len({event['SK'] for event in states}) == 3
    assert client.get(f'/api/runs/{run_id}/metrics').json()['energy_balance_max_abs_kw'] < 1e-7
    # Terminal controls are idempotent and never revive a completed run.
    for action in ['pause','resume','cancel']:
        assert client.post(f'/api/runs/{run_id}/{action}').json()['status'] == 'completed'


def test_cancel_interrupts_slow_replay_and_never_writes_success_metrics(client):
    run_id = start(client,speed=.01)[0]['id']
    wait_for(client,run_id,lambda r:r['completed_steps']==1)
    before = time.monotonic()
    cancelled = client.post(f'/api/runs/{run_id}/cancel').json()
    assert time.monotonic()-before < 2
    assert cancelled['status'] == 'cancelled' and cancelled['completed_steps'] == 1
    time.sleep(.05)
    assert client.get(f'/api/runs/{run_id}').json()['status'] == 'cancelled'
    assert len(client.get(f'/api/runs/{run_id}/events',params={'event_type':'STATE'}).json()) == 1
    assert client.get(f'/api/runs/{run_id}/metrics').json() == {}
    assert client.post(f'/api/runs/{run_id}/resume').json()['status'] == 'cancelled'


def test_cancel_requested_during_final_step_prevents_success_metrics(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = client.app.state.store.put

    def blocked(record):
        if record['event_type'] == 'STATE':
            entered.set()
            assert release.wait(5)
        original(record)

    monkeypatch.setattr(client.app.state.store,'put',blocked)
    run_id = start(client,steps=1)[0]['id']
    assert entered.wait(2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.post,f'/api/runs/{run_id}/cancel')
        try:
            time.sleep(.05)
            assert not pending.done()
        finally:
            release.set()
        cancelled = pending.result(timeout=3).json()
    assert cancelled['status'] == 'cancelled' and cancelled['completed_steps'] == 1
    assert client.get(f'/api/runs/{run_id}/metrics').json() == {}


def test_queued_pause_is_durable_and_counts_toward_bounded_admission(client):
    active = start(client,speed=.01,compare=True)
    queued = start(client,speed=.01,compare=True)
    run_id = queued[0]['id']
    paused = client.post(f'/api/runs/{run_id}/pause').json()
    assert paused['status'] == 'paused' and paused['completed_steps'] == 0
    refused = client.post('/api/runs',json={'compare':False,'scenario':{'steps':2}})
    assert refused.status_code == 422
    for run in [*active,queued[1]]:
        assert client.post(f"/api/runs/{run['id']}/cancel").json()['status'] == 'cancelled'
    time.sleep(.05)
    assert client.get(f'/api/runs/{run_id}').json()['completed_steps'] == 0
    assert client.post(f'/api/runs/{run_id}/resume').json()['status'] in {'queued','running'}
    wait_for(client,run_id,lambda r:r['completed_steps']==1)
    assert client.post(f'/api/runs/{run_id}/cancel').json()['status'] == 'cancelled'


def test_repeated_cancelled_queued_jobs_cannot_grow_executor_queue(client):
    occupied = start(client,speed=.01,compare=True)
    for run in occupied:
        wait_for(client,run['id'],lambda r:r['completed_steps']==1)
        assert client.post(f"/api/runs/{run['id']}/pause").json()['status'] == 'paused'
    queued = start(client,speed=.01,compare=True)
    for run in queued:
        assert client.post(f"/api/runs/{run['id']}/cancel").json()['status'] == 'cancelled'
    # Cancelled work is still queued behind paused workers until it is drained.
    # Admission also bounds scheduled futures, so rapid cancel/start cannot leak work.
    response = client.post('/api/runs',json={'compare':False,'scenario':{'steps':1}})
    assert response.status_code == 422
    assert len(client.app.state.runner._owned) == 4


def test_shutdown_interrupts_paused_and_slow_runs_and_restart_recovers_paused(tmp_path):
    config = settings(tmp_path)
    application = create_app(config)
    with TestClient(application) as test_client:
        payload = b'timestamp,value,unit,metric\n2024-01-01T00:00:00Z,100,kWh,demand\n2024-01-01T01:00:00Z,110,kWh,demand\n'
        assert test_client.post('/api/ingestion/csv',files={'file':('data.csv',payload,'text/csv')}).status_code == 200
        runs = start(test_client,speed=.01,steps=2,compare=True)
        wait_for(test_client,runs[0]['id'],lambda r:r['completed_steps']==1)
        assert test_client.post(f"/api/runs/{runs[0]['id']}/pause").json()['status'] == 'paused'
        before = time.monotonic()
    assert time.monotonic()-before < 2
    with application.state.factory.begin() as session:
        assert all(session.get(SimulationRun,run['id']).status == 'interrupted' for run in runs)
        # A process crash can leave a paused row: startup recovers it explicitly.
        session.get(SimulationRun,runs[0]['id']).status = 'paused'
    with TestClient(create_app(config)) as restarted:
        for run in runs:
            recovered = restarted.get(f"/api/runs/{run['id']}").json()
            assert recovered['status'] == 'interrupted' and recovered['error']
            assert restarted.get(f"/api/runs/{run['id']}/metrics").json() == {}


def test_series_metadata_and_explicit_input_selection(client):
    # A source containing two metrics requires an explicit series selection.
    payload = b'timestamp,value,unit,metric\n2024-02-01T00:00:00Z,1,kWh,solar\n2024-02-01T01:00:00Z,2,kWh,solar\n2024-02-01T00:00:00Z,100,kWh,demand\n2024-02-01T01:00:00Z,110,kWh,demand\n'
    response = client.post('/api/ingestion/csv',files={'file':('multiple.csv',payload,'text/csv')},data={'source_name':'Mixed targets'})
    assert response.status_code == 200,response.text
    source_id = response.json()['source_id']
    series = client.get('/api/series',params={'source_id':source_id}).json()
    assert {row['metric'] for row in series} == {'solar','demand'}
    assert all(row['count']==2 and row['start']<row['end'] and row['source_name']=='Mixed targets' for row in series)
    assert all(row['start_at']==row['start'] and row['end_at']==row['end'] for row in series)
    assert client.post('/api/runs',json={'source_id':source_id,'scenario':{'steps':2}}).status_code == 422
    chosen = next(row for row in series if row['metric']=='demand')
    selected = {key:chosen[key] for key in ['source_id','region_id','metric','series_key']}
    run_id = start(client,steps=2,**selected)[0]['id']
    result = wait_for(client,run_id,lambda r:r['status']=='completed')
    assert result['input_manifest']['series']['metric'] == 'demand'
    assert len(result['input_manifest']['observation_ids']) == 2


def test_all_load_assets_supply_defaults_and_explicit_scenario_overrides(client):
    for kind,capacity in [('household',100),('industrial',100),('critical',50),('ev',50)]:
        response = client.post('/api/assets',json={'name':kind,'asset_type':kind,'capacity_kw':capacity})
        assert response.status_code == 201,response.text
    run = start(client,steps=1)[0]
    assert run['config']['base_load_kw'] == 300
    assert run['config']['critical_fraction'] == pytest.approx(1/6)
    assert run['config']['flexible_fraction'] == pytest.approx(1/6)
    override = client.post('/api/runs',json={'compare':False,'scenario':{'steps':1,'base_load_kw':500,'critical_fraction':.4,'flexible_fraction':.2}})
    assert override.status_code == 202,override.text
    custom = override.json()['runs'][0]['config']
    assert custom['base_load_kw'] == 500 and custom['critical_fraction'] == .4 and custom['flexible_fraction'] == .2
