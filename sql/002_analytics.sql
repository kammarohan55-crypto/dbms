-- Run after 001_schema.sql. The application initializes these idempotently.
CREATE OR REPLACE VIEW v_run_comparison AS
SELECT r.comparison_id, r.id AS run_id, r.policy, r.status,
       m.name AS metric, m.value, d.unit
FROM simulation_runs r JOIN run_metrics m ON m.run_id = r.id
JOIN metric_definitions d ON d.name=m.name;

-- Audit run status changes even when a SQL client makes them outside the API.
DELIMITER $$
CREATE TRIGGER trg_run_status_audit AFTER UPDATE ON simulation_runs
FOR EACH ROW BEGIN
  IF OLD.status <> NEW.status THEN
    INSERT INTO audit_logs (id, action, entity_id, created_at, detail)
    VALUES (UUID(), 'run_status', NEW.id, UTC_TIMESTAMP(),
            JSON_OBJECT('before', OLD.status, 'after', NEW.status));
  END IF;
END$$
DELIMITER ;

-- Step-scoped trade guards generated from migrations.py.
DELIMITER $$
CREATE TRIGGER trg_trade_guard_insert BEFORE INSERT ON market_trades FOR EACH ROW BEGIN IF NOT (EXISTS (SELECT 1 FROM market_orders b JOIN market_orders s ON s.id=NEW.sell_order_id
      WHERE b.id=NEW.buy_order_id AND b.side='buy' AND s.side='sell'
      AND b.run_id=NEW.run_id AND s.run_id=NEW.run_id AND b.step=NEW.step AND s.step=NEW.step
      AND b.participant_id<>s.participant_id AND NEW.price>=s.limit_price AND NEW.price<=b.limit_price
      AND NEW.quantity_kwh <= b.quantity_kwh - (SELECT COALESCE(SUM(t.quantity_kwh),0)
           FROM market_trades t WHERE t.buy_order_id=NEW.buy_order_id AND t.id<>NEW.id) + 0.0000001
      AND NEW.quantity_kwh <= s.quantity_kwh - (SELECT COALESCE(SUM(t.quantity_kwh),0)
           FROM market_trades t WHERE t.sell_order_id=NEW.sell_order_id AND t.id<>NEW.id) + 0.0000001)) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Invalid trade sides, price, scope, or allocation'; END IF; END$$

CREATE TRIGGER trg_trade_guard_update BEFORE UPDATE ON market_trades FOR EACH ROW BEGIN IF NOT (EXISTS (SELECT 1 FROM market_orders b JOIN market_orders s ON s.id=NEW.sell_order_id
      WHERE b.id=NEW.buy_order_id AND b.side='buy' AND s.side='sell'
      AND b.run_id=NEW.run_id AND s.run_id=NEW.run_id AND b.step=NEW.step AND s.step=NEW.step
      AND b.participant_id<>s.participant_id AND NEW.price>=s.limit_price AND NEW.price<=b.limit_price
      AND NEW.quantity_kwh <= b.quantity_kwh - (SELECT COALESCE(SUM(t.quantity_kwh),0)
           FROM market_trades t WHERE t.buy_order_id=NEW.buy_order_id AND t.id<>NEW.id) + 0.0000001
      AND NEW.quantity_kwh <= s.quantity_kwh - (SELECT COALESCE(SUM(t.quantity_kwh),0)
           FROM market_trades t WHERE t.sell_order_id=NEW.sell_order_id AND t.id<>NEW.id) + 0.0000001)) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Invalid trade sides, price, scope, or allocation'; END IF; END$$
DELIMITER ;
