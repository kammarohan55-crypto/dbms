from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from .models import Base, Participant, SimulationRun, utcnow
from .migrations import schema_problems, seed_metrics, record_version, install_trade_guards, VIEW_SQL


def make_engine(url):
    engine = create_engine(url, pool_pre_ping=True, connect_args={'check_same_thread': False} if make_url(url).get_backend_name()=='sqlite' else {})
    if engine.dialect.name == 'sqlite':
        @event.listens_for(engine, 'connect')
        def sqlite_constraints(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('PRAGMA journal_mode=WAL')
    return engine


def initialize(engine, *, recover_runs=False):
    Base.metadata.create_all(engine)
    problems = schema_problems(engine)
    if problems:
        raise RuntimeError('Database upgrade required: run .venv\\Scripts\\python.exe scripts\\migrate.py. '+ '; '.join(problems))
    with engine.begin() as conn:
        seed_metrics(conn)
        record_version(conn)
        view_query = VIEW_SQL
        if engine.dialect.name == 'sqlite':
            conn.execute(text(view_query.replace('CREATE VIEW','CREATE VIEW IF NOT EXISTS',1)))
        elif not conn.execute(text("SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='v_run_comparison'")).scalar():
            conn.execute(text(view_query))
        if engine.dialect.name == 'mysql':
            # A status audit is generated even if a different SQL client updates a run.
            found = conn.execute(text("SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME='trg_run_status_audit'")).scalar()
            if not found:
                conn.execute(text("""CREATE TRIGGER trg_run_status_audit AFTER UPDATE ON simulation_runs
                FOR EACH ROW BEGIN IF OLD.status <> NEW.status THEN
                INSERT INTO audit_logs(id,action,entity_id,created_at,detail) VALUES
                (UUID(),'run_status',NEW.id,UTC_TIMESTAMP(),JSON_OBJECT('before',OLD.status,'after',NEW.status));
                END IF; END"""))
        install_trade_guards(conn)
    with sessionmaker(engine).begin() as session:
        for key, name in [('community','Community households'),('solar_coop','Solar cooperative'),('critical','Critical facility')]:
            if not session.scalar(select(Participant).where(Participant.key == key)):
                session.add(Participant(key=key, name=name))
        if recover_runs:
            for run in session.scalars(select(SimulationRun).where(SimulationRun.status.in_(['running','queued','paused']))):
                run.status, run.error, run.finished_at = 'interrupted', 'Application restarted before run completion; start a new run.', utcnow()


def asdict(record):
    from datetime import datetime
    return {column.name: (value.isoformat()+'Z' if isinstance(value := getattr(record, column.name), datetime) else value)
            for column in record.__table__.columns}
