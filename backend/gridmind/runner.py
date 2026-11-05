"""Bounded workers with durable, cooperative controls at step boundaries."""
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select, func
from .models import SimulationRun, RunMetric, Trade, Forecast, utcnow, uid
from .database import asdict
from .events import event_record
from .simulation import Scenario, generate_steps, metrics_for_steps
from .market import settle_step

logger = logging.getLogger('gridmind.simulation')
ACTIVE = {'queued', 'running', 'paused'}


class _RunControl(threading.Condition):
    def __init__(self):
        super().__init__(threading.RLock())
        self.request_lock = threading.Lock()
        self.waiting_commands = 0


class RunService:
    def __init__(self, factory, store):
        self.factory, self.store = factory, store
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='gridmind')
        self._registry_lock = threading.RLock()
        self._controls = {}
        self._owned = set()
        self._stopping = threading.Event()

    def _control(self, run_id):
        with self._registry_lock:
            return self._controls.setdefault(run_id, _RunControl())

    @staticmethod
    def _locked_run(session, run_id):
        run = session.scalar(select(SimulationRun).where(SimulationRun.id == run_id).with_for_update())
        if run is None:
            raise LookupError('Simulation run not found')
        return run

    def start(self, request, observations, model_id=None, forecast_values=None, forecast_records=None):
        if not observations:
            raise ValueError('Import observations before starting a simulation')
        comparison_id = uid()
        policies = ['baseline', 'gridmind'] if request.compare else ['gridmind']
        manifest = {'observation_ids': [o['id'] for o in observations],
                    'sha256': hashlib.sha256(json.dumps(observations, sort_keys=True).encode()).hexdigest(),
                    'series': {key: observations[0].get(key) for key in ('source_id', 'region_id', 'metric', 'unit', 'series_key')},
                    'provenance': 'Regional observations scaled to virtual load; all assets are modelled',
                    'forecast_mode': 'registered held-out model' if model_id else 'current observed context; ML forecast unavailable'}
        runs = []
        # Serialize admission and submission so shutdown cannot abandon a newly admitted run.
        with self._registry_lock:
            if self._stopping.is_set():
                raise RuntimeError('Simulation service is shutting down')
            if len(self._owned) + len(policies) > 4:
                raise ValueError('Simulation queue is full. Resume or cancel a paused run, or retry shortly.')
            with self.factory.begin() as session:
                active = list(session.scalars(select(SimulationRun.id).where(SimulationRun.status.in_(ACTIVE)).with_for_update()))
                if len(active) + len(policies) > 4:
                    raise ValueError('At most four queued, running or paused simulations are allowed')
                for policy in policies:
                    run = SimulationRun(id=uid(), comparison_id=comparison_id, name=request.scenario.name,
                                        policy=policy, mode=request.mode, source_id=observations[0]['source_id'],
                                        model_id=model_id, config=request.scenario.model_dump(), input_manifest=manifest,
                                        total_steps=request.scenario.steps, status='queued', completed_steps=0)
                    session.add(run)
                    session.flush()
                    for forecast in forecast_records or []:
                        session.add(Forecast(model_id=model_id, run_id=run.id, target_at=forecast['target_at'], value=forecast['value'],
                                             lower=forecast.get('lower'), upper=forecast.get('upper'), metadata_json={'purpose': 'causal replay forecast; trained before replay window'}))
                    runs.append(asdict(run))
            for run in runs:
                self._control(run['id'])
                self._owned.add(run['id'])
                self.executor.submit(self.execute, run['id'], observations, forecast_values)
        return {'comparison_id': comparison_id, 'runs': runs}

    def change_status(self, run_id, action):
        """Acknowledge controls only after an in-flight step finishes durably.

        The step lock also protects final metrics, so cancellation cannot race
        with a false successful completion.
        """
        if action not in {'pause', 'resume', 'cancel'}:
            raise ValueError('Unknown simulation control')
        control = self._control(run_id)
        # Announce before waiting for the step lock. Controls take priority over
        # the next step/finalization even when replay runs without any pacing.
        with control.request_lock:
            control.waiting_commands += 1
        with control:
            try:
                with self.factory.begin() as session:
                    run = self._locked_run(session, run_id)
                    if run.status in ACTIVE:
                        if action == 'pause':
                            run.status = 'paused'
                        elif action == 'resume' and run.status == 'paused':
                            run.status = 'running' if run.started_at else 'queued'
                        elif action == 'cancel':
                            run.status, run.finished_at = 'cancelled', utcnow()
                        session.flush()
                    return asdict(run)
            finally:
                with control.request_lock:
                    control.waiting_commands -= 1
                control.notify_all()

    def _ready(self, run_id, control):
        """Called with the step lock held; waiting releases it for controls."""
        while not self._stopping.is_set():
            with control.request_lock:
                pending = control.waiting_commands
            if pending:
                control.wait()
                continue
            with self.factory.begin() as session:
                run = self._locked_run(session, run_id)
                status = run.status
                if status == 'queued':
                    run.status, run.started_at = 'running', run.started_at or utcnow()
                    return True
                if status == 'running':
                    return True
                if status != 'paused':
                    return False
            control.wait()
        return False

    def _pace(self, run_id, control, seconds):
        # Condition waits are interruptible even at the minimum replay speed.
        remaining = seconds
        with control:
            while remaining > 0:
                if not self._ready(run_id, control):
                    return False
                started = time.monotonic()
                control.wait(timeout=remaining)
                remaining -= time.monotonic() - started
            return self._ready(run_id, control)

    def execute(self, run_id, observations, forecast_values=None):
        control = self._control(run_id)
        try:
            with control:
                if not self._ready(run_id, control):
                    return
                with self.factory() as session:
                    run = session.get(SimulationRun, run_id)
                    scenario, policy = Scenario(**run.config), run.policy
            completed = []
            records = iter(generate_steps(scenario, observations, policy=policy, forecast_values=forecast_values))
            while True:
                with control:
                    if not self._ready(run_id, control):
                        return
                    try:
                        record = next(records)
                    except StopIteration:
                        self._complete(run_id, completed, scenario)
                        break
                    step, state = record['step'], record['state']
                    if not record['safety']['approved'] and not record['safety'].get('fallback_approved', False):
                        raise ValueError('Safety rejected a plan without an approved fallback')
                    served_kwh = max(0, state['demand_kw'] - state['curtailed_kw'] - state['unmet_kw']) * scenario.step_hours
                    settle_step(self.factory, run_id, step, record.get('market_orders', []),
                                available_kwh=state['generation_kw'] * scenario.step_hours, permitted_buy_kwh=served_kwh)
                    timestamp = record['timestamp']
                    self.store.put(event_record(run_id, step, 'STATE', 'microgrid', timestamp,
                                                 {**state, 'input_observation_id': record.get('input_observation_id'), 'value_kind': 'modelled', 'policy': policy}))
                    for index, decision in enumerate(record['decisions']):
                        self.store.put(event_record(run_id, step, 'AGENT_DECISION', f'{index}:{decision["agent"]}', timestamp, decision))
                    self.store.put(event_record(run_id, step, 'SAFETY_CHECK', 'validation', timestamp, record['safety']))
                    completed.append(record)
                    with self.factory.begin() as session:
                        run = self._locked_run(session, run_id)
                        run.completed_steps = len(completed)
                if scenario.replay_speed and len(completed) < scenario.steps:
                    if not self._pace(run_id, control, 1 / scenario.replay_speed):
                        return
            logger.info('run_completed', extra={'run_id': run_id, 'steps': len(completed)})
        except Exception as exc:
            logger.exception('run_failed', extra={'run_id': run_id})
            with control:
                with self.factory.begin() as session:
                    run = self._locked_run(session, run_id)
                    if run.status in ACTIVE and not self._stopping.is_set():
                        run.status, run.error, run.finished_at = 'failed', f'{type(exc).__name__}: {str(exc)[:400]}', utcnow()
        finally:
            with self._registry_lock:
                self._owned.discard(run_id)
                self._controls.pop(run_id, None)

    def _complete(self, run_id, completed, scenario):
        metrics = metrics_for_steps(completed, scenario)
        with self.factory.begin() as session:
            run = self._locked_run(session, run_id)
            if run.status != 'running' or self._stopping.is_set():
                return
            if len(completed) != run.total_steps:
                raise RuntimeError('Simulation ended before every requested step completed')
            metrics['p2p_volume_kwh'] = float(session.scalar(select(func.coalesce(func.sum(Trade.quantity_kwh), 0)).where(Trade.run_id == run_id)))
            metrics['p2p_transaction_value'] = float(session.scalar(select(func.coalesce(func.sum(Trade.quantity_kwh * Trade.price), 0)).where(Trade.run_id == run_id)))
            for name, value in metrics.items():
                session.add(RunMetric(run_id=run_id, name=name, value=float(value)))
            run.status, run.finished_at = 'completed', utcnow()

    def shutdown(self):
        with self._registry_lock:
            self._stopping.set()
            owned = [(run_id, self._control(run_id)) for run_id in self._owned]
        for run_id, control in owned:
            with control:
                with self.factory.begin() as session:
                    run = self._locked_run(session, run_id)
                    if run.status in ACTIVE:
                        run.status, run.error, run.finished_at = 'interrupted', 'Application stopped before run completion; start a new run.', utcnow()
                control.notify_all()
        self.executor.shutdown(wait=True, cancel_futures=True)
