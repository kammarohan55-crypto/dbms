from datetime import datetime, timedelta, timezone
import math

import pytest
from pydantic import ValidationError

from gridmind import simulation as sim


def observations(count=48):
    origin = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [{"id": index + 1, "observed_at": (origin + timedelta(days=index)).isoformat(),
             "value": 300 + 40 * math.sin(index / 5), "unit": "MU", "source_id": 1}
            for index in range(count)]


@pytest.mark.parametrize("name", sim.SCENARIOS)
@pytest.mark.parametrize("policy", ["baseline", "gridmind"])
def test_scenarios_conserve_energy_and_respect_asset_bounds(name, policy):
    scenario = sim.Scenario(name=name, steps=96)
    steps = list(sim.generate_steps(scenario, observations(), policy))
    previous_battery = scenario.initial_soc * scenario.battery_capacity_kwh
    for item in steps:
        state = item["state"]
        assert item["safety"]["approved"], item["safety"]
        assert abs(state["energy_balance_error_kw"]) < 1e-7
        assert scenario.min_soc - 1e-10 <= state["battery_soc"] <= scenario.max_soc + 1e-10
        assert state["charge_kw"] <= scenario.battery_power_kw + 1e-8
        assert state["discharge_kw"] <= scenario.battery_power_kw + 1e-8
        assert state["solar_kw"] <= scenario.solar_capacity_kw + 1e-8
        assert state["wind_kw"] <= scenario.wind_capacity_kw + 1e-8
        assert state["grid_import_kw"] <= state["grid_import_limit_kw"] + 1e-8
        assert state["grid_export_kw"] <= scenario.grid_export_limit_kw + 1e-8
        assert state["curtailed_kw"] <= state["demand_kw"] * scenario.flexible_fraction + 1e-8
        assert not (state["charge_kw"] > 1e-8 and state["discharge_kw"] > 1e-8)
        actual_next = previous_battery + scenario.step_hours * (
            state["charge_kw"] * scenario.efficiency - state["discharge_kw"] / scenario.efficiency)
        assert state["battery_kwh"] == pytest.approx(actual_next)
        served = state["demand_kw"] - state["curtailed_kw"] - state["unmet_kw"]
        assert state["critical_served_kw"] == pytest.approx(min(state["critical_demand_kw"], served))
        previous_battery = state["battery_kwh"]


def test_pair_uses_identical_observations_and_exogenous_trace():
    scenario = sim.Scenario(steps=60)
    data = observations()
    coordinated = list(sim.generate_steps(scenario, data))
    baseline = list(sim.generate_steps(scenario, data, "baseline"))
    assert coordinated == list(sim.generate_steps(scenario, data))
    for first, second in zip(coordinated, baseline):
        assert first["timestamp"] == second["timestamp"]
        assert first["input_observation_id"] == second["input_observation_id"]
        for field in ("demand_kw", "solar_kw", "wind_kw", "tariff", "critical_demand_kw"):
            assert first["state"][field] == second["state"][field]
    assert coordinated[48]["timestamp"] == data[0]["observed_at"]
    assert coordinated[48]["state"]["replay_cycle"] == 1


def test_observations_are_explicitly_scaled_and_not_renamed_as_asset_measurements():
    data = observations(2)
    data[0]["value"], data[1]["value"] = 100, 200
    first = next(sim.generate_steps(sim.Scenario(steps=1), data))
    state = first["state"]
    assert state["regional_series_mean"] == 150
    assert state["regional_scale_kw_per_input_unit"] == pytest.approx(100 / 150)
    assert state["demand_kw"] == pytest.approx(100 / 150 * 100 * state["load_profile_factor"])
    assert first["timestamp"] == data[0]["observed_at"]
    assert state["value_kind"] == "modelled_simulation"
    assert state["renewable_kind"] == "modelled_profile"
    assert state["input_unit"] == "MU"


@pytest.mark.parametrize("agent_class", sim.AGENT_CLASSES)
def test_failure_of_each_agent_uses_safe_fallback(monkeypatch, agent_class):
    def broken(self, ctx):
        raise RuntimeError("injected agent error")
    monkeypatch.setattr(agent_class, "propose", broken)
    scenario = sim.Scenario(steps=4, grid_import_limit_kw=1)
    steps = list(sim.generate_steps(scenario, observations()))
    for item in steps:
        assert not item["safety"]["approved"]
        assert item["safety"]["executed_fallback"]
        assert item["safety"]["fallback_approved"]
        assert item["state"]["charge_kw"] == 0
        assert item["state"]["discharge_kw"] == 0
        assert item["state"]["battery_soc"] == pytest.approx(scenario.initial_soc)
        assert abs(item["state"]["energy_balance_error_kw"]) < 1e-7
        assert item["market_orders"] == []
    assert sim.metrics_for_steps(steps, scenario)["safety_rejections"] == 4


def test_unsafe_agent_power_proposal_is_rejected(monkeypatch):
    def unsafe(self, ctx):
        return {"action": "unsafe_test", "charge_kw": 0, "discharge_kw": 10_000_000}
    monkeypatch.setattr(sim.EnergyResourceAgent, "propose", unsafe)
    item = next(sim.generate_steps(sim.Scenario(steps=1), observations()))
    assert not item["safety"]["approved"]
    assert any("Battery" in reason for reason in item["safety"]["reasons"])
    assert item["state"]["discharge_kw"] == 0


def test_malformed_coordinator_proposal_is_rejected(monkeypatch):
    monkeypatch.setattr(sim.CoordinatorAgent, "propose", lambda self, ctx: {"plan": "bad", "market_orders": []})
    item = next(sim.generate_steps(sim.Scenario(steps=1), observations()))
    assert item["safety"]["executed_fallback"]


def test_grid_outage_prioritizes_critical_load_and_counts_curtailed_energy_separately():
    scenario = sim.Scenario(name="grid_outage", steps=10, initial_soc=0.15,
                            solar_capacity_kw=0, wind_capacity_kw=0)
    steps = list(sim.generate_steps(scenario, observations()))
    for item in steps:
        state = item["state"]
        assert state["critical_served_kw"] == 0
        assert state["curtailed_kw"] == pytest.approx(state["demand_kw"] * scenario.flexible_fraction)
        assert state["unmet_kw"] + state["curtailed_kw"] == pytest.approx(state["demand_kw"])
    metrics = sim.metrics_for_steps(steps, scenario)
    assert metrics["unmet_demand_kwh"] + metrics["flexibility_kwh"] == pytest.approx(metrics["gross_demand_kwh"])


def test_market_proposals_allocate_only_available_local_supply():
    scenario = sim.Scenario(name="renewable_surplus")
    steps = list(sim.generate_steps(scenario, observations()))
    assert any(item["market_orders"] for item in steps)
    for item in steps:
        orders, state = item["market_orders"], item["state"]
        sell = sum(order["quantity_kwh"] for order in orders if order["side"] == "sell")
        buy = sum(order["quantity_kwh"] for order in orders if order["side"] == "buy")
        assert sell == pytest.approx(buy)
        assert sell <= state["generation_kw"] * scenario.step_hours + 1e-8
        for order in orders:
            assert order["quantity_kwh"] > 0
            assert order["limit_price"] >= 0


def test_forecast_is_consumed_and_absence_is_labelled():
    scenario = sim.Scenario(steps=1)
    normal = next(sim.generate_steps(scenario, observations()))
    forecast = next(sim.generate_steps(scenario, observations(), forecast_values=[10_000]))
    assert not normal["state"]["forecast_available"]
    assert forecast["state"]["forecast_available"]
    assert forecast["state"]["forecast_demand_kw"] > normal["state"]["forecast_demand_kw"]
    assert forecast["state"]["risk"] > normal["state"]["risk"]


@pytest.mark.parametrize("fields", [
    {"initial_soc": 0.1}, {"max_soc": 0.1}, {"efficiency": 0},
    {"step_hours": 0}, {"steps": 337}, {"flexible_fraction": 0.9},
    {"import_price": 1, "export_price": 3}, {"base_load_kw": float("nan")},
    {"name": "invented_scenario"},
])
def test_invalid_scenario_configuration_is_rejected(fields):
    with pytest.raises(ValidationError):
        sim.Scenario(**fields)


@pytest.mark.parametrize("data", [[], [{"observed_at": "2024-01-01", "value": -1}],
                                    [{"observed_at": "bad", "value": 1}],
                                    [{"observed_at": "2024-01-01", "value": 0}]])
def test_invalid_input_is_never_replaced_with_fabricated_measurements(data):
    with pytest.raises(ValueError):
        list(sim.generate_steps(sim.Scenario(), data))


def test_metrics_match_direct_aggregation():
    scenario = sim.Scenario(steps=48)
    steps = list(sim.generate_steps(scenario, observations()))
    metrics = sim.metrics_for_steps(steps, scenario)
    assert metrics["battery_throughput_kwh"] == pytest.approx(sum(
        (item["state"]["charge_kw"] + item["state"]["discharge_kw"]) * scenario.step_hours for item in steps))
    assert metrics["operating_cost"] == pytest.approx(sum(
        (item["state"]["grid_import_kw"] * item["state"]["tariff"]
         - item["state"]["grid_export_kw"] * scenario.export_price) * scenario.step_hours for item in steps))
    assert metrics["energy_balance_max_abs_kw"] < 1e-7
    assert metrics["safety_rejections"] == 0
    assert metrics["critical_unmet_kwh"] >= -1e-8


def test_zero_generation_metric_and_long_step_boundaries():
    scenario = sim.Scenario(steps=2, step_hours=24, solar_capacity_kw=0, wind_capacity_kw=0,
                            initial_soc=0.95, grid_import_limit_kw=0)
    steps = list(sim.generate_steps(scenario, observations()))
    assert sim.metrics_for_steps(steps, scenario)["renewable_utilization_pct"] == 0
    assert all(item["state"]["battery_soc"] >= scenario.min_soc - 1e-10 for item in steps)
