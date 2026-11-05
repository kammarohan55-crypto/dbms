-- Fresh schema seed; existing databases use scripts/migrate.py.
INSERT INTO metric_definitions(name,unit,description) VALUES
('unmet_demand_kwh','kWh','Involuntary demand not served, excluding deliberate flexible curtailment.'),
('critical_served_kwh','kWh','Energy delivered to priority critical loads.'),
('critical_unmet_kwh','kWh','Critical demand minus critical energy served.'),
('operating_cost','INR','External import procurement cost minus export revenue; internal transfers excluded.'),
('renewable_utilization_pct','%','100 times available renewable energy minus spill, divided by available renewable energy.'),
('peak_grid_import_kw','kW','Maximum grid import power during the run.'),
('battery_throughput_kwh','kWh','Sum of charge and discharge AC-bus energy.'),
('flexibility_kwh','kWh','Intentional curtailment of configured flexible, noncritical demand.'),
('safety_rejections','count','Rejected original plans; any executed fallback is independently validated.'),
('agent_failures','count','Detected exceptions in agent proposals.'),
('gross_demand_kwh','kWh','Demand before flexibility and involuntary shortages.'),
('served_demand_kwh','kWh','Gross demand minus flexibility and unmet demand.'),
('grid_import_kwh','kWh','Energy imported from the external grid.'),
('grid_export_kwh','kWh','Energy exported to the external grid.'),
('renewable_generation_kwh','kWh','Available modelled solar and wind energy.'),
('renewable_spill_kwh','kWh','Renewable energy discarded after internal use, charging and export.'),
('energy_balance_max_abs_kw','kW','Largest absolute AC-bus balance residual across executed steps.'),
('p2p_volume_kwh','kWh','Sum of actual settled internal trade quantities.'),
('p2p_transaction_value','INR','Sum of settled internal quantity times price, not external procurement cost.');
INSERT INTO schema_migrations(version,applied_at,description) VALUES ('002_normalized_integrity',UTC_TIMESTAMP(),'Normalize metric definitions; double precision; scoped trade integrity; paused lifecycle');
