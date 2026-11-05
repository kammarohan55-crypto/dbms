# API and configuration

FastAPI exposes the authoritative interactive specification at `/docs` and JSON at `/openapi.json`. The browser accesses only the API, never MySQL or DynamoDB directly.

The separate teaching interface is at `/learn/`. Its static pages and `/learning-notes` Markdown download remain accessible when the operational database is unavailable or an API key is configured. Its animations are labelled educational examples and do not execute database writes. `frontend/learn/index.html` can also open directly from disk using its relative CSS and JavaScript files.

| Workflow | Endpoint |
|---|---|
| Readiness and backend identity | `GET /api/health` |
| Source status / real public import | `GET /api/sources`, `POST /api/ingestion/karnataka` |
| Dataset/region/metric/household identities | `GET /api/series` |
| Bounded raw CSV import | `POST /api/ingestion/csv` multipart file, source_name, region_name, timezone |
| Observed history | `GET /api/observations?source_id=...&metric=...&limit=300&offset=0` |
| Asset CRUD | `GET/POST /api/assets`, `PUT/DELETE /api/assets/{id}` |
| Model registry / training | `GET /api/models`, `POST /api/forecasts/train` with `{ "target": "demand" }` |
| Predictions | `POST /api/forecasts/generate` with model_id and horizon, `GET /api/forecasts` |
| Scenario defaults | `GET /api/scenarios` |
| Start equivalent policy pair | `POST /api/runs` with scenario, compare=true, mode=historical |
| Run status / controls | `GET /api/runs`, `GET /api/runs/{id}`, `POST /api/runs/{id}/pause`, `/resume`, `/cancel` |
| Event/state/safety retrieval | `GET /api/runs/{id}/events?event_type=STATE&step=0&limit=1000&after=...` |
| Relational market and final metrics | `GET /api/runs/{id}/market`, `GET /api/runs/{id}/metrics` |
| Measured comparison | `GET /api/comparisons/{comparison_id}` |
| Saved JSON / state CSV / comparison CSV | `GET /api/runs/{id}/export?format=json`, `?format=csv`, `GET /api/comparisons/{id}/export` |
| Safe fixed SQL examples / audit | `GET /api/db/demo`, `GET /api/audit` |

Event pagination uses the last returned SK as `after`; the filter is optional. DynamoDB uses partition-key queries and a step sort-key prefix, with no Scan or unjustified GSI. Categories over an entire run are filtered after the key query with continued pagination. SQL validates a run reference before exposing operational events.

## Run request

```json
{"scenario":{"name":"high_demand","steps":48,"step_hours":0.5,"seed":42,"replay_speed":0},"compare":true,"mode":"historical"}
```

The response is HTTP 202 with comparison_id and run records. Work continues in a bounded local worker pool. Poll status or use the dashboard. Add `source_id`, `region_id`, `metric`, `series_key` and optional `model_id` to select one observed series and a compatible registered model. Both training and run requests accept these selectors. Ambiguous input is rejected rather than choosing another household or target silently. The dashboard populates them from `/api/series`. `replay_speed=0` runs as fast as persistence allows; positive values are steps per second. Asset capacities saved in SQL supply defaults for new runs when corresponding scenario parameters are omitted. Household, industrial, critical and EV loads supply load/priority defaults in addition to battery/solar/wind capacities. Explicit scenario parameters take precedence. Existing runs retain their immutable config snapshot.

Run states: queued → running ↔ paused → completed; dependency errors → failed; user cancellation → cancelled; application restart/shutdown of pending or paused work → interrupted. Pause acknowledgment waits for an in-flight step to finish persisting, then no next step executes until resume. Replay pacing is interruptible, including very slow configured speeds. Cancellation cannot race with successful final metrics. Two workers and a maximum four owned/persisted active jobs bound the queue; paused workers keep their slot. Completed steps advance only after both market settlement and event writes succeed. Partial results remain queryable. No cross-database atomicity or automatic crash resumption is claimed. Start a new run after a failed/interrupted run; deterministic event/trade keys make individual write retries safe.

## Optional live-context adapter

`LIVE_CONTEXT_URL` can point to a measured-energy JSON feed. Optional `LIVE_CONTEXT_TOKEN` is sent as a Bearer header, never logged or stored in SQL. Configure provider name, region and timezone through `LIVE_CONTEXT_NAME`, `LIVE_CONTEXT_REGION`, and `LIVE_CONTEXT_TIMEZONE`.

Accepted response:

```json
{"observations":[{"timestamp":"2026-10-21T10:00:00+05:30","value":42.1,"unit":"kW","metric":"demand_kw","region":"Campus","value_kind":"observed"}]}
```

`POST /api/live-context/refresh` imports authentic readings with raw provenance. `mode=live-context` refreshes this provider before creating a simulation. At least two compatible readings are needed. No provider is configured by default because the workspace supplied no live integration. HTTP failures, expired credentials, malformed values and unsupported responses leave prior observations intact and report unavailability. They never fabricate live measurements or silently start a historical run. Select historical replay explicitly when a provider is unavailable.

## Configuration

`.env.example` lists options. `DATABASE_URL` is required for MySQL; `EVENT_BACKEND=local` requires no AWS. `EVENT_DIRECTORY`, `RAW_DIRECTORY` and `MODEL_DIRECTORY` may be absolute or project-relative. An optional `API_KEY` protects `/api/*` except public health. Inputs use bounded Pydantic schemas; writes use transactions and integrity errors return HTTP 409. Invalid data returns 422; unavailable SQL returns 503. Fixed DB lab queries do not accept arbitrary SQL. All persisted timestamps are UTC and original local daily dates remain in observation provenance.

For a fresh private MySQL, the project schema user has schema-scoped DDL needed to initialize the schema and audit trigger, plus SELECT/INSERT/UPDATE/DELETE. For a maintained deployment, initialize using a schema administrator and give a separate runtime user only required DML, SHOW VIEW and event-table AWS permissions. Do not expose the local demonstration publicly without deployment access controls.
