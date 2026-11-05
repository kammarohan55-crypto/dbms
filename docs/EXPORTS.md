# Results and provenance exports

Select a simulation in the dashboard to download its run JSON, state CSV, or
comparison CSV. Downloads use the same optional API key as other requests and
come from saved database/event-store records. Nothing is generated in a cloud
account.

| Endpoint | Contents |
| --- | --- |
| `GET /api/runs/{run_id}/export?format=json` | Versioned run metadata/configuration, input manifest, linked observed inputs and source/region provenance, model identity and scores, all state/agent/safety events, market orders/trades/participants, and saved metrics with units. |
| `GET /api/runs/{run_id}/export?format=csv` | One row per persisted state: step, timestamp, input observation ID, value kind, and selected numerical state fields. |
| `GET /api/comparisons/{comparison_id}/export` | Long-form CSV containing each run's policy/status/progress and actual persisted metric values/units. Runs without final metrics have a status row with empty metric cells. |

JSON uses `schema_version: 1`. Only observations listed in that run's
`input_manifest.observation_ids` are included, in manifest order. Other imported
datasets and training observations outside the replay manifest are not copied.
Missing linked records are listed in `missing_observation_ids`; provenance is
never reconstructed by taking the latest available observations. All event pages
are read, so a 336-step run includes 336 states, 336 safety checks and 2,016 agent
decisions when every step completed successfully.

The original regional observation retains its original unit and
`value_kind: observed`. Microgrid state rows retain their `modelled` value kind
and link to the originating observation. Forecast errors remain in their source
unit. CSV does not contain nested decisions or full provenance; use JSON for the
audit trail. CSV text beginning with a spreadsheet formula marker is prefixed
with an apostrophe. Numeric negative values remain numeric.

The exports omit deployment settings, credentials, local raw-data paths and
model artifact locations. They include observation values and run results, so
share them according to the sensitivity of the dataset you imported.

An active, paused, cancelled, interrupted or failed run can be exported. Its
file contains the persisted progress available at download time; final metrics
can be absent. Relational records and events are separate stores, so an active
export is a best-effort snapshot, not a transactionally consistent backup. Pause
or finish a run before downloading when a stable event history is required.

Dataset, region and metric/series selectors apply to data exploration, training
and new historical runs. The simulation dialog repeats the full selected input
identity and compatible model version. Existing runs retain their own recorded
configuration and manifest. The forecasts view reports the selected version's
chronological validation champion, final-test persistence comparison and measured
interval coverage. Longer-horizon recursive interval widening remains heuristic.
