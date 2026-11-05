-- Provenance join and aggregation, using the observation timestamp index.
SELECT s.name AS source, r.name AS region, o.metric, COUNT(*) AS readings,
       MIN(o.observed_at) AS start_at, MAX(o.observed_at) AS end_at, AVG(o.value) AS mean_value
FROM energy_observations o
JOIN data_sources s ON s.id=o.source_id JOIN regions r ON r.id=o.region_id
GROUP BY s.name,r.name,o.metric;

SELECT * FROM v_run_comparison ORDER BY comparison_id, metric, policy;

-- Actual settled quantities reconcile on both sides of the order book.
SELECT t.id, r.policy, buyer.name AS buyer, seller.name AS seller,
       t.quantity_kwh,t.price,t.quantity_kwh*t.price AS transaction_value
FROM market_trades t JOIN simulation_runs r ON r.id=t.run_id
JOIN market_orders b ON b.id=t.buy_order_id JOIN market_orders s ON s.id=t.sell_order_id
JOIN market_participants buyer ON buyer.id=b.participant_id
JOIN market_participants seller ON seller.id=s.participant_id;

-- Supply actual UUID and UTC timestamp parameters through your SQL client.
EXPLAIN SELECT id,observed_at,value FROM energy_observations
FORCE INDEX (ix_observation_region_time)
WHERE region_id='YOUR_REGION_UUID' AND observed_at>='2024-01-01' ORDER BY observed_at LIMIT 100;
EXPLAIN SELECT id,observed_at,value FROM energy_observations
IGNORE INDEX (ix_observation_region_time,ix_observation_metric_time)
WHERE region_id='YOUR_REGION_UUID' AND observed_at>='2024-01-01' ORDER BY observed_at LIMIT 100;

-- Application market transaction (backend/gridmind/market.py) follows this flow:
-- START TRANSACTION; SELECT ... FROM simulation_runs WHERE id=? FOR UPDATE;
-- INSERT market_orders; deterministically match compatible prices;
-- UPDATE BOTH orders.remaining_kwh; INSERT market_trades; COMMIT;
-- Any exception rolls back the entire unit. Unique order/trade keys prevent replay.
