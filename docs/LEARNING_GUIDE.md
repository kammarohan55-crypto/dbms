# Inside GRIDMIND: working, system design and DBMS concepts

This is the companion to the separate interactive guide at **http://127.0.0.1:8000/learn/** after starting the application. Open `frontend/learn/index.html` directly to read the same guide offline. It uses only its adjacent CSS/JavaScript files, native SVG diagrams and finite, user-controlled animations. It never calls application APIs or modifies your data.

The guide has 12 chapters. Use the chapter menu or Previous/Next controls; explored chapters are remembered in your browser. Animations start paused and provide Play/Pause, previous/next step and Restart. Reduce motion disables decorative motion. It respects your operating-system motion preference on first use. Download these notes from `/learning-notes`.

The description below follows the actual Python modules, SQL models and saved project reports. Teaching examples are explicitly illustrative. Saved metrics are historical evidence, not a query of the currently selected model or latest dashboard run.

## 1. The project in plain language

Imagine a community with homes, businesses, a critical facility, solar/wind generation, a battery and a grid connection. Demand changes and supply is limited. A decision must choose how much power to discharge, import, charge or export, which noncritical flexible load to curtail, and how to allocate local renewable supply.

GRIDMIND explores those choices in a **virtual microgrid**. It imports authentic historical readings, fits a forecasting experiment, runs two numerical policies with matching assumptions, validates physical constraints, stores the results and explains them through a dashboard.

Three meanings must stay separate:

| Kind | Project example | Interpretation |
|---|---|---|
| Observed | Karnataka daily supplied electricity, GWh | A traceable historical measurement from a source |
| Predicted | Next regional supplied-energy estimate, GWh | A model output with measurable error |
| Modelled | Virtual community load, solar/wind power and battery state | An explicit simulation assumption/result |

Authentic regional inputs do not turn modelled renewable output into measurements. GRIDMIND does not actuate real equipment, estimate network voltage/frequency, simulate AC power flow, or predict equipment failure.

## 2. How to demonstrate the project

From the project folder in PowerShell:

```powershell
.\scripts\start.ps1
```

Open the dashboard at `http://127.0.0.1:8000/` and the separate learning guide at `http://127.0.0.1:8000/learn/`. Use the port printed by the launcher if you configured a different one. Stop the owned local application processes with `scripts/stop.ps1` when finished.

A useful demonstration order:

1. Inspect the data source, original unit, observed period and quality report. Explain the raw-file checksum.
2. Inspect the registered forecasting model, candidate validation results, final-test error, interval coverage and persistence baseline.
3. Create a paired replay with matching inputs, scenario, initial battery and seed.
4. Follow one step from its input observation through the six decisions, safety record and resulting state.
5. Inspect a settled trade, its two order references and their participants.
6. In Database lab, inspect table counts, provenance joins, the comparison view and the real query plans/timings.
7. Compare served demand, flexible curtailment, unmet demand, critical service and external operating cost together. Download JSON/CSV evidence.
8. Start a slow replay to demonstrate pause/resume/cancel. Explain why an unfinished run has partial history but no final metrics.

The learning page's transaction, battery and index interactions are illustrations, not writes to the application. Use the dashboard and SQL records to demonstrate the real backend.

## 3. System architecture and ownership

The layers are:

1. **Presentation:** the browser dashboard renders inputs, commands and results. It never directly edits SQL files or event journals.
2. **API:** FastAPI validates request structure and resolves selected datasets/models.
3. **Application services:** ingestion, forecasting and the background run service implement workflows.
4. **Numerical core:** the pure simulation module computes modelled dispatch without database or network access.
5. **Persistence:** MySQL, the operational event store, and local evidence/model files have different ownership.

```text
Historical CSV → validation → original file/checksum + SQL observations
                                             ↓
                          chronological forecasting → model artifact + registry
                                             ↓
                         replay context → specialist agents → coordinator
                                             ↓
                           independent safety → approved virtual dispatch
                                             ↓
                  SQL market transaction → operational events → SQL progress
                                             ↓
                          completed metrics + status → dashboard/export
```

| Store | Information it owns | Reason |
|---|---|---|
| MySQL | Sources, regions, observations, assets, models, forecasts, runs, participants, orders, trades, metrics, audit and schema history | Relationships, integrity, joins and atomic settlement |
| Local event journal now; optional DynamoDB adapter later | Modelled states, agent decisions and safety checks | Ordered run/step retrieval with flexible versioned payloads |
| Local files | Original source CSVs, provenance/checksums and serialized models | Source traceability and reloadable experiment artifacts |

**AWS remains deferred.** The working local journal is not a DynamoDB emulator or evidence of a deployed AWS service. SQL and the event store do not participate in one distributed atomic transaction.

Data-flow levels help explain the same architecture at different resolutions:

- **Level 0:** operator/data source → GRIDMIND → inspectable predictions and simulations.
- **Level 1:** import, train, replay, settle, store and query, with the owning stores shown.
- **Level 2:** build step context → collect proposals → coordinate → validate → execute or validate fallback → settle → write events → advance progress.

Implementation: `backend/gridmind/main.py`, `runner.py`, `simulation.py`, `database.py`, `events.py`.

## 4. Data ingestion, quality and provenance

The integrated public dataset is Hunt and Bloomfield's Zenodo record `10.5281/zenodo.14983362`, distributed under CC BY 4.0. Attribution belongs to Kieran Hunt and Hannah Bloomfield. GRIDMIND keeps the original `corrected_daily_energy_met_MU.csv` and selects Karnataka's daily supplied-electricity column.

The actual downloaded file covers 2013-01-01 through 2024-04-28, despite a narrower period in the dataset title. MU equals GWh. This is **energy supplied/met**, a demand-related context signal. It is not measured unrestricted demand, household consumption or live grid telemetry.

The file contains 3,955 Karnataka rows. The parser collapses 49 identical duplicate rows and excludes every version of six conflicting dates, producing **3,894 usable observations**. Raw versions remain available for audit. The public importer checks the pinned published MD5 and records SHA-256, source URL, acquisition time, original units, date bounds and quality counts. A cached raw source retains its provenance rather than appearing newly measured.

The identity of a reading is:

```text
(source_id, region_id, series_key, metric, observed_at)
```

The SQL unique constraint enforces this natural identity independently of the row UUID. `series_key` preserves distinct meters/households. Exact repeated readings are duplicates; a different value or unit at an existing identity is a conflict. The import transaction must not silently overwrite it. Canonical imports also preserve the distinction between observed, predicted and simulated records.

Invalid/nonfinite/negative readings and invalid timestamps are rejected/reported. Missing readings are gaps rather than zeros. Outliers can be flagged for review while retaining their original numeric values. Forecast feature construction can forward-fill past history but cannot create measured training/evaluation targets.

Time interpretation is deliberate. Karnataka daily values use the local date in Asia/Kolkata; stored timestamps are handled consistently. London data preserves household identity and Europe/London semantics. Ambiguous autumn clock times and nonexistent spring times require offsets/review, rather than silently guessing a DST interpretation. Half-hour household kWh is energy over half an hour, not a kW reading.

The canonical format supports `timestamp,value,unit,metric,region` and optional series identity. Bounded uploads report row errors and truncation. Original upload bytes and their SHA-256 remain available. London parsing supports manageable household/date subsets; the complete archive is not downloaded by this project. A configured JSON live-context adapter is separate from the verified historical-data path, and no real measured live provider has been supplied.

Trace a result backward:

```text
state.input_observation_id → energy_observations.id
→ source/region/series + original timestamp/value/unit
→ raw-file path + checksum → original source bytes
```

Implementation: `public_data.py`, `ingestion.py`; detail: `docs/DATA_SOURCES.md`.

## 5. Forecasting and an honest evaluation

Forecasts retain the observed target's unit. Karnataka forecasts predict `supplied_energy_gwh`, not invented household demand, weather, renewable output or equipment-failure labels.

Features use strictly earlier lags/rolling statistics and known calendar variables. After feature construction, labelled samples split chronologically: **70% train, 15% validation, 15% final test**. The saved Karnataka experiment contains 2,720 training, 583 validation and 584 test labels.

Three fixed candidates compete:

| Candidate | Mechanism |
|---|---|
| Seasonal persistence | Previous week's value for daily data; previous day for sufficiently long subdaily data; previous reading for very short series |
| Absolute XGBoost | Predict the energy level from historical/calendar features |
| Residual XGBoost | Predict a correction to persistence, then add it to that seasonal value |

The lowest **validation MAE** wins; ties favour persistence. Models, selection and interval radii are frozen before final scoring. If persistence wins validation, future predictions really use persistence.

```text
MAE  = mean(abs(actual - predicted))
RMSE = sqrt(mean((actual - predicted)^2))
```

MAE expresses average miss in the original unit. RMSE gives large errors more influence. A nominal 90% interval uses validation absolute-residual quantiles with finite-sample adjustment. Validation is reused for selection and calibration; time dependence, selection and changing conditions mean the target coverage is not guaranteed.

Saved local QA evidence from `docs/FORECASTING.md` and `.runtime/forecast-qa/report.json`:

| Candidate | Validation MAE, GWh | Final MAE, GWh | Final RMSE, GWh | Empirical interval coverage |
|---|---:|---:|---:|---:|
| Absolute XGBoost, selected | 8.202 | 22.816 | 32.109 | 57.02% |
| Residual XGBoost | 9.002 | 13.546 | 17.683 | 76.88% |
| Seasonal persistence | 16.126 | 16.618 | 22.278 | 89.04% |

The selected candidate **underperforms persistence on the final test**. Choosing residual XGBoost afterward because it has the best final score would leak evaluation information into selection. A better model-selection experiment needs a fresh evaluation period. These historical values do not silently replace a currently registered application model.

Final-test scoring is rolling-origin **one-step** evaluation: earlier actual readings may inform each next prediction. Future multi-step inference is recursive: previous predictions become future inputs. One-step results do not establish multi-step performance. Square-root-of-horizon interval widening is explicitly heuristic.

Registry metadata stores source/region/metric/unit/series, cutoff, metrics and artifact identity. Replay only uses compatible saved held-out predictions strictly after calibration and verifies their actuals are unchanged. Repeated future requests use deterministic model/input-fingerprint/target identities and serialized writes, so an identical request reuses rows. A new actual reading changes the forecast origin.

Implementation: `forecasting.py`, `forecast_service.py`; detail: `docs/FORECASTING.md`.

## 6. The six agents and safety

| Numerical class | Responsibility |
|---|---|
| `RiskForecastAgent` | Forecast-informed energy stress and reserve context |
| `EnergyResourceAgent` | Battery charge/discharge proposals |
| `DemandManagementAgent` | Bounded flexible noncritical curtailment |
| `MarketTradingAgent` | Renewable buying/selling allocation proposals |
| `CriticalFacilityAgent` | Priority critical demand |
| `CoordinatorAgent` | Resolve proposals into a dispatch and orders |

No LLM is in the control path. The five specialists propose; the coordinator assembles; the independent `SafetyLayer` approves or rejects.

Safety verifies finite/nonnegative numbers; battery energy/power; exclusive charge/discharge and import/export modes; renewable-only charging; grid limits; flexibility bounds; critical priority; market energy budgets; and bus balance.

If an agent fails or the original plan is unsafe, rejection reasons remain in the record. A deterministic fallback disables storage movement, flexible curtailment and market orders. It uses available renewables/bounded imports, serves critical demand first, and records shortage. It must pass independent validation. A failed fallback causes an error, not a fictitious successful step.

An accepted fallback preserves `approved=false` for the original plan, plus `executed_fallback=true` and `fallback_approved=true`. Counting a rejection does not imply the unsafe plan executed.

Implementation: `simulation.py`: agent classes, `SafetyLayer`, `generate_steps`.

## 7. Physical equations and scenario assumptions

All power flows use positive kW magnitudes:

```text
solar + wind + discharge + import + unmet
    = demand - curtailed + charge + export + spill

served = demand - curtailed - unmet
```

Unmet is a shortage accounting term, not supply. Intentional flexible curtailment is separate from involuntary shortage. Critical/flexible fractions cannot overlap; available supply serves critical demand first. P2P transactions reallocate existing renewable supply and do not appear as extra generation in the equation.

Battery energy uses kWh, with step duration `dt` in hours and one-way efficiency `eta`:

```text
E_next = E_now + charge_kw * eta * dt - discharge_kw / eta * dt

available_discharge_kw = min(converter_kw, (E_now - E_min) * eta / dt)
available_charge_kw    = min(converter_kw, (E_max - E_now) / (eta * dt))
```

The bounds are also clamped nonnegative. Default assumptions include 240 kWh battery capacity, 60 kW converter, 60% initial SoC, 15% minimum, 95% maximum and `eta=0.95`. Round-trip efficiency is `eta²=0.9025`, not 0.95.

**Worked illustrative step:** renewables 60 kW + grid import 30 kW + battery discharge 10 kW = served demand 100 kW. Over 0.5 hours, this supplies 50 kWh: 30 renewable, 15 grid, 5 discharged at the bus. Battery energy falls by `5/0.95=5.263 kWh`. With 144 kWh initially, it ends at approximately 138.737 kWh.

**Illustrative unsafe proposal:** requesting 80 kW discharge from the default 60 kW converter fails, even if enough stored energy exists. Safety validates power and energy separately.

The regional series is explicitly scaled:

```text
regional_scale = base_load_kw / mean(input_values)
virtual_hour = (step * step_hours) mod 24
load_profile = 0.82
             + 0.28 * exp(-((virtual_hour - 19)/3)^2)
             + 0.14 * exp(-((virtual_hour - 8)/2.5)^2)
modelled_demand = raw_value * regional_scale * load_profile * scenario_factor
```

Solar is a daylight profile and wind a bounded noisy sinusoid, using a local seeded random generator and configured capacities. These are modelled profiles, not observed renewable data or forecast training labels. A virtual half-hour step does not upsample daily measured GWh into newly measured half-hour telemetry. Original timestamps remain; repeated short inputs record `replay_cycle`. Playback rate changes waiting only, not physical `dt` or source timestamps.

Scenarios: normal, high demand (demand ×1.5), low renewable (availability ×0.2), grid outage (imports unavailable), battery stress (demand ×1.25 and renewable ×0.4), renewable surplus (demand ×0.7). Outage disables imports; configure export capacity to zero for complete external disconnection.

Implementation: `simulation.py`; full assumptions/formulas: `docs/SIMULATION.md`.

## 8. Market settlement, ACID, locks and retries

Internal trading allocates energy that already serves load. It cannot sell more than available modelled renewables or buy more than permitted served load. Sellers are the pooled renewable cooperative (`solar_coop`, including configured wind). Participants have distinct identities and cannot trade with themselves.

Settlement opens a SQL transaction and locks its run row with `SELECT ... FOR UPDATE`. It checks existing run/step orders for an identical replay, validates proposals, inserts orders, matches them, updates both remaining quantities, inserts trades and commits. Exceptions roll back the transaction.

Buy limits sort descending; sell limits ascending. Ties preserve proposal insertion priority. Match quantity is the smaller remaining amount; price is the midpoint of compatible buy/sell limits. Order states become open, partial or filled.

**Illustrative example:** buy 8 kWh at limit 8; sell 10 kWh at 4 → trade 8 kWh at 6. Buyer remaining becomes 0; seller remaining 2; internal value is 48. Failure before commit rolls back both new orders and the trade. The interactive guide shows pending rows separately from committed rows.

| ACID property | Meaning | Implemented example |
|---|---|---|
| Atomicity | Related changes commit or roll back together | Both order decrements and the trade belong to the same SQL transaction |
| Consistency | Committed data obeys rules | CHECKs, unique constraints, FKs, trade guards and application energy validation |
| Isolation | Concurrent logical changes are coordinated | MySQL run-row lock serializes that run's settlement |
| Durability | Committed database records persist | SQL ledger survives an application restart |

SQLite compatibility mode does not reproduce MySQL row-level locking. The lock coordinates writers following that locking protocol; it is not a global promise about arbitrary unrelated SQL clients.

**Idempotency** is a separate property. Deterministic IDs, unique constraints and existing-order comparison make identical whole-step retries safe. Different order content for a committed step raises a conflict. Event idempotency similarly accepts identical key/content and rejects conflicting content. Neither mechanism creates a distributed transaction.

Implementation: `market.py`, `models.py`, trade guards defined in `migrations.py` and installed by `database.py`.

## 9. Actual relational schema: 14 tables

Count the SQLAlchemy tables: **14**, not 15. A view is not another base table, and `comparison_id` is not an independent table. The interactive guide allows each table to be selected and inspected.

| Table | Key and important relationships | Purpose |
|---|---|---|
| `data_sources` | PK `id`; unique `name` | Dataset/provider/provenance identity |
| `regions` | PK `id`; unique `name` | Location and timezone |
| `energy_observations` | PK `id`; source/region FKs; unique `(source_id, region_id, series_key, metric, observed_at)` | Actual readings and original units |
| `microgrid_assets` | PK `id`; unique `name` | Virtual capacities and SoC bounds |
| `model_registry` | PK `id`; unique `version`; source/region FKs | Model identity, metrics, metadata and artifact path |
| `simulation_runs` | PK `id`; source FK; nullable model FK | One policy replay, manifest, config, lifecycle and progress |
| `forecasts` | PK `id`; model FK; nullable run FK | Predicted value, target/origin and interval |
| `market_participants` | PK `id`; unique `key`; nullable asset FK | Buyer/seller identity |
| `market_orders` | PK `id`; run/participant FKs; unique run/step/participant/side | Orders, limits and remaining quantities |
| `market_trades` | PK `id`; run FK; two scoped order FKs; unique buy/sell pair | Settled internal allocations |
| `metric_definitions` | PK `name` | Canonical metric unit and description |
| `run_metrics` | Composite PK `(run_id, name)`, both FKs | One final value per run/metric |
| `audit_logs` | PK `id`; polymorphic entity reference is not an FK | Action/entity/time history; no authenticated actor identity is modelled |
| `schema_migrations` | PK `version` | Successful schema upgrade history |

Cardinalities: one source/region can have many observations; one source/region can have many models. A model can inform many runs and forecasts. A run has many orders, trades and metrics. A participant submits many orders. One order can participate in multiple partial trades, but each trade has exactly one buy and one sell order. A metric definition describes values from many runs.

The three nullable relationships are `simulation_runs.model_id`, `forecasts.run_id` and `market_participants.asset_id`. Many children can reference the same optional parent. `comparison_id` groups policy runs without referencing a comparison entity. `audit_logs.entity_id` deliberately varies by action and is not falsely represented as one SQL FK.

The buy and sell references in a trade are **composite** FKs to `(market_orders.id, run_id, step)`. They prevent one trade from connecting unrelated runs/steps. Database guards also check order sides, participant separation, prices and cumulative quantities. A single `order_id` reference would not express this complete scope rule.

Other checks enforce nonnegative observation values; valid asset capacity/types/SoC; valid run policy/status/progress; positive order/trade energy; nonnegative price; and remaining quantity within allocation. The application inserts final metrics and completed status in one transaction; completion-only metrics are a service rule, not a database CHECK. Numerical fields use `Float(53)` / MySQL DOUBLE, which supports simulation precision rather than exact decimal financial arithmetic.

Implementation: `models.py`, `sql/001_schema.sql`, `sql/004_metric_definitions.sql`; detail: `docs/DBMS.md`.

## 10. Normalization: 1NF, 2NF, 3NF and BCNF

Normalization follows functional dependencies, not the presence of an `id` column.

**1NF:** store separate observations, orders and trades as rows, rather than repeating columns such as trade1/trade2/trade3 on a run. JSON configuration/metadata is treated as an opaque provenance document; its nested internals are not claimed to satisfy relational normal forms.

**2NF:** a non-key fact must depend on the entire candidate key. Consider this deliberately poor design:

```text
results(run_id, name, value, unit)
key = (run_id, name)
(run_id, name) → value
name → unit                    -- partial dependency
```

The actual design separates the dependency:

```text
run_metrics(run_id, name, value)       key = (run_id, name)
metric_definitions(name, unit, description)   key = name
```

Now a unit/definition has one canonical location; new definitions can exist before runs; deleting a run result does not delete metric meaning. This avoids update, insert and delete anomalies. Changing canonical units still needs care: it must not reinterpret unchanged historic numeric values.

**3NF:** descriptions live with the fact they describe. Provider metadata belongs with a source, timezone with a region, participant name with a participant and model version details with a model. Referencing rows reach them through FKs instead of repeated copies.

**BCNF:** every nontrivial dependency's determinant must be a superkey (a set containing a candidate key). In `metric_definitions`, `name → unit,description` and name is the key. In `run_metrics`, `(run_id,name) → value` and that complete pair is the key. These relations meet BCNF under the stated dependencies.

Do not claim the entire project is automatically in BCNF. Trades deliberately materialize run/step for scoped access/integrity. Run manifests/configs deliberately freeze JSON snapshots for reproducibility. These are explicit design tradeoffs outside a blanket normal-form claim.

Schema migration `002_normalized_integrity` introduces canonical metric definitions, converts MySQL numeric columns to DOUBLE, adds pause status, strengthens scoped FKs/guards and rebuilds the analytical view. MySQL DDL commits implicitly: phases check their state and can retry. Structural checks and guard installation precede the version record; raw-precision recovery follows separately, so a recorded version alone does not establish that every recovery step finished. Existing rows/IDs must be preserved. Merely changing FLOAT to DOUBLE does not restore rounded digits; restoration requires checksum-backed original evidence and an exact old float32 match.

Implementation: `metric_definitions.py`, `migrations.py`; detail: `docs/MIGRATIONS.md`.

## 11. SQL concepts through actual queries

### Joins and aggregation

```sql
SELECT s.name AS source, r.name AS region, o.metric,
       COUNT(*) AS observations,
       MIN(o.observed_at) AS first_observation,
       MAX(o.observed_at) AS latest_observation
FROM energy_observations AS o
JOIN data_sources AS s ON s.id = o.source_id
JOIN regions AS r ON r.id = o.region_id
GROUP BY s.name, r.name, o.metric;
```

JOIN reconnects the normalized facts. GROUP BY forms source/region/metric groups; aggregate functions summarize each group.

```sql
SELECT t.id, t.run_id, t.step,
       b.name AS buyer, s.name AS seller,
       t.quantity_kwh, t.price
FROM market_trades AS t
JOIN market_orders AS bo ON bo.id = t.buy_order_id
JOIN market_orders AS so ON so.id = t.sell_order_id
JOIN market_participants AS b ON b.id = bo.participant_id
JOIN market_participants AS s ON s.id = so.participant_id;
```

Aliases let the same order and participant tables play two roles, buyer and seller, without duplicating the schema.

### Views and triggers

```sql
SELECT comparison_id, run_id, policy, status, metric, value, unit
FROM v_run_comparison
ORDER BY comparison_id, metric, policy;
```

`v_run_comparison` is a reusable query connecting runs and metric definitions/results. It is not a second results table. Its inner joins omit runs with no stored metrics. The comparison API separately retrieves run metadata so those unfinished runs remain visible; missing final results must not be interpreted as zero.

On MySQL, `trg_run_status_audit` records status changes in `audit_logs`, even when another SQL client issues the update. The unchanged-status case does not create a change event. Application operations record other audit actions. This MySQL trigger is not claimed to exist in SQLite compatibility mode.

Both MySQL and SQLite also have `trg_trade_guard_insert` and
`trg_trade_guard_update`. They validate side/scope/participant/price/allocation
rules when a trade is inserted or updated. They do not automatically settle
direct-SQL orders, maintain their remaining quantities, or retroactively validate
later changes to an order. Application settlement owns the coordinated updates.
There are three installed triggers on MySQL and two on SQLite; no stored
procedure is claimed.

### Indexes and plans

The observation `(region_id, observed_at)` index matches a region equality plus a bounded time range. Column order matters. Other indexes cover metric/time, run status/comparison, forecast target time and market book lookups `(run_id, step, side, limit_price)`. Proposal sorting and matching run in Python, not a SQL matching algorithm using that index. Unique constraints can also provide useful indexes.

An index is like an access directory, not a magical speed guarantee. It costs storage and write work. Inspect **EXPLAIN** to understand the actual chosen plan. The Database lab uses equivalent bounded queries, a warm-up and five timings, reporting the median with plans. It does not drop indexes or manufacture a speedup. An `IGNORE INDEX` hint alone does not prove a full scan.

The guide's colored-tile index lookup is explicitly an analogy. It omits tree traversal and models no database latency. For measured timings, use the current Database lab and its returned plans; small local measurements do not establish production performance.

Implementation: `analytics.py`, `database.py`, `models.py`, `sql/002_analytics.sql`.

## 12. Event keys, NoSQL and failure recovery

The optional DynamoDB table is `GridMindSimulationEvents`. The implemented access pattern is a run history, optionally narrowed to a step:

```text
PK = RUN#<run-UUID>
SK = STEP#<8-digit-step>#<event-category>#<20-character-event-hash>

Example shape:
PK = RUN#123e4567-e89b-42d3-a456-426614174000
SK = STEP#00000010#STATE#<stable-event-hash>
```

```python
store.query(run_id, event_type="STATE", limit=1000)
store.query(run_id, step=10, event_type="AGENT_DECISION", limit=1000)
```

Partition equality finds a run; `begins_with(SK, "STEP#00000010#")` finds a step. Categories filter the run history. Sort-key cursors support bounded ordered results. No secondary index is justified by these implemented access needs. Same-step hash sorting does not encode causal agent order.

Each record carries run/step/category, `schema_version=1`, original observation timestamp and structured payload. A state, decision and safety result need different fields. Document flexibility suits that purpose, while SQL remains appropriate for FK-protected entities and transactions.

The local journal uses a per-run folder, writes JSON to a temporary file, flushes/fsyncs and atomically replaces the destination. Identical key/content writes are idempotent; changed content conflicts. The DynamoDB adapter uses conditional writes, checks an existing item and retries selected transient errors with bounded backoff. It is prepared code, not proof of live AWS operation. There is no SQL FK into an event document; the application checks run existence before exposing events.

The cross-store sequence for a step is:

1. Validate and calculate virtual dispatch.
2. Settle and **commit SQL** market records.
3. Write state, agent decisions and safety-check events.
4. Advance SQL `completed_steps`.
5. After all steps, compute/store final metrics and completed status in SQL.

If step 3 fails after step 2, the committed trades stay committed, event history may be incomplete and progress does not advance for the incomplete step. When SQL remains reachable, the run becomes failed with an error. A SQL outage can prevent that status update and leave an active row until restart recovery marks it interrupted. This is inspectable partial progress, not distributed rollback. Repeated event identity is not automatic crash recovery.

Pause/resume works inside the same process at complete step boundaries and retains the active iterator in memory. Cancellation stops further work and keeps partial evidence, with no final metrics. On application restart, formerly queued/running/paused work is marked **interrupted**; it is not automatically reconstructed from a checkpoint. Start a new run after investigating an interruption.

Implementation: `events.py`, `runner.py`, startup recovery in `database.py`.

## 13. Fair policy comparisons and actual saved results

The baseline uses renewables, greedy battery discharge and then imports; it charges renewable surplus and uses neither flexible curtailment nor P2P orders. GRIDMIND preserves reserve outside peak hours unless imports are constrained, discharges at peaks, can curtail permitted flexible noncritical shortage and submits internal renewable allocations. Both use the same safety boundary.

A paired experiment shares observations, forecasts, seed, initial state and scenario. Renewable noise is drawn in the same order independent of policy. `comparison_id` and input manifests expose this equivalence.

Historical saved outage comparison `0b0e0c7c-9a95-43b9-af6d-2b90dac67a7d`, from `docs/latest_evaluation.json`, rounded:

| Metric | Baseline | GRIDMIND |
|---|---:|---:|
| Gross demand, kWh | 2,171.858 | 2,171.858 |
| Served demand, kWh | 1,316.504 | 1,316.504 |
| Intentional curtailment, kWh | 0.000 | 202.928 |
| Involuntary unmet demand, kWh | 855.354 | 652.426 |
| Critical unmet demand, kWh | 52.609 | 52.609 |
| External import, kWh | 0.000 | 0.000 |
| P2P settled allocation, kWh | 0.000 | 1,069.624 |

The reduced unmet demand is almost exactly the deliberate curtailment, while delivered energy is essentially equal. This case does not prove an improvement in served or critical energy. Read:

```text
gross demand = served demand + deliberate curtailment + involuntary unmet
```

External operating cost is imports multiplied by contemporaneous import tariff minus export revenue. Internal P2P transfers are excluded to avoid double counting community procurement. P2P volume/value are computed from **settled SQL trades**, not unexecuted proposals. Tariffs are illustrative; storage wear, capital/carbon costs and curtailment rebound are not included.

Renewable utilization includes charging and exports and excludes spill; it is not a self-consumption or carbon metric. Battery throughput counts AC-bus charged plus discharged energy. Maximum balance residual is in kW, whereas integrated energies multiply each power by `dt`.

Saved evidence proves that specific local workflow ran under its documented assumptions. It does not establish real-grid suitability, production database performance or an across-the-board winning policy.

## 14. Review questions with answers

**Why both SQL and an operational document store?** SQL protects entity relationships, supports joins and makes market settlement atomic. Run/step documents suit varying operational payloads. The application handles their non-atomic boundary.

**Where is 2NF visible?** `run_metrics(run_id,name,value)` has a composite key; `name → unit` is removed into `metric_definitions`.

**How are cross-run trades prevented?** Both order references include `(order_id,run_id,step)` in composite foreign keys. Price/side/allocation guards add rules beyond referential integrity.

**What rolls back when settlement fails?** The SQL transaction's new orders, quantity updates and trades. A later event failure cannot roll back already committed settlement in another transaction.

**How does idempotency differ from isolation?** Idempotency makes identical retries have no extra effect. Isolation coordinates overlapping concurrent operations. A transaction groups writes; neither alone substitutes for the other.

**Why not select the best final-test candidate?** It would use the evaluation period to tune the model selection. Validation chooses; the final test assesses that frozen choice.

**Does 90% nominal coverage mean a guarantee?** No. The saved selected candidate achieved 57.02% final coverage. Time dependence, selection and change can break nominal expectations.

**Does lower unmet energy prove more service?** No. Deliberate curtailment can move energy from the unmet category without increasing delivered energy. Report served, curtailed, unmet and critical service together.

**Is everything BCNF because it has a UUID?** No. BCNF depends on candidate keys and functional dependencies. Make relation-specific claims and disclose JSON snapshots and scoped trade materialization.

**Can pause survive a process restart?** No automatic checkpoint resume is implemented. Pause/resume is within the same active application session; restart marks active work interrupted.

**Is DynamoDB already connected?** No. Local event persistence works now. Live AWS configuration/table permissions and real service behaviour remain future integration work.

## 15. A compact vocabulary

| Term | Meaning |
|---|---|
| Primary key | Chosen unique row identifier; may be composite |
| Candidate key | Minimal attribute set that can uniquely identify a row |
| Foreign key | Constraint requiring valid referenced rows |
| Functional dependency | A fact such as metric name determining one canonical unit |
| Transaction | Writes committed or rolled back together within one database |
| Lock | Coordination against conflicting concurrent operations |
| Idempotency | Same request/content repeated without an extra effect |
| View | Named query over existing relations |
| Trigger | Database action caused by a qualifying data change |
| Index | Extra access structure with storage and write costs |
| Provenance | Recorded origin/transformation/identity of data |
| SoC | Stored battery energy divided by capacity |
| kW / kWh | Power rate / energy accumulated over time |
| Replay | Historical input driving a virtual experiment |
| Calibration | Estimating interval size from earlier errors |
| Leakage | Revealing evaluation/future information to fitting or selection |

For further implementation detail, read `docs/DBMS.md`, `docs/SIMULATION.md`, `docs/FORECASTING.md`, `docs/DATA_SOURCES.md`, `docs/MIGRATIONS.md`, `docs/API.md` and `docs/VERIFICATION.md`. The actual model definitions and running database remain authoritative if a future change makes these notes stale.
