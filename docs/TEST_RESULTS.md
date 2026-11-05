# Verified test results

Verified locally on **27 October 2026 (Asia/Kolkata)** with Python **3.11.5**,
the installed project dependencies, and a running **MySQL 8.4.13** instance.

**Complete suite: 141 passed, 0 failed, 0 skipped, in 21.00 seconds.**

The machine-readable final report is [test-results.xml](test-results.xml). It
records the full run against the current implementation and actual local MySQL.

One dependency deprecation warning came from Starlette's TestClient/httpx adapter.
No test failure or warning was suppressed. The final run used the real installed
XGBoost library; import placeholders were not used.

| Test file | Passed | Verified behaviour |
| --- | ---: | --- |
| `tests/test_simulation.py` | 40 | Six scenarios under both policies, conservation, battery efficiency/power/SoC bounds, critical priority, physical market allocations, identical paired exogenous traces, invalid inputs, failure of each agent independently, unsafe proposal rejection |
| `tests/test_storage.py` | 16 | Identical relational checks on SQLite and actual MySQL, foreign keys/checks/unique identities, market rollback after injected failure, price-priority matching and retry idempotence, completed-run persistence, partial event failure marked failed, journal conflicts/filtering/pagination, stubbed DynamoDB writes |
| `tests/test_api.py` | 12 | Authentication/readiness, asset CRUD/audit, CSV ingestion and repeated imports, observation provenance, London household separation, conflicting duplicate rollback, timezone/schema-width validation, paired end-to-end local HTTP simulation, events/trades/metrics/analytical SQL, live-feed adapter success/failure, unavailable events causing failed runs |
| `tests/test_forecasting.py` | 18 | Chronological features and splits, real absolute/residual XGBoost and persistence selection on validation only, selected-algorithm inference, v1/v2 artifacts and checksums, daily/half-hour frequency, missing targets excluded, unsupported labels rejected, CSV/DST handling, public-data provenance |
| `tests/test_forecast_service.py` | 8 | Explicit series identity and ambiguity errors, saved model identity, deterministic forecast IDs, repeat/extended-horizon idempotence, changed input history, concurrent writes, bounded held-out replay including one step |
| `tests/test_run_control.py` | 8 | Durable step-boundary pause, resume without repeated events, prompt slow-replay cancellation, final-step cancellation race, bounded active/scheduled jobs, paused shutdown/restart recovery, exact series selection and load-asset defaults |
| `tests/test_exports.py` | 4 | Complete paginated events, manifest-only observations, canonical metric units, incomplete run status, missing input references, sanitized JSON, formula-safe state/comparison CSV |
| `tests/test_integrity_upgrade.py` | 22 | Metric foreign keys, double precision, direct-SQL INSERT/UPDATE scope/side/price/self-trade/allocation guards on SQLite and actual MySQL, cumulative limits, non-finite budgets, conflicting retries, legacy SQLite preservation/idempotence, checksum-backed precision recovery |
| `tests/test_ingestion_reliability.py` | 12 | Entire-import rollback for conflicting readings, independent meter identities, generated labels rejected, original live JSON provenance, credential-safe URLs, provider failures, bounded streaming and total download deadline |
| `tests/test_learning_routes.py` | 1 | Separate guide/assets/Markdown notes available despite database unavailability and API-key protection; database notes route and API documentation remain distinct; traversal rejected |

The MySQL tests created isolated UUID-labelled records and removed only their own
records. They did not drop tables or interrupt other simulation runs. MySQL check
violations were confirmed as server error 3819; PyMySQL reports that error as an
OperationalError rather than an IntegrityError.

The cached Zenodo test verified the imported source's 3,894 retained Karnataka
daily supplied-energy observations, including documented source duplicate and
conflict handling. Model unit tests also use explicit generated test fixtures;
their scores are not claims about real-grid performance.

In the earlier 22 October verification, all fresh-schema SQL scripts were executed successfully in a new,
isolated MySQL schema: 14 tables, the analytical view, status audit trigger,
trade guards, canonical metric seeds and schema version. Only that temporary
verification schema was then removed. Evidence is saved in
[sql_recreation_verification.json](sql_recreation_verification.json).

Real API/browser checks are recorded in
[continuation_verification.json](continuation_verification.json). They confirm
pause/resume/cancel, no final metrics on the cancelled run, an outage run with
48 input observations, 48 states, 288 decisions and 48 safety checks, correct
CSV fields, and identical repeated forecast records.

## AWS distinction

No AWS credentials or AWS account were used. DynamoDB tests use botocore Stubber
with a non-routable local endpoint to verify throttle retry, conditional identical
replay, conflicting-content rejection, and bounded retry exhaustion. The local
journal's query and pagination behaviour was tested directly. A deployed DynamoDB
table, IAM permissions, real service availability and account-level query behaviour
remain unverified until AWS is supplied. HTTP live-feed tests similarly use
explicit mocked provider responses; no configured live measurement provider is
claimed by those tests.

## Reproduce

The default command runs every test and skips the seven MySQL repetitions unless
the optional test URL is set:

```powershell
.venv\Scripts\python.exe -m pytest -q
```

To repeat the complete verified run using the configured local MySQL URL without
printing credentials:

```powershell
$env:PYTHONPATH = 'backend'
.venv\Scripts\python.exe -c "import os; from gridmind.config import settings; os.environ['GRIDMIND_TEST_MYSQL_URL']=settings.database_url; import pytest; raise SystemExit(pytest.main(['-q']))"
```

These results establish the tested local software behaviour. They do not certify
physical-grid operation, production deployment, or live AWS integration.
