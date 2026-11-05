# MASTER PROJECT PROMPT — GRIDMIND

## 1. Project Identity

**Project Name:** GRIDMIND — AI-Based Predictive and Autonomous Microgrid Management System

**Project Type:** Database Management Systems (DBMS) Experiential Learning Project integrating Machine Learning, multi-agent coordination, energy analytics, and a simulated microgrid.

**Primary Objective:**

Build a data-driven microgrid digital twin that uses real-world electricity-demand, renewable-energy, and weather data wherever available to forecast energy conditions, assess energy stress, coordinate multiple intelligent agents, execute validated decisions inside a virtual microgrid, and compare the results against a baseline system.

The project must demonstrate a meaningful integration of **MySQL (SQL) and Amazon DynamoDB (NoSQL)**. Database design and functionality are central project requirements, not merely storage added to an AI application.

The system is intended for research, demonstration, and simulation. It must not claim to control the actual Karnataka electricity grid or predict real physical grid failures without suitable real-world failure-labelled data.

---

## 2. Core Problem Statement

Electricity demand and renewable-energy generation vary over time, making it difficult to balance supply, storage, consumption, and operating costs. A centralized energy-management strategy may not adequately coordinate storage, flexible demand, critical facilities, and local energy trading under changing conditions.

GRIDMIND aims to address this challenge by combining time-series forecasting, energy-risk assessment, multiple cooperating agents, peer-to-peer energy trading, deterministic safety checks, and simulation-based evaluation.

The system will use real observations as inputs wherever available, replay historical data at an accelerated rate, and model the effects of control actions in a virtual microgrid. It will evaluate whether coordinated decisions improve defined performance metrics compared with a baseline policy.

## 3. Mandatory Technical Architecture

Use the following architecture unless an existing working implementation requires a compatible alternative.

- **Backend:** Python with FastAPI.
- **Relational database:** MySQL.
- **NoSQL database:** Amazon DynamoDB.
- **Machine learning:** Python, pandas, NumPy, scikit-learn, and XGBoost where appropriate.
- **Simulation engine:** Python-based, discrete-time, and reproducible.
- **Frontend:** Reuse the existing frontend if present. Otherwise, implement a clean dashboard suitable for monitoring simulation runs, energy conditions, agent decisions, database-backed metrics, and comparisons.
- **Development:** Run the FastAPI application locally. Connect to DynamoDB using configured AWS credentials and region, or an already available compatible endpoint. Do not introduce Docker, Kubernetes, S3, or other AWS services unless explicitly necessary.
- **Configuration:** Environment variables and a `.env` file for local development. Never commit API keys, AWS secrets, or other credentials.

Before changing anything, inspect the existing repository, identify the implemented modules, available data sources, database connections, and existing tests. Reuse functioning code and preserve existing interfaces. Do not rebuild the entire project unnecessarily.

## 4. Hybrid Database Design — MySQL + DynamoDB

### 4.1 MySQL: Source of Truth for Relational Data

Use MySQL for normalized, structured records and transactional operations.

Design appropriate relational tables, including:

1. `data_sources` — source provider, source type, frequency, units, and metadata.
2. `regions` — region identifiers and geographical metadata.
3. `energy_observations` — timestamped observed demand, generation, and weather data, including provenance and data-quality information.
4. `microgrid_assets` — virtual batteries, consumers, renewable generators, EVs, industrial loads, and critical facilities.
5. `model_registry` — model names, versions, training-data references, evaluation metrics, and metadata.
6. `forecasts` — model predictions, target timestamps, prediction intervals when available, and model-version references.
7. `simulation_runs` — run ID, scenario, input dataset, initial configuration, model versions, status, and execution timestamps.
8. `market_participants` — virtual market participants and their associated assets where needed.
9. `market_orders` — buy/sell orders, quantity, limit price, participant, and order status.
10. `market_trades` — matched orders, traded energy, transaction price, and settlement status.
11. `run_metrics` — baseline and GRIDMIND performance metrics for each run.
12. `audit_logs` — important database or administrative changes when appropriate.

Additional tables, such as `battery_states`, should be introduced only when required by the relational model or query patterns.

Define primary keys, foreign keys, suitable data types, unique constraints, check constraints, indexes, and appropriate relationships. Use migrations or version-controlled SQL scripts so the database schema can be recreated reliably.

Normalize the relational design and explain 1NF, 2NF, 3NF, and BCNF using the actual schema. Avoid storing repeated participant, asset, model, and source information in every record.

### 4.2 DynamoDB: Simulation Events and Operational State

Create a DynamoDB table called `GridMindSimulationEvents`.

Use a composite-key design, initially:

- Partition key `PK`: `RUN#<run_id>`
- Sort key `SK`: an ordered identifier containing the simulation step, event type, and relevant asset or event ID.

Store event categories such as:

- `STATE` — simulation-state snapshots, battery charge levels, renewable output, consumption, grid imports/exports, and unmet demand.
- `AGENT_DECISION` — proposed actions, responsible agent, decision outcome, timestamp, and structured decision metadata.
- `SAFETY_CHECK` — approval or rejection, violated constraint, and reason.
- Other operational events only when they have a justified use case.

Use a flexible payload structure, an event type, a schema version, and consistent timestamps. Design sort keys to support retrieval of events for an entire simulation run and for an individual simulation step. Add secondary indexes only when demonstrated query requirements justify them.

DynamoDB must have an independent purpose rather than simply storing duplicate copies of all MySQL records.

### 4.3 Clear Data Ownership

- MySQL owns normalized entities, observed measurements, forecast records, run metadata, relational market orders and trades, and final analytics.
- DynamoDB owns high-frequency virtual-state snapshots and simulation event histories.
- Both databases must reference the same application-level `run_id`.
- MySQL foreign keys do not enforce relationships inside DynamoDB. Validate those references in the application.
- Do not assume cross-database writes are atomic. Implement retry-safe writes, idempotency, appropriate run statuses, and failure handling.
- A failed simulation must not silently appear completed, and partially written results must be detectable.

### 4.4 DBMS Functionality That Must Be Demonstrated

Implement meaningful examples of:

- CRUD operations and relational joins.
- Normalization and referential integrity.
- ACID transactions for market-order matching and trade recording.
- Constraints that prevent invalid transaction quantities and asset configurations.
- Indexes for frequent timestamp-, region-, and run-based queries.
- SQL views or equivalent analytical queries for comparing simulation results.
- A meaningful stored procedure or trigger where justified.
- DynamoDB partition/sort-key queries and flexible event payloads.
- Appropriate access control, configuration security, and auditability.
- Performance comparisons for selected indexed and non-indexed SQL queries.
- Tests for database constraints, transaction rollback, retry behaviour, and database failure handling where practical.

Do not add triggers, procedures, indexes, or tables solely to inflate the project scope. Explain the purpose of each implemented database feature.

---

## 5. Real-World Data and Historical Replay

The system must prioritize authentic observations over fabricated data.

### 5.1 Candidate Public Datasets

Investigate and use suitable sources, validating their format, availability, licensing, and data quality before integrating them.

**A. Indian state-level electricity data**

https://zenodo.org/records/14983362

Candidate source for historical state-level demand, peak demand, unmet peak demand, and weather features. Use the Karnataka records when available and suitable.

The data is daily, so it is primarily useful for regional context, demand analysis, and appropriately scoped forecasting—not by itself for minute-by-minute household control.

**B. London Smart Meter Energy Consumption dataset**

https://data.london.gov.uk/dataset/smartmeter-energy-consumption-data-in-london-households-vqm0d

Candidate source for half-hourly household electricity consumption. Begin with a manageable time range or household subset rather than loading the entire dataset indiscriminately.

This is UK household data, not Karnataka household data. Use it as a separate consumer-level load-profile dataset for the virtual market, and never claim that it represents Karnataka consumers.

**C. Central Electricity Authority**

https://www.cea.nic.in/reports/dashboardView?headingId=107&lang=en&source=archive&subHeadingId=1003&subSubHeadingId=10011&title=Power+Supply+Position+-+Peak

https://cea.nic.in/reports/external/daily-generation?lang=en

Investigate available demand and generation reports and determine whether structured files can be retrieved reliably for the project's needs.

Retain the existing configured live API integrations already present in the repository. Inspect their provider, units, geographical coverage, response format, update frequency, and error behaviour before making assumptions.

### 5.2 Data Ingestion Requirements

Build a repeatable ingestion pipeline that:

- Retrieves data through configured API credentials or imports supported public datasets.
- Validates timestamps, units, missing values, duplicates, negative readings, and outliers.
- Converts timestamps and units consistently while retaining source information.
- Records the source, acquisition time, observation time, quality status, and processing metadata as appropriate.
- Handles API timeouts, rate limits, expired credentials, and unavailable sources.
- Supports repeatable imports without creating duplicate observations.
- Preserves original observations or a traceable raw-data copy.
- Distinguishes actual observations from forecasts and simulation-generated values.

Do not invent API measurements when a request fails. Display source unavailability and switch to a clearly labelled historical replay when configured.

### 5.3 Live Data and Accelerated Simulation

A data source updating every 15 minutes does not require the simulation itself to run only once every 15 minutes.

Implement two modes:

1. **Historical replay mode:** Use historical observations as the time-series input and advance through them rapidly for demonstrations.
2. **Live-context mode:** Retrieve newly available real observations, update the regional context, and use suitable observations as the starting point for a simulation.

Make simulation step duration and replay speed configurable.

Preserve the original observation timestamps. Accelerating playback must not create the impression that the actual electricity grid is generating new readings at the accelerated rate.

Where the microgrid needs more granular household, battery, or asset-level variables than real datasets provide, explicitly label those values as modelled simulation variables. Use documented assumptions and configurable asset parameters. Do not present them as measured values.

Do not fabricate renewable-generation or failure labels merely to complete an ML pipeline. Disable or simplify unsupported capabilities if suitable training targets are unavailable.

---

## 6. Machine-Learning Module

Use XGBoost as the primary candidate for forecasting when the available data supports it.

Develop separate forecasting workflows as appropriate for:

- Electricity demand.
- Solar generation.
- Wind generation.

Train a model only when suitable historical target labels are available for that variable.

### Requirements

- Data cleaning and feature engineering.
- Time-of-day, day-of-week, seasonal and weather features where applicable.
- Lagged demand/generation features and rolling statistics where justified.
- Chronological train, validation, and test splits.
- Prevention of target leakage and use of future information.
- MAE and RMSE, with MAPE only when meaningful for the target data.
- Comparison against a basic baseline forecasting method.
- Model versioning and traceability of forecasts to their input data and model version.
- Uncertainty intervals when a defensible method can be validated.
- Saving and loading model artifacts reliably.

Do not describe regression performance as classification accuracy.

### Planned Experimental Models

The original GridMind design also considers:

- **Hopfield network:** associative retrieval of similar historical microgrid states.
- **Boltzmann machine / Restricted Boltzmann Machine:** experimental anomaly detection or representation of historical state patterns.

Treat these as experimental components. Implement and retain them only when their role, data requirements, output, and evaluation are clearly defined. Benchmark them where possible, and do not claim they improve performance without evidence.

Forecast outputs must be available to the Risk & Forecast Agent and linked to the relevant simulation run.

---

## 7. Six-Agent Architecture

Implement six clearly separated agents with defined inputs, outputs, and responsibilities.

### Agent 1: Risk & Forecast Agent

Consumes forecasts, observed conditions, and available uncertainty estimates. Assesses demand imbalance, renewable uncertainty, reserve adequacy, and energy-stress risk.

Do not claim exact equipment-failure prediction without suitable real failure-labelled data. In the absence of such labels, use the term energy-stress or supply-shortage risk assessment.

### Agent 2: Energy Resource Agent

Coordinates renewable availability, battery charging/discharging, EV resources when configured, and other energy resources. Respects their operating limits.

### Agent 3: Demand Management Agent

Identifies configurable flexible loads that can be shifted, deferred, or curtailed within defined limits. Respects comfort, operational, and priority constraints.

### Agent 4: Market & Trading Agent

Determines buy/sell quantities and proposed bids using the available surplus, demand, storage state, and market rules.

The actual peer-to-peer market matching engine must use deterministic, testable algorithmic logic—not LLM-generated transaction results.

### Agent 5: Critical Facility Agent

Prioritizes configured critical loads, monitors their energy requirements, and coordinates responses that reduce the risk of unmet critical demand.

### Agent 6: Coordinator Agent

Collects proposals from the other agents, resolves conflicting proposals according to documented priorities, and sends a proposed action plan to the safety layer.

### Agent Design Rules

- Use Python interfaces or classes with explicit inputs and outputs.
- Use ML models, deterministic rules, and optimization where appropriate for numerical decisions.
- Do not call an LLM on every simulation tick.
- If an LLM is used for optional explanations or strategic summaries, keep it outside the trusted numerical-control path.
- Record actionable decisions and outcomes in DynamoDB.
- Ensure that a single agent's failure can be detected and handled safely.

---

## 8. Deterministic Safety Layer

The safety layer is separate from the six agents and is mandatory.

Use the flow:

**Agents propose → Coordinator resolves → Safety layer validates → Simulation executes approved actions.**

Validate proposed actions against configured limits such as:

- Battery minimum and maximum state of charge.
- Charge/discharge power limits.
- Available renewable output.
- Grid import/export limits.
- Flexible-load bounds and critical-load priorities.
- Energy conservation and consistency of the proposed energy balance.
- Valid transaction quantities and applicable market constraints.

Reject unsafe actions with explicit reasons and use a documented fallback policy.

No agent should bypass this layer to modify simulation state.

Record safety approvals and rejections in DynamoDB, and make important outcomes visible in the dashboard.

---

## 9. Virtual Microgrid and Simulation Engine

Build a discrete-time simulation that represents a configurable microgrid with:

- Electricity demand.
- Solar and wind generation when supported by available data or explicitly modelled profiles.
- Battery storage.
- Grid imports and exports subject to configured limits.
- Household and industrial loads.
- Flexible demand.
- Critical facilities.
- Peer-to-peer energy trading.

At every simulation step:

1. Load the relevant historical observation or replay input.
2. Retrieve the latest relevant forecast.
3. Construct the current virtual microgrid state.
4. Calculate energy imbalance and relevant risk indicators.
5. Invoke the agents.
6. Obtain and resolve proposed actions.
7. Validate the proposed plan through the safety layer.
8. Apply only approved actions.
9. Update battery, load, generation, and market state.
10. Record state snapshots and events in DynamoDB.
11. Persist structured transactions and relevant outputs in MySQL.
12. Update simulation counters and run status.
13. Advance to the next step and repeat until the configured scenario completes.

Treat this as the intended 13-stage logical loop; implement it as clean, testable components rather than one large function.

Enforce a consistent energy balance. Clearly define the sign convention for imports, exports, charging, discharging, and unmet demand.

Keep scenario configuration separate from simulation code. Use documented assumptions for initial battery charge, battery capacity, renewable capacity, grid limits, flexible-load limits, and critical-load priorities.

If those variables are not available in real data, identify them as modelled parameters.

---

## 10. Peer-to-Peer Energy Market

Implement a functional market within the simulated microgrid.

- Participants submit buy and sell orders.
- Orders contain quantities, prices, participant identifiers, and relevant timestamps.
- A deterministic matching engine matches compatible orders using documented pricing and priority rules.
- Matched transactions are recorded in MySQL.
- Both sides of a transaction must remain consistent.
- Use database transactions and rollback on failure when updating matching orders and recording a trade.
- Add idempotency or equivalent protection against duplicate trade execution.
- Validate that no participant sells unavailable energy or buys more than permitted.
- Distinguish traded energy from energy imported from or exported to the external grid.

Do not implement the market as a UI-only demonstration. It must produce actual simulated orders and matched trades that can be queried in the database.

---

## 11. Baseline Versus GRIDMIND Evaluation

Implement two runs under equivalent scenario conditions:

**Baseline:** A simple documented policy with limited coordination—for example, a defined rule-based dispatch approach.

**GRIDMIND:** The coordinated multi-agent policy with safety validation.

Both runs must use equivalent input observations, initial conditions, asset capacities, simulation duration, and relevant constraints. Keep random seeds and other controlled assumptions consistent where applicable.

Calculate and save appropriate metrics, including:

- Total unmet demand in kWh.
- Critical-load energy served.
- Operating or energy procurement cost under documented pricing assumptions.
- Renewable-energy utilization.
- Peak grid import.
- Battery throughput or other defined battery-usage measures.
- P2P trading volume and transaction value.
- Number of safety rejections.
- Demand flexibility impact, where applicable.

Define every metric precisely and use consistent units.

Do not assume GRIDMIND performs better. Display measured outcomes and explain when it improves, worsens, or has no measurable effect. Simulation results demonstrate behaviour under the chosen scenario; they do not prove real-grid operational performance.

---

## 12. Dashboard Requirements

Create a clean dashboard with the following views:

1. **Overview:** current run status, data-source status, demand, generation, reserve, and stress indicators.
2. **Data Explorer:** observation history, source metadata, timestamps, and data-quality status.
3. **Forecasts:** predicted versus actual values and model evaluation metrics.
4. **Microgrid State:** battery state of charge, renewable production, loads, grid exchange, and unmet demand.
5. **Agent Activity:** decisions, actions proposed, approval/rejection status, and safety reasons.
6. **Energy Market:** buy/sell orders, matched trades, quantities, and prices.
7. **Simulation Comparison:** baseline versus GRIDMIND metrics and charts.
8. **DBMS Demonstration:** selected SQL query results, run/event retrieval, and relevant transaction or database-performance demonstrations.

Make the distinction between observed, predicted, and modelled simulation values visible in the interface.

The dashboard must retrieve data through backend services or APIs rather than directly connecting to either database from the browser.

---

## 13. FastAPI Backend and APIs

Build modular FastAPI routes and services for:

- Data-source status and data ingestion.
- Historical observation queries.
- Forecast generation and forecast retrieval.
- Microgrid asset configuration.
- Starting, pausing if supported, and retrieving simulation runs.
- Retrieving state histories and agent events.
- Querying safety outcomes.
- Market orders and trade history.
- Run metrics and baseline comparisons.
- Database health or readiness checks.

Use schemas for request validation and responses. Return meaningful errors for invalid input, missing data, unavailable dependencies, and failed transactions.

Separate routers, services, database repositories, simulation logic, agent logic, and model code. Keep business logic out of route handlers.

---

## 14. Security, Reliability, and Testing

- Keep secrets out of source control.
- Validate API inputs and external observations.
- Use least-privilege database credentials where practical.
- Add structured logging and useful error messages.
- Prevent duplicate ingestion and duplicate event/trade writes.
- Handle failures without falsely marking a run complete.
- Provide safe behaviour when forecasts or external APIs are unavailable.
- Use consistent units, timestamps, and identifiers.
- Use deterministic tests for calculations and decision rules.
- Test MySQL schema creation, foreign keys, constraints, and transactions.
- Test DynamoDB key design, run-history queries, and retry behaviour.
- Test the energy-balance calculations, market-matching engine, safety layer, and baseline comparisons.
- Test FastAPI endpoints and integration between the relevant components.
- Do not use a passing unit-test suite as proof that the entire application works end to end.

Provide a reliable local setup guide, dependency list, environment-variable example without real secrets, database initialization scripts, dataset-ingestion instructions, model-training instructions, startup commands, and tests.

---

## 15. Required DBMS Project Deliverables

Prepare project artefacts based on the actual implementation:

1. ER diagram with entities, primary keys, foreign keys, and cardinalities.
2. Normalized SQL schema and explanation of 1NF, 2NF, 3NF, and BCNF where applicable.
3. Explanation of why each data category belongs in MySQL or DynamoDB.
4. Hybrid-database architecture diagram and data-flow diagram.
5. DFD Level 0, Level 1, and Level 2 where required by the course.
6. SQL queries demonstrating joins, aggregation, constraints, indexes, views, and relevant transactions.
7. DynamoDB key-schema explanation and examples of queries for a run's state history and agent events.
8. Sample records and a traceable example linking a simulation run across both databases.
9. Test results demonstrating important correctness properties.
10. Baseline-versus-GRIDMIND evaluation and documented limitations.
11. Technical documentation covering setup, data sources, assumptions, architecture, testing, and future enhancements.

All documentation and diagrams must match the implemented system. Do not claim features, data sources, or results that have not actually been implemented or verified.

---

## 16. Implementation Strategy

Work incrementally. Do not attempt a large rewrite before understanding the repository.

**Phase 1 — Repository and environment audit**
- Inspect the existing project, identify working components, dependencies, configured APIs, database setup, and tests.
- Report the current state, blockers, and missing components.

**Phase 2 — Data ingestion and database foundation**
- Establish reproducible dataset ingestion and validation.
- Implement MySQL schema/migrations.
- Set up DynamoDB and test its key design.
- Verify traceability using a common run ID.

**Phase 3 — Forecasting**
- Build reliable data preparation, forecasting, evaluation, and model versioning.

**Phase 4 — Simulation core**
- Implement the energy-balance model, asset state, scenario configuration, and historical replay.

**Phase 5 — Agents and safety**
- Add the six agents, coordinator, deterministic safety layer, and audit events.

**Phase 6 — Energy market**
- Implement order matching, transaction-safe settlement, and market metrics.

**Phase 7 — Analytics and dashboard**
- Implement baseline comparison, charts, event histories, and data provenance.

**Phase 8 — Testing and documentation**
- Run unit and integration tests, address failures, verify the complete workflow, and produce DBMS deliverables.

At each phase, preserve functioning code, make changes small enough to review, run relevant tests, and report what was actually completed. Do not simply create placeholder modules with TODO comments and claim the feature is implemented.

---

## 17. Non-Negotiable Rules

1. MySQL and DynamoDB are both mandatory and must have distinct, defensible responsibilities.
2. Real measurements, model predictions, and simulation-generated values must remain distinguishable.
3. Historical data replay is permitted and encouraged, but accelerated playback must never be misrepresented as a faster real-world sampling rate.
4. Do not fabricate live observations, training labels, model accuracy, or experimental results.
5. Do not claim real-time Karnataka grid control or exact grid-failure prediction.
6. Use deterministic numerical logic and a mandatory safety layer for control actions.
7. Market matching and transactional settlement must work rather than exist only as visual demonstrations.
8. Compare the baseline and GRIDMIND under equivalent conditions.
9. Prioritize DBMS correctness, reproducibility, testability, and a working end-to-end prototype over unnecessary technical complexity.
10. Inspect the existing repository before changing it, preserve useful modules, and verify new functionality through tests.

## Final Objective

Deliver a functioning, locally runnable, DBMS-centred AI microgrid prototype that ingests real-world energy data where available, forecasts relevant conditions, replays historical scenarios through a virtual microgrid, coordinates six agents through a deterministic safety layer, performs peer-to-peer energy trading, stores relational records in MySQL and operational simulation events in DynamoDB, and produces reproducible baseline comparisons through a dashboard.

The final system must be technically defensible, clearly documented, demonstrable to academic evaluators, and honest about what has been measured, predicted, modelled, and simulated.