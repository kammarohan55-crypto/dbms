# Schema upgrades and data preservation

Schema version `002_normalized_integrity` is recorded in `schema_migrations` only after the upgrade checks succeed. Fresh databases start with the current schema. Existing databases report a pending upgrade; startup does not silently replace populated tables.

```powershell
.\.venv\Scripts\python.exe scripts\migrate.py --check
.\.venv\Scripts\python.exe scripts\migrate.py
```

The private Windows installation uses its protected, ignored administrator credential for migration. The application user receives no additional global privileges. For another server, supply an administrator connection through `GRIDMIND_MIGRATION_URL` in the process environment. Credentials are not printed or accepted in command-line arguments.

The upgrade:

1. Creates `metric_definitions(name, unit, description)` and moves canonical metric units there. `run_metrics(run_id, name, value)` references both its run and metric, removing the partial dependency `name → unit` from the result relation.
2. Converts numerical MySQL columns from single-precision FLOAT to DOUBLE. Previously rounded values cannot be recovered by changing the column alone. Where original raw observations remain available and their SHA256 matches provenance, a reading is restored only if its old value equals the exact float32 encoding of that original measurement. This recovery is audited. Different values, changed raw files and missing evidence are not silently merged.
3. Adds `paused` to allowed run statuses.
4. Adds scoped foreign keys from both trade orders to `(order.id, run_id, step)`. Trades cannot link unrelated runs or simulation steps.
5. Installs INSERT/UPDATE trade guards checking buy/sell sides, distinct participants, price limits and cumulative quantities against the original order allocations. Market settlement remains a transaction that updates both sides together.
6. Rebuilds the analytical view using metric definitions, then records the schema version.

MySQL DDL commits implicitly. Each phase checks existing state before changing it, so an interrupted upgrade can be retried. A failed upgrade is not recorded as complete. SQLite development databases rebuild only affected relations inside a transaction, copy IDs and values, and check foreign keys before completion. The actual MySQL upgrade was applied to the existing demonstration database without resetting its sources, observations, models, runs, market records or journals.

The verified existing dataset retained all **3,894** observations and its original application IDs. **3,021** float32-rounded observation values were restored from checksum-verified raw evidence. A subsequent public-data import inserted zero observations and identified all 3,894 as duplicates. Previous model versions and run results remain available; old numerical results retain the precision with which they were originally saved.

For a fresh SQL-only recreation, run `sql/001_schema.sql`, `sql/002_analytics.sql`, and `sql/004_metric_definitions.sql`; these are fresh-schema scripts, not commands to rerun over a populated schema. `sql/003_demonstrations.sql` contains read/query examples. Use the inspected Python migrator for an existing database. Database normal forms, diagrams and deliberate scope materialization are documented in [DBMS.md](DBMS.md).
