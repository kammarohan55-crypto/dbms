# Virtual microgrid and reproducible evaluation

`backend/gridmind/simulation.py` is a pure numerical module. It performs no network
or database writes. The application persists yielded states, decisions and safety
outcomes, then matches the yielded local-market proposals transactionally.

## Observations, profiles and time

The simulator requires a nonempty, consistently unit-labelled series of finite,
nonnegative historical values with a positive mean. Invalid data raises an error;
it is never replaced by an invented measurement. Every state retains its input
observation ID, source ID, raw value, original unit and original timestamp.

Regional daily electricity values cannot describe a household or a battery. A
regional series is explicitly scaled by `base_load_kw / mean(input values)` and
multiplied by a documented modelled intraday profile:

`load_profile = 0.82 + 0.28 exp(-((h-19)/3)^2) + 0.14 exp(-((h-8)/2.5)^2)`.

Demand is `raw_value × regional_scale × load_profile × scenario_demand_factor`.
These states carry `value_kind=modelled_simulation`. The observation timestamp is
unchanged. The separate simulation hour `h = step × step_hours mod 24` controls the
virtual asset profiles; it is not an inferred measurement time. If there are fewer
observations than steps, the series repeats in its supplied order and `replay_cycle`
records the repeat. This is a demonstration assumption, not temporal upsampling or
a claim that new regional observations exist. Supply chronological inputs for a
chronological replay. For a daily input and half-hour steps the simulator does not
claim that the original series has become half-hour measurement data.

Solar is a daylight sine profile between virtual hours 6 and 18, multiplied by a
seeded factor in [0.9, 1.0]. Wind is a bounded sinusoid with seeded noise. Both are
modelled, labelled `renewable_kind=modelled_profile`, and respect configured asset
capacity. No renewable model is trained from these profiles. The default 240 kWh
battery, 60 kW converter, efficiencies, grid limits, critical-load fraction,
flexible-load fraction and other capacities are configurable model assumptions.

`replay_speed` is a caller-controlled playback rate in steps per second (0 means no delay).
It never changes the energy duration, input timestamps or physical calculations.
The pure iterator does not sleep.

Forecast values, when supplied, must be in the same regional units as the inputs;
they undergo the same explicit scaling. Missing forecast entries use current
context with `forecast_available=false`; no invented prediction is recorded.
The Risk & Forecast Agent consumes supplied forecasts for energy-stress assessment.
This module does not predict equipment failure.

## Scenarios

| Name | Model changes |
| --- | --- |
| normal | Configured assets and profiles |
| high_demand | Demand multiplied by 1.5 |
| low_renewable | Solar and wind availability multiplied by 0.2 |
| grid_outage | External grid import unavailable throughout the run |
| battery_stress | Demand × 1.25, renewable availability × 0.4 |
| renewable_surplus | Demand × 0.7; renewable capacities unchanged |

Aliases: heatwave/demand_spike → high_demand; cloudy → low_renewable;
outage → grid_outage. Outage restricts imports; a configured external export path
remains available. Set the export limit to zero to model full grid disconnection.

## Six agents and deterministic safety

The six separate classes are `RiskForecastAgent`, `EnergyResourceAgent`,
`DemandManagementAgent`, `MarketTradingAgent`, `CriticalFacilityAgent` and
`CoordinatorAgent`. They propose numerical actions and explanations. No LLM is in
the control path. The coordinator resolves their proposals; only the independent
`SafetyLayer` can approve a dispatch for execution.

Safety checks nonnegative/finite values, battery energy and power bounds, exclusive
charge/discharge and import/export modes, renewable-only charging, grid limits,
flexibility bounds, critical priority, local-market quantities and energy balance.
If any agent raises an exception, returns an unusable proposal or produces unsafe
actions, the original proposal is rejected. A separately validated deterministic
fallback disables storage movement, flexible curtailment and market orders. It
uses available renewable supply and bounded grid imports, serves critical load
first, and records any remaining unmet demand. A rejection has `approved=false`,
explicit reasons, `executed_fallback=true` and `fallback_approved=true`. A fallback
that fails validation raises an error instead of producing a completed step.

Flexible load is noncritical and disjoint from critical load; their fractions may
not sum above one. Intentional flexible curtailment is accounted separately from
unmet demand. All available supply serves critical facilities first. Local market
orders allocate renewable energy already serving load; they do not create an
additional energy source or alter the AC-bus energy balance. Critical buyers have
priority in the proposed allocation. The solar_coop participant represents the
pooled virtual renewable generators, including configured wind output.

## Physical units and sign convention

All instantaneous powers are positive kW quantities. Each state obeys:

`solar + wind + discharge + import + unmet = demand - curtailed + charge + export + spill`.

Battery energy is kWh, and each step lasts `dt=step_hours`. Charge and discharge are
AC-bus powers. With one-way battery efficiency `η`:

`next_battery_kwh = battery_kwh + charge_kw × η × dt - discharge_kw / η × dt`.

Thus a charge/discharge cycle has round-trip efficiency `η²`. Power is capped by
both converter rating and the energy headroom available within a single step.
No EV, industrial process detail, network power flow, voltage or frequency is
modelled. These cannot be inferred from the regional input series.

## Paired policies and tariffs

For a defensible comparison, run both policies with the same `Scenario`, input
observations, forecast values and seed. Every call starts from the configured
initial SoC. The local random generator draws exogenous renewable noise in the
same order regardless of policy. Demand, renewable availability, tariffs and input
references therefore match exactly between policies.

Baseline uses renewables, then greedily discharges storage, then imports from the
grid. It charges renewable surplus, applies no flexible curtailment and submits no
P2P orders. GRIDMIND preserves battery reserve outside peak hours unless imports
are constrained; during peak hours it discharges against net load. It can curtail
only the permitted flexible noncritical shortage. Neither policy is guaranteed to
win. Compare unmet demand together with curtailment and served demand; curtailment
must not be disguised as supplying energy.

Prices are illustrative currency units per kWh, interpreted as INR in the default
application. Import tariff is configured import price × 0.8 at virtual hours
[0,6), × 1.45 at [17,22), and × 1 otherwise. Export price is constant. Market bids
buy at the current import tariff and sell between export and import prices. The
simulation does not establish real utility tariffs. Storage wear, capital costs,
carbon costs and deferral/rebound of curtailed demand are not included.

## Metric definitions

Every sum below multiplies state power by `dt`.

| Metric | Definition / unit |
| --- | --- |
| unmet_demand_kwh | Sum of unmet demand after flexible curtailment, kWh |
| critical_served_kwh | Sum of power actually allocated to critical demand, kWh |
| critical_unmet_kwh | Critical requested energy minus critical served energy, kWh |
| operating_cost | External imports × contemporaneous import price minus exports × export price, currency units |
| renewable_utilization_pct | 100 × (renewable generation − spill) / renewable generation; includes exports and battery charging; 0 if generation is zero |
| peak_grid_import_kw | Maximum grid import power, kW; 0 for no steps |
| battery_throughput_kwh | AC-bus energy charged plus AC-bus energy discharged, kWh |
| flexibility_kwh | Intentional noncritical flexible-load curtailment, kWh; no rebound model |
| safety_rejections | Number of original step proposals rejected, including agent failures; a fallback is not counted twice |
| agent_failures | Number of recorded agent exception decisions |
| gross_demand_kwh | Total pre-curtailment requested load, kWh |
| served_demand_kwh | Gross demand minus flexible curtailment minus unmet demand, kWh |
| grid_import_kwh / grid_export_kwh | External imported/exported energy, kWh |
| renewable_generation_kwh / renewable_spill_kwh | Available renewable energy / discarded renewable energy, kWh |
| energy_balance_max_abs_kw | Largest absolute per-step conservation residual, kW |

P2P volume and value are computed by the application from actual settled database
trades, never from unexecuted proposals. P2P payments are internal transfers and
are excluded from community operating cost to avoid double counting.

Tests exercise all six scenarios under both policies, AC energy conservation,
battery efficiency and bounds, critical priority, market allocations, exact paired
exogenous traces, invalid input, and failure of each agent independently. Passing
these numerical tests does not establish external-database connectivity or real
grid suitability.
