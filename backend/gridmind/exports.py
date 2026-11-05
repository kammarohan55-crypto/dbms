"""Portable, provenance-linked exports. Never include deployment settings or artifacts."""
import csv
import io
import json
from sqlalchemy import select
from .database import asdict
from .models import (DataSource, Region, Observation, SimulationRun, ModelRecord,
                     MarketOrder, Trade, RunMetric, MetricDefinition, Participant)

SCHEMA_VERSION = 1
STATE_COLUMNS = (
    'demand_kw', 'solar_kw', 'wind_kw', 'generation_kw', 'battery_kwh',
    'battery_soc', 'charge_kw', 'discharge_kw',
    'grid_import_kw', 'grid_export_kw', 'curtailed_kw', 'unmet_kw',
    'critical_demand_kw', 'critical_served_kw', 'renewable_spill_kw', 'risk',
    'forecast_demand_kw', 'tariff', 'export_tariff', 'grid_import_limit_kw',
    'energy_balance_error_kw', 'input_value', 'regional_series_mean',
    'regional_scale_kw_per_input_unit', 'simulation_hour', 'load_profile_factor',
    'demand_scenario_factor', 'generation_scenario_factor',
)
RUN_FIELDS = ('id', 'comparison_id', 'name', 'policy', 'mode', 'source_id',
              'model_id', 'status', 'config', 'total_steps', 'completed_steps',
              'created_at', 'started_at', 'finished_at')
MANIFEST_FIELDS = ('observation_ids', 'sha256', 'provenance', 'forecast_mode',
                   'source_id', 'region_id', 'metric', 'unit', 'series_key',
                   'selected_series', 'series', 'normalization_mean',
                   'forecast_model_version', 'forecast_model_id')


def _csv_cell(value):
    """Keep spreadsheet software from interpreting user text as a formula."""
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def _csv_bytes(columns, rows):
    output = io.StringIO(newline='')
    writer = csv.writer(output, lineterminator='\r\n')
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(column, '')) for column in columns])
    return output.getvalue().encode('utf-8-sig')


def _public(value):
    """Defense in depth for nested generated payloads; file paths stay local."""
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()
                if not any(word in key.lower() for word in
                           ('password', 'secret', 'credential', 'token', 'api_key', 'artifact_path', 'raw_path', 'raw_json'))
                and not key.lower().endswith(('_path', '_directory', '_url'))}
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    return value


def _all_events(store, run_id):
    events, after = [], None
    while True:
        page = store.query(run_id, limit=1000, after=after)
        if not page:
            return events
        page = sorted(page, key=lambda item: item['SK'])
        if after is not None and page[-1]['SK'] <= after:
            raise ValueError('Event store pagination did not advance')
        events.extend(_public(item) for item in page if not after or item['SK'] > after)
        after = page[-1]['SK']


def _metrics(session, run_id):
    query = (select(RunMetric, MetricDefinition.unit).join(MetricDefinition, RunMetric.name == MetricDefinition.name)
             .where(RunMetric.run_id == run_id).order_by(RunMetric.name))
    return [{**asdict(row), 'unit': unit} for row, unit in session.execute(query)]


def export_run(factory, store, run_id, format='json'):
    """Return (content bytes, download filename, media type); no filesystem writes.

    JSON includes the entire event history and only observations named in the
    input manifest. CSV is an explicitly limited, flat state-event report.
    Active runs are a best-effort snapshot, not an atomic cross-store backup.
    """
    if format not in {'json', 'csv'}:
        raise ValueError('Export format must be json or csv')
    with factory() as session:
        run = session.get(SimulationRun, run_id)
        if not run:
            raise LookupError('Run not found')
        run_data = {key: value for key, value in asdict(run).items() if key in RUN_FIELDS}
        manifest = {key: value for key, value in (run.input_manifest or {}).items() if key in MANIFEST_FIELDS}
        run_data['input_manifest'] = manifest
        ids = list(dict.fromkeys((run.input_manifest or {}).get('observation_ids', [])))
        observations = list(session.scalars(select(Observation).where(Observation.id.in_(ids)))) if ids else []
        by_id = {row.id: row for row in observations}
        observed = [{**{key: value for key, value in asdict(by_id[identity]).items()
                        if key != 'metadata_json'}, 'value_kind': 'observed'}
                    for identity in ids if identity in by_id]
        source_ids = {row.source_id for row in observations} | {run.source_id}
        region_ids = {row.region_id for row in observations}
        sources = [{key: value for key, value in asdict(row).items()
                    if key in ('id', 'name', 'provider', 'source_type', 'frequency', 'units', 'status')}
                   for row in session.scalars(select(DataSource).where(DataSource.id.in_(source_ids)))]
        regions = [{key: value for key, value in asdict(row).items() if key in ('id', 'name', 'timezone')}
                   for row in session.scalars(select(Region).where(Region.id.in_(region_ids)))]
        model = session.get(ModelRecord, run.model_id) if run.model_id else None
        model_info = ({key: value for key, value in asdict(model).items()
                       if key in ('id', 'name', 'version', 'target', 'source_id', 'region_id', 'algorithm', 'metrics', 'created_at')}
                      if model else None)
        orders = [asdict(row) for row in session.scalars(select(MarketOrder).where(MarketOrder.run_id == run_id).order_by(MarketOrder.step, MarketOrder.id))]
        trades = [asdict(row) for row in session.scalars(select(Trade).where(Trade.run_id == run_id).order_by(Trade.step, Trade.id))]
        participant_ids = {row['participant_id'] for row in orders}
        participants = [asdict(row) for row in session.scalars(select(Participant).where(Participant.id.in_(participant_ids)))]
        metrics = _metrics(session, run_id)
    events = _all_events(store, run_id)
    states = [event for event in events if event['event_type'] == 'STATE']
    filename = f'gridmind-run-{run_id}'
    if format == 'csv':
        rows = [{**{key: event.get('payload', {}).get(key) for key in STATE_COLUMNS},
                 'step': event['step'], 'timestamp': event['timestamp'],
                 'input_observation_id': event.get('payload', {}).get('input_observation_id'),
                 'input_source_id': event.get('payload', {}).get('input_source_id'),
                 'input_unit': event.get('payload', {}).get('input_unit'),
                 'value_kind': event.get('payload', {}).get('value_kind', 'modelled')}
                for event in states]
        return (_csv_bytes(('step', 'timestamp', 'input_observation_id', 'input_source_id', 'input_unit', 'value_kind', *STATE_COLUMNS), rows),
                filename + '-states.csv', 'text/csv')
    payload = _public({
        'schema_version': SCHEMA_VERSION, 'run': run_data,
        'export_scope': 'Snapshot of persisted records; an active run can continue after export.',
        'observations': observed, 'missing_observation_ids': [identity for identity in ids if identity not in by_id],
        'sources': sources, 'regions': regions, 'model': model_info,
        'states': states,
        'decisions': [event for event in events if event['event_type'] == 'AGENT_DECISION'],
        'safety_checks': [event for event in events if event['event_type'] == 'SAFETY_CHECK'],
        'orders': orders, 'trades': trades, 'participants': participants, 'metrics': metrics,
    })
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'), filename + '.json', 'application/json'


def export_comparison(factory, comparison_id):
    """Long-form CSV of the actual persisted metrics, including incomplete runs."""
    with factory() as session:
        runs = list(session.scalars(select(SimulationRun).where(SimulationRun.comparison_id == comparison_id).order_by(SimulationRun.policy)))
        if not runs:
            raise LookupError('Comparison not found')
        rows = []
        for run in runs:
            metrics = _metrics(session, run.id)
            info = {'comparison_id': comparison_id, 'run_id': run.id, 'policy': run.policy,
                    'status': run.status, 'completed_steps': run.completed_steps, 'total_steps': run.total_steps}
            rows.extend({**info, 'metric': metric['name'], 'value': metric['value'], 'unit': metric['unit']} for metric in metrics)
            if not metrics:
                rows.append({**info, 'metric': '', 'value': '', 'unit': ''})
    columns = ('comparison_id', 'run_id', 'policy', 'status', 'completed_steps', 'total_steps', 'metric', 'value', 'unit')
    return _csv_bytes(columns, rows), f'gridmind-comparison-{comparison_id}.csv', 'text/csv'
