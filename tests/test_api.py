"""Local HTTP workflow checks with isolated SQLite and event-journal storage."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import time
from uuid import uuid4

import pytest
import httpx
from fastapi.testclient import TestClient

from gridmind.config import Settings
from gridmind.main import create_app


API_KEY = "test-only-gridmind-key"


@pytest.fixture
def client(tmp_path):
    config = Settings(_env_file=None, database_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}",
                      event_backend="local", event_directory=str(tmp_path / "events"),
                      model_directory=str(tmp_path / "models"), raw_directory=str(tmp_path / "raw"),
                      api_key=API_KEY)
    application = create_app(config)
    with TestClient(application, headers={"X-API-Key": API_KEY}) as test_client:
        yield test_client


def csv_content(count=8):
    start = datetime(2024, 1, 1)
    rows = ["timestamp,value,unit,metric"]
    rows.extend(f"{(start + timedelta(days=index)).isoformat()}Z,{100 + index},GWh,supplied_energy_gwh"
                for index in range(count))
    return ("\n".join(rows) + "\n").encode()


def upload(client, content=None, source_name="Integration observations"):
    return client.post("/api/ingestion/csv", files={"file": ("observations.csv", content or csv_content(), "text/csv")},
                       data={"source_name": source_name, "region_name": "Fixture region", "timezone": "UTC"})


def await_runs(client, runs):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        states = [client.get(f"/api/runs/{run['id']}").json() for run in runs]
        if all(run["status"] not in {"queued", "running"} for run in states):
            return states
        time.sleep(0.02)
    pytest.fail(f"Simulation did not finish within 10 seconds: {states}")


def test_health_authentication_and_unavailable_database_are_explicit(client):
    public = client.get("/api/health", headers={"X-API-Key": ""})
    assert public.status_code == 200
    body = public.json()
    assert body["status"] == "ready"
    assert body["relational"] == {"backend": "sqlite", "ready": True}
    assert body["aws_deferred"] is True
    assert body["events"]["ready"] is True
    assert client.get("/api/assets", headers={"X-API-Key": "wrong"}).status_code == 401
    client.app.state.ready = False
    try:
        assert client.get("/api/assets").status_code == 503
        assert client.get("/api/health").json()["status"] == "degraded"
    finally:
        client.app.state.ready = True


def test_assets_crud_validation_conflicts_and_audit(client):
    asset = {"name": "Test battery", "asset_type": "battery", "capacity_kw": 60,
             "capacity_kwh": 240, "min_soc": 0.15, "max_soc": 0.95}
    created = client.post("/api/assets", json=asset)
    assert created.status_code == 201
    asset_id = created.json()["id"]
    assert client.get("/api/assets").json()[0]["id"] == asset_id
    assert client.post("/api/assets", json=asset).status_code == 409
    assert client.post("/api/assets", json={**asset, "name": "Bad battery", "capacity_kwh": 0}).status_code == 422
    assert client.post("/api/assets", json={**asset, "min_soc": 0.98, "max_soc": 0.9}).status_code == 422
    updated = client.put(f"/api/assets/{asset_id}", json={**asset, "capacity_kw": 45})
    assert updated.status_code == 200
    assert updated.json()["capacity_kw"] == 45
    assert client.delete(f"/api/assets/{asset_id}").status_code == 200
    assert client.get("/api/assets").json() == []
    assert client.delete(f"/api/assets/{asset_id}").status_code == 404
    actions = [entry["action"] for entry in client.get("/api/audit").json()]
    assert set(actions) == {"asset_create", "asset_update", "asset_delete"}


def test_csv_ingestion_is_idempotent_and_observations_keep_provenance(client):
    first = upload(client)
    assert first.status_code == 200, first.text
    assert first.json()["inserted"] == 8
    second = upload(client)
    assert second.status_code == 200
    assert second.json()["inserted"] == 0
    assert second.json()["duplicates"] == 8
    source_id = first.json()["source_id"]
    sources = client.get("/api/sources").json()
    assert sources[0]["observation_count"] == 8
    observations = client.get("/api/observations", params={"source_id": source_id, "limit": 2}).json()
    assert [row["value"] for row in observations] == [106, 107]
    assert all(row["value_kind"] == "observed" and row["source_name"] == "Integration observations" for row in observations)
    assert all(row["unit"] == "GWh" and row["observed_at"].endswith("Z") for row in observations)
    assert client.get("/api/observations?limit=0").status_code == 422
    assert client.get("/api/observations?offset=-1").status_code == 422


def test_london_households_remain_separate_series_and_regions(client):
    content = b"LCLid,stdorToU,DateTime,KWH/hh\nMAC00001,Std,2013-01-01 00:00:00,0.2\nMAC00002,Std,2013-01-01 00:00:00,0.4\n"
    response = upload(client, content, source_name="London subset")
    assert response.status_code == 200, response.text
    assert response.json()["inserted"] == 2
    rows = client.get("/api/observations").json()
    assert {row["series_key"] for row in rows} == {"MAC00001", "MAC00002"}
    assert {row["region_name"] for row in rows} == {"London/MAC00001", "London/MAC00002"}
    assert {row["value"] for row in rows} == {0.2, 0.4}
    assert all(row["metric"] == "household_energy_kwh" and row["unit"] == "kWh" for row in rows)
    repeated = upload(client, content, source_name="London subset")
    assert repeated.json()["duplicates"] == 2


def test_changed_duplicate_is_rejected_without_rewriting_observation_provenance(client):
    first = upload(client)
    assert first.status_code == 200
    source_before = client.get("/api/sources").json()[0]
    changed = csv_content().replace(b",100,GWh,", b",999,GWh,", 1)
    response = upload(client, changed)
    assert response.status_code == 422, response.text
    source_after = client.get("/api/sources").json()[0]
    assert source_after["metadata_json"] == source_before["metadata_json"]
    assert client.get("/api/observations").json()[0]["value"] == 100


def test_invalid_timezone_returns_validation_error(client):
    content = b"timestamp,value,unit,metric\n2024-01-01T00:00:00,1,kWh,demand\n"
    response = client.post("/api/ingestion/csv", files={"file": ("input.csv", content, "text/csv")},
                           data={"timezone": "Invalid/Timezone"})
    assert response.status_code == 422


@pytest.mark.parametrize("metric,region", [("m" * 61, "Custom"), ("demand", "r" * 101)])
def test_csv_field_widths_match_relational_schema(client, metric, region):
    content = f"timestamp,value,unit,metric,region\n2024-01-01T00:00:00Z,1,kWh,{metric},{region}\n".encode()
    response = upload(client, content)
    assert response.status_code == 422
    assert client.get("/api/observations").json() == []


def test_bad_input_and_unconfigured_live_context_are_rejected(client):
    bad = b"timestamp,value,unit,metric\n2024-01-01T00:00:00Z,-1,GWh,demand\n"
    assert upload(client, bad).status_code == 422
    assert client.get("/api/observations").json() == []
    assert client.post("/api/runs", json={"mode": "live-context"}).status_code == 422
    assert client.post("/api/runs", json={"scenario": {"steps": 0}}).status_code == 422
    assert client.post("/api/runs", json={"scenario": {"steps": 2}}).status_code == 422
    assert client.post("/api/forecasts/train", json={"target": "solar"}).status_code == 422
    assert client.get("/api/runs/not-a-uuid").status_code == 422
    assert client.get(f"/api/runs/{uuid4()}").status_code == 404
    imported = upload(client).json()
    assert client.post("/api/runs", json={"source_id": imported["source_id"], "model_id": str(uuid4()),
                                          "scenario": {"steps": 2}}).status_code == 422


def test_paired_simulation_http_workflow_has_real_events_trades_and_metrics(client):
    imported = upload(client).json()
    started = client.post("/api/runs", json={"source_id": imported["source_id"], "compare": True,
                                            "scenario": {"steps": 4, "seed": 7}})
    assert started.status_code == 202, started.text
    result = started.json()
    runs = await_runs(client, result["runs"])
    assert len(runs) == 2
    assert {run["policy"] for run in runs} == {"baseline", "gridmind"}
    assert all(run["status"] == "completed" and run["completed_steps"] == 4 for run in runs), runs
    histories = {}
    for run in runs:
        run_id = run["id"]
        states = client.get(f"/api/runs/{run_id}/events", params={"event_type": "STATE"}).json()
        assert len(states) == 4
        assert all(event["payload"]["value_kind"] == "modelled" for event in states)
        assert all(event["payload"]["input_observation_id"] for event in states)
        histories[run["policy"]] = states
        metrics = client.get(f"/api/runs/{run_id}/metrics").json()
        assert metrics["energy_balance_max_abs_kw"] < 1e-7
        assert metrics["safety_rejections"] == 0
        market = client.get(f"/api/runs/{run_id}/market").json()
        if run["policy"] == "gridmind":
            assert market["orders"] and market["trades"]
            assert metrics["p2p_volume_kwh"] == pytest.approx(sum(trade["quantity_kwh"] for trade in market["trades"]))
            decisions = client.get(f"/api/runs/{run_id}/events", params={"event_type": "AGENT_DECISION", "step": 0}).json()
            assert len(decisions) == 6
        else:
            assert market == {"orders": [], "trades": []}
        all_events = client.get(f"/api/runs/{run_id}/events").json()
        first = client.get(f"/api/runs/{run_id}/events", params={"limit": 3}).json()
        remaining = client.get(f"/api/runs/{run_id}/events", params={"after": first[-1]["SK"]}).json()
        assert first + remaining == all_events
    for baseline, coordinated in zip(histories["baseline"], histories["gridmind"]):
        for key in ("demand_kw", "solar_kw", "wind_kw", "tariff"):
            assert baseline["payload"][key] == coordinated["payload"][key]
        assert baseline["timestamp"] == coordinated["timestamp"]
    comparison = client.get(f"/api/comparisons/{result['comparison_id']}").json()
    assert len(comparison["runs"]) == 2
    assert all(run["metrics"] for run in comparison["runs"])
    demo = client.get("/api/db/demo")
    assert demo.status_code == 200, demo.text
    assert demo.json()["joins"][0]["observations"] == 8
    assert demo.json()["benchmark"]["available"] is True
    assert demo.json()["benchmark"]["plans"]["indexed"]
    assert len(demo.json()["comparison"]) >= 10


def test_event_write_failure_is_reported_as_failed_run_through_api(client, monkeypatch):
    imported = upload(client).json()
    def fail(record):
        raise OSError("Injected event-store outage")
    monkeypatch.setattr(client.app.state.store, "put", fail)
    response = client.post("/api/runs", json={"source_id": imported["source_id"], "compare": False,
                                             "scenario": {"steps": 2}})
    assert response.status_code == 202
    run = await_runs(client, response.json()["runs"])[0]
    assert run["status"] == "failed"
    assert "event-store outage" in run["error"]
    assert run["completed_steps"] == 0
    assert client.get(f"/api/runs/{run['id']}/metrics").json() == {}


def test_configured_live_adapter_imports_observations_and_retains_them_on_failure(tmp_path, monkeypatch):
    import gridmind.live_context as live_module
    url = "https://example.invalid/measured-energy"
    config = Settings(_env_file=None, database_url=f"sqlite:///{(tmp_path / 'live.db').as_posix()}",
                      event_backend="local", event_directory=str(tmp_path / "events"),
                      raw_directory=str(tmp_path / "raw"), live_context_url=url,
                      live_context_name="Fixture measured feed", api_key="")
    payload = {"observations": [
        {"observed_at": "2024-01-01T00:00:00Z", "value": 100, "unit": "kWh", "metric": "demand"},
        {"observed_at": "2024-01-01T00:30:00Z", "value": 110, "unit": "kWh", "metric": "demand"},
    ]}
    @contextmanager
    def stream_payload(*args, **kwargs):
        yield httpx.Response(200, json=payload, request=httpx.Request("GET", url))
    monkeypatch.setattr(live_module.httpx, "stream", stream_payload)
    with TestClient(create_app(config)) as client:
        response = client.post("/api/live-context/refresh")
        assert response.status_code == 200, response.text
        assert response.json()["inserted"] == 2
        assert client.get("/api/sources").json()[0]["source_type"] == "live_api"
        prior = client.get("/api/observations").json()
        assert [row["value"] for row in prior] == [100, 110]
        assert all(row['metadata_json']['source_url'] == url for row in prior)
        assert all(row['metadata_json']['raw_json_sha256'] for row in prior)
        assert response.json()['mode'] == 'observed live-context data'
        def unavailable(*args, **kwargs):
            raise httpx.ConnectError("Injected source outage")
        monkeypatch.setattr(live_module.httpx, "stream", unavailable)
        failed = client.post("/api/live-context/refresh")
        assert failed.status_code == 422
        assert "no measurements were invented" in failed.json()["detail"]
        assert client.get("/api/observations").json() == prior
        assert client.get("/api/sources").json()[0]["status"].startswith("unavailable")
        payload = {"observations": [42]}
        monkeypatch.setattr(live_module.httpx, "stream", stream_payload)
        assert client.post("/api/live-context/refresh").status_code == 422

