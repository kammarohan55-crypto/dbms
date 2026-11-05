# Forecasting and model selection

GRIDMIND forecasts the observed series in its original units. Karnataka's public
target is supplied energy in GWh, not unconstrained demand. Household readings
remain household energy in kWh. No renewable-generation, weather, failure, or
future target labels are invented.

## Chronological evaluation

After constructing historical features, actual labels are split by timestamp:
70% for fitting, 15% for selection and interval calibration, and 15% for final
evaluation. Features use earlier readings and known calendar values only. Missing
history periods are forward-filled for features; missing targets are never
counted as actual evaluation labels.

Three fixed candidates are compared:

- Seasonal persistence repeats the preceding week's value for daily series, or
  the preceding day's value for subdaily series. Very short series fall back to
  the previous reading.
- Absolute XGBoost predicts the energy level from lag, rolling, and calendar
  features.
- Residual XGBoost predicts the difference from seasonal persistence, then adds
  that correction to the seasonal value. This lets the estimator learn changes
  without requiring its trees to extrapolate absolute energy levels.

The candidate with the lowest **validation MAE** becomes the champion. Ties go
to seasonal persistence. Selection, fitted estimators, and interval radii are
frozen before final-test scoring. Final-test results cannot change the champion.
If persistence wins validation, future forecasts actually use persistence.
Both XGBoost models remain available as native JSON evidence.

Metrics include every candidate's validation MAE/RMSE, final-test MAE/RMSE,
calibrated interval radius, and empirical test interval coverage. The top-level
MAE/RMSE describe the selected champion. `outperforms_baseline_mae` compares that
champion with seasonal persistence on the final test. A better validation score
does not guarantee a better final-test score.

These are rolling-origin **one-step** evaluations: each target may use earlier
actual observations. Future inference is recursive and uses its earlier predicted
values. One-step test results do not establish multi-step performance.

## Intervals

The 90% absolute-residual quantile is calibrated on chronological validation
errors using a finite-sample quantile adjustment. The validation segment is
reused for model selection and calibration. Dependence, selection, and changing
conditions mean that 90% coverage is a nominal target, not a guarantee. Empirical
coverage is reported on the untouched final test. Recursive forecast intervals
widen by the square root of the horizon; this is a heuristic, not a separately
validated multi-step uncertainty model.

## Measured local public-data verification

The continuation build was checked against the cached, provenance-preserving
Karnataka public dataset with 3,894 actual observations. There were 2,720 fitting,
583 validation, and 584 final-test labels after feature construction. No source
download or relabeling was needed.

| Candidate | Validation MAE, GWh | Test MAE, GWh | Test RMSE, GWh | Test interval coverage |
| --- | ---: | ---: | ---: | ---: |
| Absolute XGBoost, selected | 8.202 | 22.816 | 32.109 | 57.02% |
| Residual XGBoost | 9.002 | 13.546 | 17.683 | 76.88% |
| Seasonal persistence | 16.126 | 16.618 | 22.278 | 89.04% |

**The validation-selected champion underperforms persistence on this final test,
and its interval coverage is below the nominal 90%.** Residual XGBoost has a lower
final-test error, but selecting it after seeing these test results would leak
test information into model selection. The build reports this limitation openly;
these results do not establish readiness for real-grid control. Improving and
validating selection requires a new experiment with a fresh evaluation period.

The local verification report is `.runtime/forecast-qa/report.json`. It contains
the full measured metrics and time boundaries. This is a separate QA artifact;
it does not silently replace a registered application model.

## Registry, replay, and request identity

Training accepts explicit `source_id`, `region_id`, `metric`, and `series_key`
selection. Region, metric, unit, source, and series identities are persisted in
both the native metadata and model registry. Prediction rejects incompatible
series. Existing version-1 native JSON models remain loadable; new artifacts use
version 2 with checksums for both XGBoost candidate files.

Up to the latest 336 actual held-out predictions are retained in model metadata.
Historical replay uses only matching actual observations strictly after the
model's calibration cutoff, verifies that their actual values have not changed,
and returns at most the requested number of steps. It does not fit on replay
labels. Older version-1 artifacts retain their originally stored replay length;
retraining is required to obtain longer stored replay windows.

Repeated future-forecast requests reuse stored rows for the same model, full
observed input fingerprint, and target timestamp. Extending a horizon reuses the
overlapping rows. A new actual reading creates a new forecast origin and distinct
rows even for overlapping target times. MySQL serializes writes for a model;
deterministic forecast primary keys also prevent duplicate concurrent inserts.
