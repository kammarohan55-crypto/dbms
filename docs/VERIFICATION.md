# End-to-end verification and limits

Latest verification: **27 October 2026, Asia/Kolkata**. See [test results](TEST_RESULTS.md), [UI/guide integration evidence](polish_verification.json), [latest outage comparison](latest_evaluation.json), [initial runtime evidence](runtime_verification.json), and [earlier continuation evidence](continuation_verification.json).

## 27 October interface and learning-guide verification

The complete suite passed **141 tests, none failed or skipped**, including the actual local MySQL repetitions. JavaScript syntax checks passed for both interfaces. New tests cover conflicting-input rollback, independent meter identities, generated-label rejection, live provenance, bounded streaming/deadlines and public learning routes when the operational database is unavailable.

The redesigned dashboard was inspected at desktop width 1,265 px and mobile width 394 px with no page-wide horizontal overflow. Browser checks exercised navigation, a saved-state scrubber/playback, searchable paginated observation tables, forecast evidence, microgrid operations, agent/safety filters, actual market rows, comparisons and the database lab.

The browser created a new **12-step high-demand pair**. Both policies completed with identical input manifests. The GRIDMIND export contained **12 states, 72 decisions, 12 safety checks, 12 linked observations and 12 settled trades**; its state CSV had 12 rows. Both policies had saved final metrics. The real database lab returned 14 tables and live index plans/timings. Exact identifiers and measured results are retained in [polish_verification.json](polish_verification.json).

The separate `/learn/` interface and downloadable notes were served successfully. Browser checks verified a finite architecture sequence, transaction COMMIT and injected ROLLBACK, identical/conflicting retries, unsafe battery rejection, charging calculations, clickable schema entities, finite index lookup, event-key construction and partial-failure sequencing. The guide also fit the 394 px preview, with a working mobile chapter menu and reduced-motion control. These are labelled teaching illustrations and do not execute SQL or change saved runs. Direct `file://` opening was not browser-verified: the preview browser blocks that protocol. The guide's assets are relative and its code uses no application APIs; the HTTP version was exercised in the browser.

The following earlier experiment details remain dated evidence; they were not all rerun during the interface iteration.

## What actually ran

* Private MySQL **8.4.13** was downloaded from Oracle's official CDN, checksum-verified, initialized and restarted successfully on loopback port 3307. No system service or Docker was introduced.
* Schema creation, foreign keys, CHECK constraints, the analytical view and run-status audit trigger worked on that server. Real rollback and idempotency tests passed.
* **3,894** authentic Karnataka daily supplied-energy observations were imported. Reimport inserted **0** rows and reported **3,894** duplicates. The immutable raw source and checksum evidence are retained under `data/raw/`.
* A real XGBoost model was trained, registered, serialized, loaded and used to generate forecasts. Daily GWh targets remain distinct from virtual kW simulation variables.
* A **48-step high-demand baseline/GRIDMIND pair** completed, with market orders/trades, event histories and final metrics persisted. The run manifest identifies exactly the same input observation IDs, seed, initial state and scenario for both policies.
* Browser QA loaded Overview, Forecasts, Comparison and Database lab and inspected the rendered layout. The New simulation form started a further **8-step normal pair** through the actual local FastAPI/MySQL application; both runs completed and appeared in the dashboard.
* The continuation suite has **128 passing tests, none skipped**, including real MySQL repetitions. It adds migration/data-preservation checks, direct-SQL trade guards, candidate forecasting and idempotent prediction, exports, and cooperative run-control races. The single Starlette dependency deprecation warning is recorded rather than suppressed.

## Verified continuation features

The populated MySQL database was upgraded to `002_normalized_integrity`: 14 tables and one analytical view include metric definitions and migration history, results reference canonical units, numerical fields use DOUBLE, and scoped foreign keys plus INSERT/UPDATE guards protect market records. All 3,894 observed rows remain. Checksum-backed recovery restored 3,021 float32-rounded readings from their original raw source; repeat import produced no new records.

A v2 forecast artifact was trained and registered. Validation selects among absolute XGBoost, residual XGBoost and seasonal persistence; all final-test results and coverage are exposed without reselecting from the final test. The selected model still underperforms persistence on this dataset's final test, as documented in [FORECASTING.md](FORECASTING.md). Repeating the same 14-step prediction request returned identical database IDs and values.

The real dashboard started a deliberately slow simulation, paused it, displayed Resume, resumed it, and cancelled it. SQL confirmed cancelled status with no final metrics. Separate tests verify pause acknowledgment after an in-flight step, preserved event identities on resume, prompt cancellation/shutdown during slow replay, bounded admission, and restart recovery for paused runs.

The new 48-step outage pair completed with the current model and normalized database. Its [run JSON](samples/outage-run.json), [state CSV](samples/outage-states.csv), and [comparison CSV](samples/outage-comparison.csv) are saved for inspection. API verification confirmed 48 linked observed inputs, 48 states, 48 safety checks and 288 agent decisions, plus actual settled trades. Both policies served essentially the same total demand in this outage case; GRIDMIND's deliberate flexibility reduces involuntary unmet demand without creating additional served energy. This scenario does not establish an across-the-board policy improvement.

## Preserved initial measured example

Comparison `e387564c-cebe-48aa-ab08-3766e375fbf4`:

| Metric | Baseline | GRIDMIND |
|---|---:|---:|
| Unmet demand, kWh | 231.949 | 64.3202 |
| Intentional flexible curtailment, kWh | 0 | 108.030 |
| Served demand, kWh | 2,798.25 | 2,857.85 |
| Critical energy served, kWh | 757.550 | 757.550 |
| Procurement cost, INR | 12,882.9 | 13,002.7 |
| P2P settled allocation, kWh | 0 | 1,217.78 |
| Safety rejections | 0 | 0 |

GRIDMIND reduced involuntary unmet demand but also curtailed flexible demand and incurred slightly greater procurement cost. Curtailment must be read alongside unmet demand: it is not energy served. Internal P2P transfers do not create extra energy and are excluded from community procurement cost. The largest per-step conservation residual was less than `3e-14 kW` in this pair. These preserved initial metrics were saved before the DOUBLE-precision upgrade; their historical rounding remains visible. New results retain double precision.

The real-data forecasting experiment measured XGBoost test MAE **22.816 GWh** and RMSE **32.109 GWh**, versus weekly persistence MAE **16.618 GWh** and RMSE **22.278 GWh**. XGBoost **did not outperform** that baseline. Empirical held-out interval coverage was **57.0%** for a nominal 90% calibration target, so intervals must not be presented as guaranteed bounds. Both the dashboard and model registry expose these results.

The saved index microbenchmark compared equivalent warm-connection SQL queries over 3,894 observations: median indexed execution **0.752 ms**, index-ignored execution **1.816 ms**, five repetitions. EXPLAIN confirmed range access versus a table scan. These small local timings are evidence of the implemented query paths, not a production speedup guarantee.

## Explicit limits

* **AWS was not connected.** The local journal implements operational event storage now. DynamoDB writes, idempotency and bounded retries were tested with botocore stubs; deployed table permissions and real service behaviour remain for the later AWS step.
* No measured live energy provider was supplied. A configured JSON feed adapter supports live-context imports; its HTTP failure handling is tested with mocked responses. Historical replay is the verified real-data path.
* The regional input is daily supplied electricity, not exact unconstrained demand, household consumption, renewable output or a failure label. Solar/wind and assets are explicitly modelled. Renewable forecasting requires actual target-labelled CSV data and stays disabled for this dataset.
* London Smart Meter parsing supports bounded user-supplied subsets with preserved household identities. The large full archive was investigated, not downloaded. CEA/NPP report formats were investigated, not falsely advertised as a functioning live energy API.
* Hopfield/RBM experiments were not retained: this implementation does not claim validated benefit or invent anomaly/failure targets to justify them.
* SQL market transactions are atomic; MySQL plus the operational store is not a distributed transaction. A partially persisted run becomes failed/interrupted and remains inspectable. Automatic crash resumption is not claimed.
* Six-agent simulation and policy comparisons establish local numerical behaviour only. They do not establish physical Karnataka grid control or equipment-failure prediction.

Use `scripts/start.ps1` to start the delivered local application and `scripts/stop.ps1` to stop only its owned processes. Source, raw data, model artifacts, SQL records and journals remain available between restarts.
