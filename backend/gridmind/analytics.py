from time import perf_counter
from sqlalchemy import select, text, func
from .models import Base, Observation, SimulationRun, RunMetric


def comparison(session, comparison_id):
    return {'comparison_id':comparison_id,'runs':[{'id':r.id,'policy':r.policy,'status':r.status,
            'metrics':dict(session.execute(select(RunMetric.name,RunMetric.value).where(RunMetric.run_id == r.id)).all())}
            for r in session.scalars(select(SimulationRun).where(SimulationRun.comparison_id == comparison_id))]}


def database_demo(session, engine):
    tables = [{'name':table.name,'count':session.scalar(select(func.count()).select_from(table))} for table in Base.metadata.sorted_tables]
    joins = [dict(r) for r in session.execute(text('SELECT s.name AS source, r.name AS region, o.metric, COUNT(*) AS observations, MIN(o.observed_at) AS first_observation, MAX(o.observed_at) AS latest_observation FROM energy_observations o JOIN data_sources s ON s.id=o.source_id JOIN regions r ON r.id=o.region_id GROUP BY s.name,r.name,o.metric')).mappings()]
    comparisons = [dict(r) for r in session.execute(text('SELECT * FROM v_run_comparison LIMIT 100')).mappings()]
    indexes = [{'table':table.name,'name':index.name,'columns':[c.name for c in index.columns]} for table in Base.metadata.sorted_tables for index in table.indexes]
    benchmark = {'available':False,'note':'Import observations to benchmark queries. Small warm-cache timings do not imply production speedups.'}
    observation = session.scalar(select(Observation).limit(1))
    if observation:
        indexed = 'SELECT id,observed_at,value FROM energy_observations WHERE region_id=:region AND observed_at>=:start ORDER BY observed_at LIMIT 100'
        unindexed = indexed
        if engine.dialect.name == 'mysql':
            indexed = indexed.replace('energy_observations WHERE','energy_observations FORCE INDEX (ix_observation_region_time) WHERE')
            unindexed = unindexed.replace('energy_observations WHERE','energy_observations IGNORE INDEX (ix_observation_region_time,ix_observation_metric_time) WHERE')
        elif engine.dialect.name == 'sqlite':
            indexed = indexed.replace('energy_observations WHERE','energy_observations INDEXED BY ix_observation_region_time WHERE')
            unindexed = unindexed.replace('energy_observations WHERE','energy_observations NOT INDEXED WHERE')
        parameters = {'region':observation.region_id,'start':observation.observed_at}
        timings = {}
        plans = {}
        for name,query in [('indexed',indexed),('nonindexed',unindexed)]:
            session.execute(text(query),parameters).all()
            elapsed = []
            for _ in range(5):
                start = perf_counter()
                session.execute(text(query),parameters).all()
                elapsed.append((perf_counter()-start)*1000)
            timings[name+'_ms'] = sorted(elapsed)[2]
            prefix = 'EXPLAIN QUERY PLAN ' if engine.dialect.name == 'sqlite' else 'EXPLAIN '
            plans[name] = [dict(row) for row in session.execute(text(prefix+query),parameters).mappings()]
        benchmark = {'available':True,**timings,'plans':plans,'repetitions':5,'statistic':'median milliseconds, warm connection','row_count':next(t['count'] for t in tables if t['name']=='energy_observations'),'note':'Equivalent queries; no index dropped. Warm-cache microbenchmark, not a production performance claim.'}
    return {'tables':tables,'joins':joins,'comparison':comparisons,'indexes':indexes,'benchmark':benchmark,
            'ownership':{'mysql':'Entities, observations, models, forecasts, run metadata, ACID market settlement, final metrics and audit',
                         'events':'Modelled STATE, AGENT_DECISION and SAFETY_CHECK payloads; local journal until AWS is supplied'},
            'dialect':engine.dialect.name,'normalization_document':'/docs/DBMS.md'}
