"""Exports preserve complete histories and the selected run's input provenance."""
import csv
import io
import json
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker
from gridmind.database import initialize, make_engine
from gridmind.events import event_record
from gridmind.exports import export_run, export_comparison
from gridmind.models import DataSource, Region, Observation, SimulationRun, RunMetric, MetricDefinition, ModelRecord


@pytest.fixture
def export_data(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'export.db').as_posix()}")
    initialize(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    source_id, region_id, run_id, comparison_id, model_id = (str(uuid4()) for _ in range(5))
    ids = [str(uuid4()) for _ in range(3)]
    with factory.begin() as session:
        session.add(DataSource(id=source_id, name='Source', provider='Public data', units='MU',
                               url='https://name:password@example.invalid', metadata_json={'raw_json': 'C:/private/raw.json'}))
        session.add(Region(id=region_id, name='Karnataka'))
        session.flush()
        session.add(ModelRecord(id=model_id, name='Registered model', version=str(uuid4()), target='demand',
                                source_id=source_id, region_id=region_id, algorithm='SeasonalPersistence',
                                metrics={'mae': 1.5}, metadata_json={'token': 'secret'}, artifact_path='C:/private/model.joblib'))
        session.flush()
        for index, observation_id in enumerate(ids):
            session.add(Observation(id=observation_id, source_id=source_id, region_id=region_id,
                                    observed_at=datetime(2024, 1, 1) + timedelta(days=index),
                                    metric='demand', value=100 + index, unit='MU', metadata_json={'raw_path': 'private'}))
        session.add(SimulationRun(id=run_id, comparison_id=comparison_id, name='normal', policy='gridmind',
                                  source_id=source_id, model_id=model_id, total_steps=336,
                                  config={'seed': 42, 'api_key': 'secret'}, input_manifest={
                                      'observation_ids': ids[:2], 'sha256': 'traceable-input-hash', 'secret': 'private'}))
        session.flush()
        session.add(MetricDefinition(name='energy_kwh', unit='kWh', description='Export test metric'))
        session.flush()
        session.add(RunMetric(run_id=run_id, name='energy_kwh', value=12.5))

    class MemoryStore:
        def __init__(self):
            self.records = []
            self.cursors = []
        def query(self, identity, *, limit, after):
            assert identity == run_id
            self.cursors.append(after)
            # Short pages must not terminate an export before all events arrive.
            return [item for item in self.records if not after or item['SK'] > after][:min(limit, 173)]

    store = MemoryStore()
    for step in range(336):
        for kind, suffix in [('STATE', 'state'), ('SAFETY_CHECK', 'validation'), *[('AGENT_DECISION', str(i)) for i in range(6)]]:
            store.records.append(event_record(run_id, step, kind, suffix, '2024-01-01T00:00:00Z', {
                'input_observation_id': ids[step % 2], 'value_kind': 'modelled',
                'demand_kw': step + 0.5, 'token': 'secret', 'artifact_path': 'C:/private',
            }))
    store.records.sort(key=lambda item: item['SK'])
    yield factory, store, run_id, comparison_id, ids
    engine.dispose()


def test_json_includes_only_manifest_observations_and_every_event(export_data):
    factory, store, run_id, _, ids = export_data
    content, filename, media = export_run(factory, store, run_id)
    data = json.loads(content)
    assert filename == f'gridmind-run-{run_id}.json'
    assert media == 'application/json' and data['schema_version'] == 1
    assert [row['id'] for row in data['observations']] == ids[:2]
    assert ids[2] not in content.decode()
    assert data['observations'][0]['value_kind'] == 'observed'
    assert len(data['states']) == 336
    assert len(data['decisions']) == 336 * 6
    assert len(data['safety_checks']) == 336
    assert len(store.cursors) > 2 and store.cursors[0] is None
    assert data['sources'][0]['name'] == 'Source' and data['regions'][0]['name'] == 'Karnataka'
    assert data['metrics'][0]['value'] == 12.5 and data['run']['config'] == {'seed': 42}
    for private in ('secret', 'password', 'private', 'artifact_path', 'raw_json', 'raw_path', 'token', 'api_key'):
        assert private not in content.decode()


def test_csv_is_flat_state_report_and_text_is_spreadsheet_safe(export_data):
    factory, store, run_id, _, _ = export_data
    next(item for item in store.records if item['event_type'] == 'STATE')['payload']['value_kind'] = ' =HYPERLINK("bad")'
    content, filename, media = export_run(factory, store, run_id, 'csv')
    rows = list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
    assert len(rows) == 336 and rows[-1]['step'] == '335'
    assert rows[0]['value_kind'].startswith("' =")
    assert rows[0]['demand_kw'] == '0.5'
    assert 'input_observation_id' in rows[0] and 'payload' not in rows[0]
    assert filename.endswith('-states.csv') and media == 'text/csv'


def test_export_comparison_reports_saved_metrics_and_incomplete_runs(export_data):
    factory, _, run_id, comparison_id, _ = export_data
    with factory.begin() as session:
        original = session.get(SimulationRun, run_id)
        session.add(SimulationRun(id=str(uuid4()), comparison_id=comparison_id, name='normal', policy='baseline',
                                  source_id=original.source_id, total_steps=336, config={}, input_manifest={}, status='running'))
        session.add(MetricDefinition(name='=unsafe', unit='@unsafe', description='Text escaping test'))
        session.flush()
        session.add(RunMetric(run_id=run_id, name='=unsafe', value=-4))
    content, filename, media = export_comparison(factory, comparison_id)
    rows = list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))))
    assert len(rows) == 3 and any(row['status'] == 'running' and row['metric'] == '' for row in rows)
    assert any(row['metric'] == "'=unsafe" and row['unit'] == "'@unsafe" and row['value'] == '-4.0' for row in rows)
    assert any(row['metric'] == 'energy_kwh' and row['value'] == '12.5' for row in rows)
    assert filename == f'gridmind-comparison-{comparison_id}.csv' and media == 'text/csv'


def test_export_rejects_unknown_entities_formats_and_surfaces_missing_inputs(export_data):
    factory, store, run_id, _, ids = export_data
    with pytest.raises(LookupError, match='Run not found'):
        export_run(factory, store, str(uuid4()))
    with pytest.raises(LookupError, match='Comparison not found'):
        export_comparison(factory, str(uuid4()))
    with pytest.raises(ValueError, match='format'):
        export_run(factory, store, run_id, 'xlsx')
    missing_id = str(uuid4())
    with factory.begin() as session:
        session.get(SimulationRun, run_id).input_manifest = {'observation_ids': [*ids[:2], missing_id]}
    data = json.loads(export_run(factory, store, run_id)[0])
    assert data['missing_observation_ids'] == [missing_id]
