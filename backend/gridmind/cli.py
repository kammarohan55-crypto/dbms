"""Reproducible local setup, training and paired evaluation commands."""
import argparse
import json
import time
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from .config import settings
from .database import initialize, make_engine
from .models import SimulationRun, Asset, uid
from .ingestion import ingest_karnataka
from .forecast_service import train, observation_series, replay_forecasts, predict
from .events import create_store
from .runner import RunService
from .schemas import RunInput
from .simulation import Scenario
from .analytics import comparison


def main():
    parser = argparse.ArgumentParser(description='GRIDMIND local database and simulation tools')
    parser.add_argument('command',choices=['init-db','ingest','train','demo','export-schema'])
    parser.add_argument('--steps',type=int,default=48)
    parser.add_argument('--scenario',default='high_demand')
    args = parser.parse_args()
    if args.command == 'export-schema':
        from sqlalchemy.schema import CreateTable, CreateIndex
        from sqlalchemy.dialects import mysql
        from .models import Base
        statements = ['-- GRIDMIND fresh schema snapshot: 002_normalized_integrity. MySQL >= 8.0.16; InnoDB. Generated from models.py.']
        for table in Base.metadata.sorted_tables:
            statements.append(str(CreateTable(table).compile(dialect=mysql.dialect()))+' ENGINE=InnoDB;')
            statements.extend(str(CreateIndex(index).compile(dialect=mysql.dialect()))+';' for index in table.indexes)
        path = settings.path('sql/001_schema.sql')
        path.write_text('\n\n'.join(statements)+'\n',encoding='utf-8')
        from .migrations import trade_guard_statement, VERSION, DESCRIPTION
        from .metric_definitions import METRICS
        analytics = settings.path('sql/002_analytics.sql')
        marker = '-- Step-scoped trade guards generated from migrations.py.'
        source = analytics.read_text(encoding='utf-8').split(marker)[0].rstrip()
        guards = '\n\n'.join(trade_guard_statement(operation,'mysql')+'$$' for operation in ['INSERT','UPDATE'])
        analytics.write_text(source+'\n\n'+marker+'\nDELIMITER $$\n'+guards+'\nDELIMITER ;\n',encoding='utf-8')
        quote = lambda value: "'"+value.replace("'","''")+"'"
        rows = ',\n'.join('('+','.join(map(quote,[name,unit,description]))+')' for name,(unit,description) in METRICS.items())
        seeds = '-- Fresh schema seed; existing databases use scripts/migrate.py.\nINSERT INTO metric_definitions(name,unit,description) VALUES\n'+rows+';\n'
        seeds += 'INSERT INTO schema_migrations(version,applied_at,description) VALUES ('+quote(VERSION)+',UTC_TIMESTAMP(),'+quote(DESCRIPTION)+');\n'
        settings.path('sql/004_metric_definitions.sql').write_text(seeds,encoding='utf-8')
        print('Exported versioned MySQL schema.')
        return
    engine = make_engine(settings.database_url)
    initialize(engine)
    factory = sessionmaker(engine,expire_on_commit=False)
    if args.command == 'init-db':
        print(json.dumps({'initialized':True,'backend':engine.dialect.name}))
    elif args.command == 'ingest':
        result = ingest_karnataka(factory,settings.path(settings.raw_directory))
        print(json.dumps({key:result[key] for key in ['source_id','inserted','duplicates','mode']}))
    elif args.command == 'train':
        record = train(factory,settings.path(settings.model_directory))
        print(json.dumps({'id':record['id'],'version':record['version'],'metrics':record['metrics']},indent=2))
        predict(factory,record['id'],14)
    else:
        with factory() as session:
            observations = observation_series(session)
            replay,model_id,values,forecasts = replay_forecasts(session,observations,steps=args.steps)
        if args.steps > len(replay):
            raise ValueError(f'Only {len(replay)} held-out replay observations available')
        runner = RunService(factory,create_store(settings))
        try:
            result = runner.start(RunInput(scenario=Scenario(name=args.scenario,steps=args.steps)),replay,model_id,values,forecasts)
            while True:
                with factory() as session:
                    states = list(session.scalars(select(SimulationRun).where(SimulationRun.comparison_id==result['comparison_id'])))
                    if all(r.status not in {'queued','running','paused'} for r in states):
                        report = comparison(session,result['comparison_id'])
                        report['errors'] = [r.error for r in states if r.error]
                        settings.path('docs/latest_evaluation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
                        print(json.dumps(report,indent=2))
                        if any(r.status != 'completed' for r in states):
                            raise SystemExit(1)
                        break
                time.sleep(.2)
        finally:
            runner.shutdown()
    engine.dispose()


if __name__ == '__main__':
    main()
