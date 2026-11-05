# GRIDMIND

A local DBMS project: real historical Karnataka observations → XGBoost forecasting → six cooperating agents → independent safety validation → virtual microgrid → transactional P2P settlement → paired evaluation.

**Stack:** Python 3.11+, FastAPI, MySQL 8.0.16+, SQLAlchemy, XGBoost, pandas, NumPy, scikit-learn; a dependency-free responsive dashboard. AWS is deferred: operational events use a clearly labelled local journal. A DynamoDB adapter and table setup script are supplied for later connection. No Docker or other cloud service is needed.

## Start on this Windows machine

From this project directory in PowerShell:

```powershell
.\scripts\start.ps1
```

Open [the dashboard](http://127.0.0.1:8000), [Learn GRIDMIND](http://127.0.0.1:8000/learn/), or [interactive API documentation](http://127.0.0.1:8000/docs). Stop owned local processes with `.\scripts\stop.ps1`.

The separate [animated learning guide](frontend/learn/index.html) also opens directly from disk without starting the application. It explains the working system, architecture, energy balance, SQL relationships, normalization, transactions, indexes, and event keys with step-by-step controls. Read the accompanying [detailed learning notes](docs/LEARNING_GUIDE.md) for worked examples and revision questions. Educational animations use labelled examples; the operational dashboard reads saved simulation results from the backend.

For a fresh checkout, run `.\scripts\start.ps1 -Setup` once. This creates `.venv`, installs dependencies, downloads the official MySQL ZIP with checksum verification, and provisions a project-private MySQL server on loopback port **3307**. It creates random passwords in ignored, access-restricted local files. It does not install a Windows service. The initial download is about 268 MB. Python 3.11 is preferred on this machine. If PowerShell blocks scripts, run `powershell -ExecutionPolicy Bypass -File .\scripts\start.ps1 -Setup` for that process.

Use an existing MySQL server instead by copying `.env.example` to `.env`, creating a dedicated schema/user and configuring `DATABASE_URL`. Do not run the private-MySQL setup over unrelated settings. For non-Windows hosts, use your installed MySQL and run `python -m uvicorn gridmind.main:app --app-dir backend --host 127.0.0.1 --port 8000`. An explicit SQLite URL is supported for isolated development/tests, and the dashboard identifies that backend; it is not evidence of a MySQL demonstration.

## Reproduce the complete demonstration

```powershell
$env:PYTHONPATH = 'backend'
.\.venv\Scripts\python.exe -m gridmind.cli init-db
.\.venv\Scripts\python.exe -m gridmind.cli ingest
.\.venv\Scripts\python.exe -m gridmind.cli train
.\.venv\Scripts\python.exe -m gridmind.cli demo --scenario high_demand --steps 48
.\.venv\Scripts\python.exe -m pytest -q
```

The dashboard exposes dataset/region/series selection, training and model selection, prediction, pause/resume/cancel, asset editing, event histories, market queries, comparisons and JSON/CSV downloads. The CLI writes the latest measured comparison to `docs/latest_evaluation.json`. Versioned recreation scripts are in `sql/`; run 001, 002 and 004 on an empty schema. Existing schema upgrades preserve records and run explicitly with `.\.venv\Scripts\python.exe scripts\migrate.py`; startup reports pending upgrades without silently changing existing columns. See [migration details](docs/MIGRATIONS.md).

### Data and forecasting

The import downloads/caches a real, checksum-verified [Zenodo dataset](https://zenodo.org/records/14983362) (Hunt and Bloomfield, CC BY 4.0). Its Karnataka subset provides **3,894 usable daily supplied-energy observations**, from 2013-01-01 to 2024-04-28, after collapsing identical duplicates and excluding conflicting dates. Daily supplied electricity is a demand proxy; it is not unmet demand or a household meter measurement. Original MU is retained in provenance and represented as GWh. Dates remain historical.

CSV import accepts `timestamp,value,unit,metric` with optional `region` and `series_key` for independent meters, or a bounded original London Smart Meter CSV. Uploads are at most 10 MiB / 20,000 rows, retain raw evidence, reject invalid readings, flag upper-tail outliers, abort conflicting duplicate imports, and preserve household geography. Explicit predicted/simulated input labels cannot become observations. London readings are UK measurements and are never identified as Karnataka households.

Forecasting uses strictly shifted history/calendar features and chronological 70/15/15 splits. Absolute XGBoost, residual XGBoost and seasonal persistence compete using validation MAE; the chosen method is frozen before final-test evaluation. A validation win does not imply a test win. Saved native JSON artifacts include checksums, source identifiers and held-out predictions; existing v1 artifacts still load. Standalone prediction requests are idempotent for the same model, input history and target. Replay uses held-out predictions after training and calibration cutoffs. Solar/wind forecasting requires authentic target labels; it stays disabled for the supplied-energy dataset. Recursive future intervals are explicitly heuristic. See [forecasting evidence](docs/FORECASTING.md) and [data documentation](docs/DATA_SOURCES.md).

### Simulation and comparisons

The default model uses a 240 kWh battery, explicit charge efficiency, configurable limits, critical and flexible demand, modelled solar/wind profiles and INR/kWh tariffs. Regional readings are scaled to virtual loads; all microgrid asset values are **modelled**, not measured. One replay observation supplies context for each virtual step; the configurable virtual step duration does not change the source's daily sampling frequency. Six scenarios include high demand, low renewables, outages and renewable surplus.

Baseline and GRIDMIND receive identical observation IDs, initial conditions, seed and capacities. The baseline is greedy renewable → battery → grid dispatch, with no flexibility or P2P coordination. GRIDMIND has six agent classes and a separate deterministic safety layer. A failure invokes a separately validated fallback. The P2P auction allocates existing local renewable energy; it cannot create physical energy. Both order updates and matched trades commit in one SQL transaction. Community procurement cost excludes internal transfer payments to avoid double counting.

See [simulation assumptions and metric definitions](docs/SIMULATION.md), [ER/normalization/architecture/DFDs](docs/DBMS.md), [API and configuration](docs/API.md), [export formats](docs/EXPORTS.md), [completion matrix](docs/IMPLEMENTATION_STATUS.md), and [verified results and limitations](docs/VERIFICATION.md).

### Connect AWS later

Keep `EVENT_BACKEND=local` until you provide credentials. After configuring your normal AWS profile/region, run `python scripts/setup_dynamodb.py --create`, then set `EVENT_BACKEND=dynamodb` and restart. The adapter uses `GridMindSimulationEvents`, `PK=RUN#<uuid>`, and ordered `SK=STEP#00000001#STATE#<stable-id>`. No AWS requests are made in local mode. Existing local journal files remain local; changing backends does not migrate past history automatically. AWS integration must be verified against your account later.

Set `API_KEY` to require `X-API-Key` on data endpoints; configure the same value in dashboard Connection settings. The app binds to loopback by default. Database passwords, AWS credentials, API tokens, raw uploads and runtime journals stay outside source control.
