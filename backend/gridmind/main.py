from contextlib import asynccontextmanager
import hmac
import logging
from pathlib import Path
from uuid import UUID
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text, func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import sessionmaker, aliased
from .config import Settings, settings, ROOT
from .models import DataSource, Region, Observation, Asset, ModelRecord, Forecast, SimulationRun, Participant, MarketOrder, Trade, RunMetric, AuditLog, utcnow
from .database import make_engine, initialize, asdict
from .events import create_store
from .schemas import (AssetInput, TrainInput, PredictInput, RunInput, SourceResponse,
    ObservationResponse, AssetResponse, ModelResponse, ForecastResponse, RunResponse, RunStartResponse, EventResponse)
from .simulation import Scenario, SCENARIOS
from .ingestion import ingest_karnataka, ingest_csv, parse_time
from .forecast_service import train, predict, observation_series, replay_forecasts
from .runner import RunService
from .analytics import database_demo, comparison
from .live_context import refresh as refresh_live

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
log = logging.getLogger('gridmind.api')


def create_app(config: Settings | None = None):
    config = config or settings
    engine = make_engine(config.database_url)
    factory = sessionmaker(engine,expire_on_commit=False)
    store = create_store(config)

    @asynccontextmanager
    async def lifespan(application):
        try:
            initialize(engine,recover_runs=True)
            application.state.ready = True
        except SQLAlchemyError:
            log.error('Database unavailable. Configure DATABASE_URL or run scripts/setup_mysql.py.')
            application.state.ready = False
        except RuntimeError as exc:
            log.error('Database initialization requires attention: %s',exc)
            application.state.ready = False
        yield
        application.state.runner.shutdown()
        engine.dispose()

    app = FastAPI(title='GRIDMIND',version='1.0.0',description='DBMS-centred virtual microgrid. Observed, predicted and modelled values are explicitly separated.',lifespan=lifespan)
    app.state.ready = False
    app.state.engine, app.state.factory, app.state.store = engine,factory,store
    app.state.runner = RunService(factory,store)

    @app.middleware('http')
    async def security(request: Request, call_next):
        if request.url.path.startswith('/api/') and request.url.path != '/api/health':
            if config.api_key and not hmac.compare_digest(request.headers.get('X-API-Key',''),config.api_key):
                return JSONResponse({'detail':'A valid X-API-Key is required'},status_code=401)
            if not app.state.ready:
                return JSONResponse({'detail':'Relational database is unavailable. Check configuration and restart.'},status_code=503)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(ValueError)
    async def invalid_input(request,exc):
        return JSONResponse({'detail':str(exc)},status_code=422)

    @app.exception_handler(IntegrityError)
    async def constraint_error(request,exc):
        return JSONResponse({'detail':'Database constraint rejected this change: duplicate name, invalid values, or referenced record.'},status_code=409)

    @app.exception_handler(SQLAlchemyError)
    async def dependency_error(request,exc):
        log.error('Database operation failed: %s',type(exc).__name__)
        return JSONResponse({'detail':'Database operation failed; the transaction was rolled back.'},status_code=503)

    @app.exception_handler(RuntimeError)
    async def service_error(request,exc):
        return JSONResponse({'detail':str(exc)[:400]},status_code=503)

    def require_run(session,run_id):
        try:
            UUID(run_id)
        except ValueError:
            raise HTTPException(422,'Invalid run identifier')
        run = session.get(SimulationRun,run_id)
        if not run:
            raise HTTPException(404,'Simulation run not found')
        return run

    @app.get('/api/health')
    def health():
        relational_ready,events_ready = False,False
        try:
            with engine.connect() as connection:
                connection.execute(text('SELECT 1'))
            relational_ready = app.state.ready
        except SQLAlchemyError:
            pass
        try:
            events_ready = store.ready()
        except Exception:
            pass
        return {'status':'ready' if relational_ready and events_ready else 'degraded',
                'relational':{'backend':engine.dialect.name,'ready':relational_ready},
                'events':{'backend':store.backend,'ready':events_ready},'aws_deferred':config.event_backend == 'local',
                'scope':'Local research simulation; no physical grid control'}

    @app.get('/api/sources',response_model=list[SourceResponse])
    def sources():
        with factory() as session:
            return [{**asdict(s),'observation_count':session.scalar(select(func.count()).select_from(Observation).where(Observation.source_id==s.id))} for s in session.scalars(select(DataSource))]

    @app.get('/api/series')
    def available_series(source_id: str | None = None):
        """Expose compatible series identities so clients can select data explicitly."""
        with factory() as session:
            fields = [Observation.source_id, DataSource.name.label('source_name'), Observation.region_id,
                      Region.name.label('region_name'), Observation.metric, Observation.unit, Observation.series_key]
            query = select(*fields, func.count(Observation.id).label('count'),
                           func.min(Observation.observed_at).label('start'), func.max(Observation.observed_at).label('end'))
            query = query.join(DataSource, Observation.source_id == DataSource.id).join(Region, Observation.region_id == Region.id)
            if source_id:
                query = query.where(Observation.source_id == source_id)
            rows = session.execute(query.group_by(*fields).order_by(DataSource.name, Region.name, Observation.metric, Observation.series_key)).mappings()
            return [{**row, 'start': row['start'].isoformat()+'Z', 'end': row['end'].isoformat()+'Z',
                     'start_at':row['start'].isoformat()+'Z','end_at':row['end'].isoformat()+'Z'} for row in rows]

    @app.post('/api/ingestion/karnataka')
    def import_karnataka():
        return ingest_karnataka(factory,config.path(config.raw_directory))

    @app.post('/api/ingestion/csv')
    async def import_csv(file: UploadFile = File(...),source_name: str = Form('Uploaded dataset',max_length=150),region_name: str = Form('Custom',max_length=100),timezone: str = Form('UTC',max_length=60)):
        content = await file.read(10*1024*1024+1)
        if len(content)>10*1024*1024:
            raise HTTPException(413,'Upload a bounded CSV of at most 10 MiB')
        return ingest_csv(factory,content,config.path(config.raw_directory),source_name,region_name,timezone)

    @app.get('/api/observations',response_model=list[ObservationResponse])
    def observations(limit: int = Query(300,ge=1,le=10000),offset: int = Query(0,ge=0),source_id: str | None = None,region_id: str | None = None,metric: str | None = None,series_key: str | None = None):
        with factory() as session:
            query = select(Observation,DataSource.name,Region.name).join(DataSource,Observation.source_id==DataSource.id).join(Region,Observation.region_id==Region.id)
            if source_id:
                query = query.where(Observation.source_id==source_id)
            if region_id:
                query = query.where(Observation.region_id==region_id)
            if metric:
                query = query.where(Observation.metric==metric)
            if series_key:
                query = query.where(Observation.series_key==series_key)
            rows = session.execute(query.order_by(Observation.observed_at.desc()).offset(offset).limit(limit)).all()
            return [{**asdict(o),'source_name':source,'region_name':region,'value_kind':'observed'} for o,source,region in reversed(rows)]

    @app.post('/api/live-context/refresh')
    def live_context():
        return refresh_live(factory,config)

    @app.get('/api/assets',response_model=list[AssetResponse])
    def assets():
        with factory() as session:
            return [asdict(a) for a in session.scalars(select(Asset))]

    @app.post('/api/assets',status_code=201,response_model=AssetResponse)
    def create_asset(body: AssetInput):
        with factory.begin() as session:
            asset = Asset(**body.model_dump())
            session.add(asset)
            session.flush()
            session.add(AuditLog(action='asset_create',entity_id=asset.id,detail=body.model_dump()))
            return asdict(asset)

    @app.put('/api/assets/{asset_id}',response_model=AssetResponse)
    def update_asset(asset_id: str,body: AssetInput):
        with factory.begin() as session:
            asset = session.get(Asset,asset_id)
            if not asset:
                raise HTTPException(404,'Asset not found')
            for key,value in body.model_dump().items():
                setattr(asset,key,value)
            session.add(AuditLog(action='asset_update',entity_id=asset.id,detail=body.model_dump()))
            session.flush()
            return asdict(asset)

    @app.delete('/api/assets/{asset_id}')
    def delete_asset(asset_id: str):
        with factory.begin() as session:
            asset = session.get(Asset,asset_id)
            if not asset:
                raise HTTPException(404,'Asset not found')
            session.add(AuditLog(action='asset_delete',entity_id=asset.id,detail={'name':asset.name}))
            session.delete(asset)
        return {'deleted':asset_id}

    @app.get('/api/models',response_model=list[ModelResponse])
    def models():
        with factory() as session:
            return [asdict(m) for m in session.scalars(select(ModelRecord).order_by(ModelRecord.created_at.desc()))]

    @app.post('/api/forecasts/train',response_model=ModelResponse)
    def train_model(body: TrainInput):
        return train(factory,config.path(config.model_directory),body.target,body.source_id,
                     region_id=body.region_id,metric=body.metric,series_key=body.series_key)

    @app.post('/api/forecasts/generate',response_model=list[ForecastResponse])
    def generate_forecasts(body: PredictInput):
        return predict(factory,body.model_id,body.horizon)

    @app.get('/api/forecasts',response_model=list[ForecastResponse])
    def forecasts(model_id: str | None = None,limit: int = Query(500,ge=1,le=2000)):
        with factory() as session:
            query = select(Forecast).order_by(Forecast.generated_at.desc(),Forecast.target_at)
            if model_id:
                query = query.where(Forecast.model_id==model_id)
            return [asdict(f) for f in session.scalars(query.limit(limit))]

    @app.get('/api/scenarios')
    def scenarios():
        return [Scenario(name=name).model_dump() for name in SCENARIOS]

    @app.post('/api/runs',status_code=202,response_model=RunStartResponse)
    def start_run(body: RunInput):
        if body.mode == 'live-context':
            result = refresh_live(factory,config)
            body.source_id = result['source_id']
        with factory() as session:
            observed = observation_series(session,body.source_id,region_id=body.region_id,metric=body.metric,series_key=body.series_key)
            replay,model_id,values,forecast_rows = replay_forecasts(session,observed,body.model_id,steps=body.scenario.steps)
            # Persisted asset capacities supply defaults; explicitly supplied scenario
            # parameters take priority. Both policies receive the exact same snapshot.
            updates = {}
            for kind,field in [('solar','solar_capacity_kw'),('wind','wind_capacity_kw')]:
                configured = list(session.scalars(select(Asset).where(Asset.asset_type==kind)))
                if configured and field not in body.scenario.model_fields_set:
                    updates[field] = sum(a.capacity_kw for a in configured)
            batteries = list(session.scalars(select(Asset).where(Asset.asset_type=='battery')))
            if batteries:
                for field,value in [('battery_capacity_kwh',sum(a.capacity_kwh for a in batteries)),('battery_power_kw',sum(a.capacity_kw for a in batteries)),('min_soc',max(a.min_soc for a in batteries)),('max_soc',min(a.max_soc for a in batteries))]:
                    if field not in body.scenario.model_fields_set:
                        updates[field] = value
            loads = list(session.scalars(select(Asset).where(Asset.asset_type.in_(['household','industrial','critical','ev']))))
            if loads:
                total_load = sum(a.capacity_kw for a in loads)
                if 'base_load_kw' not in body.scenario.model_fields_set:
                    updates['base_load_kw'] = total_load
                if 'critical_fraction' not in body.scenario.model_fields_set:
                    updates['critical_fraction'] = sum(a.capacity_kw for a in loads if a.asset_type == 'critical') / total_load
                if 'flexible_fraction' not in body.scenario.model_fields_set:
                    ev_load = sum(a.capacity_kw for a in loads if a.asset_type == 'ev')
                    critical = updates.get('critical_fraction',body.scenario.critical_fraction)
                    updates['flexible_fraction'] = min(ev_load / total_load if ev_load else body.scenario.flexible_fraction,1-critical)
            body.scenario = Scenario(**{**body.scenario.model_dump(),**updates})
        if body.scenario.steps > len(replay):
            raise ValueError(f'Only {len(replay)} compatible replay observations are available; reduce the simulation steps.')
        return app.state.runner.start(body,replay,model_id,values,forecast_rows)

    @app.get('/api/runs',response_model=list[RunResponse])
    def runs(limit: int = Query(100,ge=1,le=500)):
        with factory() as session:
            return [asdict(r) for r in session.scalars(select(SimulationRun).order_by(SimulationRun.created_at.desc()).limit(limit))]

    @app.get('/api/runs/{run_id}',response_model=RunResponse)
    def run_detail(run_id: str):
        with factory() as session:
            return asdict(require_run(session,run_id))

    @app.post('/api/runs/{run_id}/cancel',response_model=RunResponse)
    def cancel_run(run_id: str):
        with factory() as session:
            require_run(session,run_id)
        return app.state.runner.change_status(run_id,'cancel')

    @app.post('/api/runs/{run_id}/pause',response_model=RunResponse)
    def pause_run(run_id: str):
        with factory() as session:
            require_run(session,run_id)
        return app.state.runner.change_status(run_id,'pause')

    @app.post('/api/runs/{run_id}/resume',response_model=RunResponse)
    def resume_run(run_id: str):
        with factory() as session:
            require_run(session,run_id)
        return app.state.runner.change_status(run_id,'resume')

    @app.get('/api/runs/{run_id}/export')
    def download_run(run_id: str,format: str = Query('json',pattern='^(json|csv)$')):
        from .exports import export_run
        with factory() as session:
            require_run(session,run_id)
        try:
            content,filename,media_type = export_run(factory,store,run_id,format=format)
        except LookupError as exc:
            raise HTTPException(404,str(exc))
        return Response(content,media_type=media_type,headers={'Content-Disposition':f'attachment; filename="{filename}"'})

    @app.get('/api/runs/{run_id}/events',response_model=list[EventResponse])
    def events(run_id: str,event_type: str | None = Query(None,pattern='^(STATE|AGENT_DECISION|SAFETY_CHECK)$'),step: int | None = Query(None,ge=0),limit: int = Query(1000,ge=1,le=5000),after: str | None = None):
        with factory() as session:
            require_run(session,run_id)
        try:
            return store.query(run_id,step=step,event_type=event_type,limit=limit,after=after)
        except (ValueError,OSError) as exc:
            raise HTTPException(503,f'Operational event store unavailable: {str(exc)[:150]}')

    @app.get('/api/runs/{run_id}/metrics',response_model=dict[str,float])
    def metrics(run_id: str):
        with factory() as session:
            require_run(session,run_id)
            return dict(session.execute(select(RunMetric.name,RunMetric.value).where(RunMetric.run_id==run_id)).all())

    @app.get('/api/runs/{run_id}/market')
    def market(run_id: str):
        with factory() as session:
            require_run(session,run_id)
            orders = [{**asdict(o),'participant_name':name} for o,name in session.execute(select(MarketOrder,Participant.name).join(Participant).where(MarketOrder.run_id==run_id).order_by(MarketOrder.step))]
            buy,sell = aliased(MarketOrder),aliased(MarketOrder)
            buyer,seller = aliased(Participant),aliased(Participant)
            query = select(Trade,buyer.name,seller.name).join(buy,Trade.buy_order_id==buy.id).join(sell,Trade.sell_order_id==sell.id).join(buyer,buy.participant_id==buyer.id).join(seller,sell.participant_id==seller.id).where(Trade.run_id==run_id).order_by(Trade.step)
            trades = [{**asdict(t),'buyer_name':b,'seller_name':s} for t,b,s in session.execute(query)]
            return {'orders':orders,'trades':trades}

    @app.get('/api/comparisons/{comparison_id}')
    def compare(comparison_id: str):
        with factory() as session:
            result = comparison(session,comparison_id)
            if not result['runs']:
                raise HTTPException(404,'Comparison not found')
            return result

    @app.get('/api/comparisons/{comparison_id}/export')
    def download_comparison(comparison_id: str):
        from .exports import export_comparison
        try:
            content,filename,media_type = export_comparison(factory,comparison_id)
        except LookupError as exc:
            raise HTTPException(404,str(exc))
        return Response(content,media_type=media_type,headers={'Content-Disposition':f'attachment; filename="{filename}"'})

    @app.get('/api/db/demo')
    def db_demo():
        with factory() as session:
            return database_demo(session,engine)

    @app.get('/api/audit')
    def audit():
        with factory() as session:
            return [asdict(a) for a in session.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200))]

    frontend = ROOT / 'frontend'
    if frontend.exists():
        app.mount('/static',StaticFiles(directory=frontend),name='static')
        @app.get('/docs/DBMS.md',include_in_schema=False)
        def database_notes():
            return FileResponse(ROOT/'docs'/'DBMS.md',media_type='text/markdown',filename='GRIDMIND-database-design.md')

        @app.get('/learning-notes',include_in_schema=False)
        def learning_notes():
            path = ROOT/'docs'/'LEARNING_GUIDE.md'
            if not path.is_file():
                raise HTTPException(404,'The learning notes are not available')
            return FileResponse(path,media_type='text/markdown',filename='GRIDMIND-learning-notes.md')

        app.mount('/learn',StaticFiles(directory=frontend/'learn',html=True,check_dir=False),name='learning')
        @app.get('/',include_in_schema=False)
        def dashboard():
            return FileResponse(frontend/'index.html')
    return app


app = create_app()
