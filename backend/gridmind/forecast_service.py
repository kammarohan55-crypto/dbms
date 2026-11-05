"""Registered forecasting with explicit series identity and repeat-safe storage."""
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .models import Observation, ModelRecord, Forecast, AuditLog, Region
from .database import asdict
from .forecasting import train_forecaster, load_predict
from .ingestion import parse_time


def observation_series(session, source_id=None, *, target=None, region_id=None, metric=None, series_key=None):
    query = select(Observation).order_by(Observation.observed_at)
    if source_id:
        query = query.where(Observation.source_id == source_id)
    if target in {'solar','wind'}:
        query = query.where(Observation.metric.like(f'%{target}%'))
    if region_id:
        query = query.where(Observation.region_id==region_id)
    if metric:
        query = query.where(Observation.metric==metric)
    if series_key:
        query = query.where(Observation.series_key==series_key)
    records = list(session.scalars(query))
    if not records:
        raise ValueError('No suitable observed target labels. Import an observed dataset first.')
    identities = {(r.source_id,r.region_id,r.metric,r.series_key) for r in records}
    if len(identities) != 1:
        raise ValueError('Multiple observed series match; select one source, region, metric and series_key explicitly')
    # A series must be explicitly unambiguous; never silently choose a household.
    reference = records[-1]
    region_name = session.get(Region,reference.region_id).name
    return [{**asdict(r),'region':region_name} for r in records]


def train(factory, directory, target='demand', source_id=None, *, region_id=None, metric=None, series_key=None):
    with factory() as session:
        observations = observation_series(session,source_id,target=target,region_id=region_id,metric=metric,series_key=series_key)
    metadata = train_forecaster(observations,directory,target)
    with factory.begin() as session:
        record = ModelRecord(id=metadata['id'],name=metadata['name'],version=metadata['version'],target=metadata['target'],
                             source_id=observations[0]['source_id'],region_id=observations[0]['region_id'],
                             algorithm=metadata['algorithm'],metrics=metadata['metrics'],metadata_json=metadata,
                             artifact_path=metadata['artifact_path'])
        session.add(record)
        session.add(AuditLog(action='model_train',entity_id=record.id,detail={'version':record.version,'training_data_sha256':metadata['training_data_sha256']}))
        session.flush()
        return asdict(record)


def predict(factory, model_id, horizon):
    with factory() as session:
        model = session.get(ModelRecord,model_id)
        if not model:
            raise ValueError('Model not found')
        observations = observation_series(session,model.source_id,region_id=model.region_id,
                                          metric=model.metadata_json.get('metric'),series_key=model.metadata_json.get('series_key'))
        predictions = load_predict(model.artifact_path,observations,horizon)
        algorithm = model.algorithm
    # An identical model, observed history, and target always has the same row
    # identity, even when requests use different horizons. Never use NULL run_id
    # uniqueness for this: SQL permits multiple NULL values in unique indexes.
    input_identity = [{key: (parse_time(row[key]).isoformat() if key == 'observed_at' else row.get(key))
                       for key in ('id','source_id','region_id','series_key','observed_at','metric','unit','value','quality_status')}
                      for row in observations]
    fingerprint = hashlib.sha256(json.dumps(input_identity,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    ids = [str(uuid5(NAMESPACE_URL,f"gridmind:forecast:{model_id}:{fingerprint}:{parse_time(p['target_at']).isoformat()}"))
           for p in predictions]
    for attempt in range(3):
        try:
            with factory.begin() as session:
                # Serialize writes for a model on MySQL. Deterministic primary
                # keys protect databases which do not implement row locks.
                session.scalar(select(ModelRecord).where(ModelRecord.id == model_id).with_for_update())
                result = []
                for forecast_id, prediction in zip(ids,predictions):
                    forecast = session.get(Forecast,forecast_id)
                    if forecast is None:
                        forecast = Forecast(id=forecast_id,model_id=model_id,target_at=parse_time(prediction['target_at']),value=prediction['value'],
                            lower=prediction['lower'],upper=prediction['upper'],metadata_json={
                                'value_kind':'predicted','unit':observations[0]['unit'],'algorithm':algorithm,
                                'horizon_type':'recursive; intervals heuristic','input_sha256':fingerprint,
                                'input_end':parse_time(observations[-1]['observed_at']).isoformat()})
                        session.add(forecast)
                        session.flush()
                        # Return stored precision, including MySQL DATETIME's
                        # seconds, identically on first and repeat requests.
                        session.refresh(forecast)
                    result.append(asdict(forecast))
                return result
        except IntegrityError:
            # Another writer can win an insert race on SQLite. When concurrent
            # horizons overlap partly, retry to add only the remaining targets.
            with factory() as session:
                existing = [session.get(Forecast,forecast_id) for forecast_id in ids]
                if all(existing):
                    return [asdict(row) for row in existing]
                if not any(existing) or attempt == 2:
                    raise


def replay_forecasts(session, observations, model_id=None, *, steps=336):
    """Use only a registered model trained AND calibrated before the replay.

    Held-out one-step predictions have actual past available and no future input.
    Never fit on the replay window or use fitted training values as predictions.
    """
    if isinstance(steps,bool) or not isinstance(steps,int) or not 1 <= steps <= 336:
        raise ValueError('steps must be an integer between 1 and 336 observation periods')
    if not observations:
        raise ValueError('Replay requires actual observations')
    query = select(ModelRecord).where(ModelRecord.source_id == observations[0]['source_id']).order_by(ModelRecord.created_at.desc())
    reference = observations[0]
    def compatible(candidate):
        meta = candidate.metadata_json
        return candidate.region_id == reference['region_id'] and all(meta.get(key)==reference.get(key) for key in ['metric','unit','region']) and (not meta.get('series_key') or meta['series_key']==reference['series_key'])
    model = session.get(ModelRecord,model_id) if model_id else next((candidate for candidate in session.scalars(query) if compatible(candidate)),None)
    if model_id and not model:
        raise ValueError('Model not found')
    if not model:
        return observations[-steps:],None,None,[]
    if model.source_id != observations[0]['source_id']:
        raise ValueError('Model and simulation input source do not match')
    if not compatible(model):
        raise ValueError('Model region, metric, unit or household series does not match the simulation input')
    metadata = model.metadata_json
    cutoff = parse_time(metadata['validation_end'])
    predictions = {parse_time(p['target_at']):p for p in metadata.get('test_predictions',[])}
    replay = [o for o in observations if parse_time(o['observed_at']) > cutoff and parse_time(o['observed_at']) in predictions][-steps:]
    if not replay:
        raise ValueError('Registered model has no compatible held-out replay observations')
    values, rows = [],[]
    for observation in replay:
        prediction = predictions[parse_time(observation['observed_at'])]
        # Legacy artifacts could contain MySQL FLOAT readings, later restored to
        # source precision. Permit only their bounded float32 roundtrip error.
        relative_tolerance = 1e-7 if metadata.get('artifact_schema') == 1 else 1e-9
        if abs(float(prediction['actual']) - float(observation['value'])) > max(1e-9,abs(float(observation['value'])) * relative_tolerance):
            raise ValueError('Replay observations changed since the registered held-out evaluation')
        values.append(prediction['value'])
        rows.append({**prediction,'target_at':parse_time(prediction['target_at'])})
    return replay,model.id,values,rows
