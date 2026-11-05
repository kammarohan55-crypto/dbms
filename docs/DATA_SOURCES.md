# Data provenance, ingestion and forecasting

## Integrated public observations

[Hunt and Bloomfield, Zenodo record 14983362](https://zenodo.org/records/14983362)
provides daily Indian state electricity supplied and peak series. GRIDMIND downloads
only `corrected_daily_energy_met_MU.csv`, selects the `Karnataka` column, and keeps
the complete original CSV in `data/raw/`. Its `Date` field identifies a daily
aggregate. The record API reports **CC-BY-4.0**; retain attribution to Kieran Hunt
and Hannah Bloomfield and DOI `10.5281/zenodo.14983362` when reusing the data.

The actual downloaded file contains 3,955 Karnataka rows from
2013-01-01 through 2024-04-28. These observed bounds differ from the dataset
title's 2014–2024 range. Values are **MU = GWh** of electricity supplied, called
`supplied_energy_gwh` in this application. They are a demand-related context
series, not direct unconstrained demand measurements, household consumption, or
minute-level grid telemetry. Daily observations are anchored at midnight in
Asia/Kolkata; UTC conversion preserves that local date interpretation.

The file includes 49 identical duplicate rows and six dates with conflicting
duplicate readings. Exact repeats are collapsed; every version of the six
conflicting dates is excluded from training labels, yielding **3,894 usable
observations**. The provenance JSON lists those dates, and all original values
remain in the preserved raw file.

`fetch_karnataka(raw_dir)` verifies the published raw MD5
`172540c2c13282cc3dd4c9679b537aa0`, records a SHA-256, acquisition time, source
URL, original unit, row count, skipped rows and date bounds in a provenance JSON.
The Zenodo record metadata is also retained in
`data/raw/zenodo_14983362_metadata.json` from initial acquisition. Cached raw
files are reused without a network request and retain their original acquisition
time when the provenance file is present. Checksums differing from the pinned
record fail visibly. No synthetic values replace source outages. The raw CSV is
not altered during extraction.

Parser observation identifiers are deterministic UUIDs derived from source,
region, metric, observation timestamp and, for canonical/household imports, the
series key. SQL ingestion allocates persistent application UUIDs; repeat imports
reuse existing rows by the unique source/region/series/metric/timestamp identity.
Database constraints also enforce that uniqueness. Invalid dates,
nonfinite or negative values, and duplicate source dates are checked and counted.
Original values and units remain available. Daily regional GWh must be normalized
and explicitly scaled by the simulation before use as virtual asset kWh; this
does not turn the regional observations into measured microgrid consumption.

## Bounded household imports

The [London Datastore Low Carbon London dataset](https://data.london.gov.uk/dataset/smartmeter-energy-consumption-data-in-london-households-vqm0d)
describes half-hourly household energy in kWh, covering November 2011 to February
2014, with approximately 167 million rows. Its page lists Creative Commons
Attribution and UK Power Networks as provider. Both one-file and 168-file ZIP
options are listed. The full download is deliberately not performed by GRIDMIND.
Download a manageable file yourself and prepare a bounded household/date subset.
These observations represent UK households and are never labelled Karnataka.

`parse_csv` supports the familiar LCL columns:

```csv
LCLid,stdorToU,DateTime,KWH/hh (per half hour)
```

Leading/trailing header spaces are accepted. Each household gets a separate
region `London/<LCLid>` and metric `household_energy_kwh`. A row's numeric value
is kWh consumed over the half hour; it is not kW. Tariff group is retained as
metadata. The importer converts local timestamps using Europe/London. Ambiguous
autumn times and nonexistent spring times without a UTC offset are rejected for
review instead of assigning an arbitrary DST fold. Explicit offset timestamps
are accepted. A resulting gap is visible and must not be mistaken for zero usage.

The same importer accepts canonical CSV:

```csv
timestamp,value,unit,metric,region
```

`region` is optional. Optional `series_key` keeps independent meters within the
same region separate; omitted values use `regional`. If `value_kind` is supplied,
only `observed` rows are accepted. Explicit predictions or simulated values are
reported as rejected rather than relabelled as observed. Timestamp must be ISO 8601, preferably with `Z` or an
explicit UTC offset. Naive canonical timestamps use the configured timezone,
UTC by default. Accepted units are `kWh`, `MWh`, `GWh`, `kW` and `MW`; energy and
power are not silently interchanged. Uploads are limited to 10 MiB and 20,000
scanned rows, with an explicit `truncated` result. Invalid rows are counted with
up to 100 row-level errors. Exact repeat identities are deduplicated. Conflicting
values or units for one identity abort the import, so file order cannot select
an arbitrary training label. Conflicts with previously stored observations roll
back the SQL transaction. Upper-tail
outliers beyond Q3 + 3 IQR within a region/series/metric/unit group are flagged for
review and retained as original measurements, not clipped. A content SHA-256
supports traceability. Retain the uploaded source file for subsequent audit.

The configured JSON live adapter streams responses with a 10 MiB decoded-size
limit, a 20-second total download deadline checked between received chunks, and
shorter connect/read timeouts. Responses exceeding 20,000 observations are
rejected without importing a silent subset. There is no configured provider or
verified live connection by default. Original JSON and its SHA-256 are saved
before observations and their provenance commit together in SQL. Each inserted
observation references that raw JSON, the canonical CSV, acquisition time and a
provider URL with user credentials/query strings removed. `series_key` (or
`household_id`) is preserved from live rows. Failed refreshes retain earlier
observations and mark an existing source unavailable; they never invent new
measurements. Provider HTTP behaviour is checked using simulated test responses,
not represented as a completed provider deployment.

## Investigated reporting endpoints

The supplied [CEA daily-generation endpoint](https://cea.nic.in/reports/external/daily-generation?lang=en)
redirected to the [National Power Portal published reports](https://npp.gov.in/publishedReports)
when checked on 2026-10-21. The page offers PDF and XLS generation reports at
several aggregation levels. This is a human reporting interface, not an
established stable JSON observations API. Automated report extraction is not
implemented or presented as live telemetry; reviewed report values can be
converted to the canonical import format with provider and units documented.

The supplied [CEA power-supply peak archive](https://www.cea.nic.in/reports/dashboardView?headingId=107&lang=en&source=archive&subHeadingId=1003&subSubHeadingId=10011&title=Power+Supply+Position+-+Peak)
timed out during investigation. Availability, machine-readable file layout and
specific redistribution terms were not verified, so it is not an active data
feed. No pre-existing live credentials or API integrations were present in the
initial empty repository. A historical source timestamp is always preserved;
accelerated replay is not newly measured live data.

## Forecast model and evaluation

The implemented primary model is `XGBRegressor` with a fixed seed and a native
JSON artifact. Training requires at least 120 actual nonnegative labels from one
region/metric/unit. Duplicate timestamps, mixed units or regions, and explicitly
predicted or simulation-generated labels fail validation. The default demand
workflow adopts the actual metric name, so Karnataka training registers
`supplied_energy_gwh` instead of claiming a directly measured demand target.

Features include known calendar cycles and strictly shifted lags and rolling
means/deviations. The calendar uses Asia/Kolkata for Karnataka and Europe/London
for London profiles. No contemporaneous target, unobserved future weather, or
fabricated renewable targets enter training. Regular frequency is inferred from
median timestamp spacing. Missing aligned periods are forward-filled **only to
construct past-history features**; they never become observed training or
evaluation labels. Severely incomplete or irregular time grids are rejected.

After lag construction, chronological samples are split 70% train, 15%
validation and 15% test. The estimator fits only training labels. Validation
absolute residuals calibrate a nominal 90% interval. Final test MAE, RMSE,
conditional MAPE, interval coverage, sample counts and seasonal persistence
baseline errors are saved. The baseline uses the same target timestamps and
available past data: a week for daily series, a day for sufficiently long
subdaily series, otherwise one previous period. A measured boolean records
whether model MAE beats this baseline; improvement is not assumed.

Scores describe **one-step rolling-origin forecasts**, with preceding actuals
available as features. Future predictions are recursive; previously predicted
values populate subsequent lags. Intervals widen by square root of forecast
horizon as an explicit heuristic. Neither one-step test scores nor dependent
time-series residual calibration establish guaranteed multi-step coverage.
Changing conditions can make these intervals poorly calibrated.

An actual cached-data verification on this pinned source produced test
MAE **22.816 GWh** and RMSE **32.109 GWh**, compared with the weekly persistence
baseline's **16.618 GWh** MAE and **22.278 GWh** RMSE. Empirical interval coverage
was **57.0%**, below the nominal 90%. These results show that the current fixed
XGBoost model underperformed this baseline on the heldout period. The dashboard
must show that outcome, not claim superior forecast accuracy. There were 242
unobserved daily periods on the aligned history grid; these did not become
training labels. Model parameters are not tuned against these published test
scores.

Model metadata includes version, target, units, frequency, train/validation/test
bounds, source and observation identifiers, training-data hash, model-file hash,
heldout chart points and limitations. Load verifies the native model checksum
and the prediction input's region/metric/unit/frequency. Forecast values retain
the training unit. Repeated loads of the same artifact and history reproduce the
same predictions. Artifacts do not use Python pickle.

Solar and wind forecasting require actual corresponding target labels. Weather
wind or solar-radiation covariates are not generator-output measurements.
Unsupported renewable training fails with a clear message. Exact equipment
failure prediction is not implemented because failure labels are absent.

## Deferred experiments

Hopfield associative retrieval and BernoulliRBM are deliberately deferred. The
downloaded state-level scalar series does not provide measured multivariate
microgrid state vectors, an established binary representation, or labelled
failure/anomaly outcomes. Training a network on invented asset states would not
validate real anomaly detection. A future experiment must specify state
representation, a chronological retrieval/anomaly benchmark, comparisons against
nearest-neighbour or other simple baselines, and criteria for useful output
before being integrated into the trusted control path. No improvement claim is
made for either experimental model.
