"""Reproducible, AC-bus microgrid simulation. No persistence or network access.

Regional measurements drive a *modelled* load profile; they are never represented
as measured household or renewable output. See docs/SIMULATION.md for units.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
import random
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field, model_validator


SCENARIOS = (
    "normal", "high_demand", "low_renewable", "grid_outage",
    "battery_stress", "renewable_surplus",
)
_ALIASES = {"heatwave": "high_demand", "demand_spike": "high_demand",
            "cloudy": "low_renewable", "outage": "grid_outage"}
_EPS = 1e-7


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: str = "normal"
    steps: int = Field(default=48, ge=1, le=336)
    step_hours: float = Field(default=0.5, gt=0, le=24)
    seed: int = Field(default=42, ge=0, le=2**32 - 1)
    battery_capacity_kwh: float = Field(default=240, gt=0, le=1_000_000)
    initial_soc: float = Field(default=0.6, ge=0, le=1)
    min_soc: float = Field(default=0.15, ge=0, lt=1)
    max_soc: float = Field(default=0.95, gt=0, le=1)
    battery_power_kw: float = Field(default=60, ge=0, le=1_000_000)
    efficiency: float = Field(default=0.95, gt=0, le=1)
    grid_import_limit_kw: float = Field(default=100, ge=0, le=1_000_000)
    grid_export_limit_kw: float = Field(default=60, ge=0, le=1_000_000)
    solar_capacity_kw: float = Field(default=120, ge=0, le=1_000_000)
    wind_capacity_kw: float = Field(default=35, ge=0, le=1_000_000)
    base_load_kw: float = Field(default=100, gt=0, le=1_000_000)
    flexible_fraction: float = Field(default=0.15, ge=0, le=1)
    critical_fraction: float = Field(default=0.25, ge=0, le=1)
    import_price: float = Field(default=8, ge=0, le=1_000_000)
    export_price: float = Field(default=3, ge=0, le=1_000_000)
    replay_speed: float = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def coherent_limits(self) -> "Scenario":
        self.name = _ALIASES.get(self.name, self.name)
        if self.name not in SCENARIOS:
            raise ValueError(f"name must be one of: {', '.join(SCENARIOS)}")
        if not self.min_soc < self.max_soc:
            raise ValueError("min_soc must be below max_soc")
        if not self.min_soc <= self.initial_soc <= self.max_soc:
            raise ValueError("initial_soc must lie between min_soc and max_soc")
        if self.flexible_fraction + self.critical_fraction > 1 + 1e-12:
            raise ValueError("flexible and critical fractions must not overlap")
        if self.export_price > self.import_price:
            raise ValueError("export_price must not exceed import_price")
        return self


@dataclass
class TickContext:
    scenario: Scenario
    step: int
    hour: float
    demand_kw: float
    solar_kw: float
    wind_kw: float
    battery_kwh: float
    import_limit_kw: float
    tariff: float
    forecast_demand_kw: float
    proposals: dict[str, dict[str, Any]] = field(default_factory=dict)
    failed_agents: list[str] = field(default_factory=list)

    @property
    def generation_kw(self) -> float:
        return self.solar_kw + self.wind_kw

    @property
    def critical_kw(self) -> float:
        return self.demand_kw * self.scenario.critical_fraction

    @property
    def available_discharge_kw(self) -> float:
        s = self.scenario
        return max(0.0, min(s.battery_power_kw,
                   (self.battery_kwh - s.min_soc * s.battery_capacity_kwh)
                   * s.efficiency / s.step_hours))

    @property
    def available_charge_kw(self) -> float:
        s = self.scenario
        return max(0.0, min(s.battery_power_kw,
                   (s.max_soc * s.battery_capacity_kwh - self.battery_kwh)
                   / (s.efficiency * s.step_hours)))


class RiskForecastAgent:
    name = "Risk & Forecast Agent"

    def propose(self, ctx: TickContext) -> dict[str, Any]:
        firm = ctx.generation_kw + ctx.import_limit_kw + ctx.available_discharge_kw
        shortage = max(0.0, ctx.forecast_demand_kw - firm)
        reserve = max(0.0, ctx.battery_kwh - ctx.scenario.min_soc
                      * ctx.scenario.battery_capacity_kwh)
        risk = min(1.0, shortage / max(ctx.forecast_demand_kw, 1e-9)
                   + 0.2 * max(0.0, ctx.forecast_demand_kw - ctx.generation_kw
                               - ctx.import_limit_kw) / max(ctx.forecast_demand_kw, 1e-9))
        return {"action": "assess_energy_stress", "risk": risk,
                "forecast_demand_kw": ctx.forecast_demand_kw,
                "usable_reserve_kwh": reserve, "projected_shortage_kw": shortage,
                "reason": "Supply adequacy assessment; no equipment-failure labels are used."}


class EnergyResourceAgent:
    name = "Energy Resource Agent"

    def propose(self, ctx: TickContext) -> dict[str, Any]:
        net = ctx.demand_kw - ctx.generation_kw
        charge = min(max(0.0, -net), ctx.available_charge_kw)
        # At inexpensive hours, retain storage unless grid capacity is insufficient.
        peak = 17 <= ctx.hour < 22
        desired = max(0.0, net if peak else net - ctx.import_limit_kw)
        discharge = min(desired, ctx.available_discharge_kw)
        return {"action": "dispatch_storage", "charge_kw": charge,
                "discharge_kw": discharge,
                "reason": "Charge renewable surplus; retain reserve off peak and cover constrained supply."}


class DemandManagementAgent:
    name = "Demand Management Agent"

    def propose(self, ctx: TickContext) -> dict[str, Any]:
        shortage = max(0.0, ctx.demand_kw - ctx.generation_kw
                       - ctx.import_limit_kw - ctx.available_discharge_kw)
        reduction = min(shortage, ctx.demand_kw * ctx.scenario.flexible_fraction)
        return {"action": "curtail_flexible_load", "curtailed_kw": reduction,
                "reason": "Only configured noncritical flexible load may be curtailed, when supply is insufficient."}


class MarketTradingAgent:
    name = "Market & Trading Agent"

    def propose(self, ctx: TickContext) -> dict[str, Any]:
        return {"action": "propose_local_orders",
                "available_local_kwh": ctx.generation_kw * ctx.scenario.step_hours,
                "buy_limit_price": ctx.tariff,
                "sell_limit_price": min(ctx.tariff, ctx.scenario.export_price
                                        + 0.25 * (ctx.tariff - ctx.scenario.export_price)),
                "reason": "Allocate available local renewable energy; settlement is performed transactionally outside the simulator."}


class CriticalFacilityAgent:
    name = "Critical Facility Agent"

    def propose(self, ctx: TickContext) -> dict[str, Any]:
        return {"action": "prioritize_critical_load", "critical_demand_kw": ctx.critical_kw,
                "reason": "Serve critical load before remaining noncritical load; never curtail critical load as flexibility."}


def _dispatch(ctx: TickContext, charge: float = 0, discharge: float = 0,
              curtailed: float = 0) -> dict[str, float]:
    """Balance the bus; deliberately do not sanitize agent battery proposals."""
    s = ctx.scenario
    residual = ctx.demand_kw - curtailed + charge - ctx.generation_kw - discharge
    imported = min(max(0.0, residual), ctx.import_limit_kw)
    exported = min(max(0.0, -residual), s.grid_export_limit_kw)
    unmet = max(0.0, residual - imported)
    spill = max(0.0, -residual - exported)
    served = ctx.demand_kw - curtailed - unmet
    return {"charge_kw": charge, "discharge_kw": discharge,
            "curtailed_kw": curtailed, "grid_import_kw": imported,
            "grid_export_kw": exported, "unmet_kw": unmet,
            "renewable_spill_kw": spill, "critical_served_kw": min(ctx.critical_kw, max(0.0, served))}


def _orders(ctx: TickContext, plan: dict[str, float], proposal: dict[str, Any]) -> list[dict[str, Any]]:
    """Internal allocations only: they do not add a second energy source to the bus."""
    s = ctx.scenario
    served = max(0.0, ctx.demand_kw - plan["curtailed_kw"] - plan["unmet_kw"])
    quantity = min(ctx.generation_kw, served) * s.step_hours
    if quantity <= 1e-10:
        return []
    critical = min(plan["critical_served_kw"] * s.step_hours, quantity)
    result = [{"participant": "solar_coop", "side": "sell", "quantity_kwh": quantity,
               "limit_price": proposal["sell_limit_price"]}]
    for participant, amount in (("critical", critical), ("community", quantity - critical)):
        if amount > 1e-10:
            result.append({"participant": participant, "side": "buy", "quantity_kwh": amount,
                           "limit_price": proposal["buy_limit_price"]})
    return result


class CoordinatorAgent:
    name = "Coordinator Agent"

    def propose(self, ctx: TickContext) -> dict[str, Any]:
        if ctx.failed_agents:
            return {"action": "request_safe_fallback", "plan": _dispatch(ctx), "market_orders": [],
                    "reason": "An agent failed; request bounded grid supply with storage and market disabled."}
        energy = ctx.proposals[EnergyResourceAgent.name]
        demand = ctx.proposals[DemandManagementAgent.name]
        plan = _dispatch(ctx, energy["charge_kw"], energy["discharge_kw"], demand["curtailed_kw"])
        return {"action": "coordinate_dispatch", "plan": plan,
                "market_orders": _orders(ctx, plan, ctx.proposals[MarketTradingAgent.name]),
                "reason": "Resolve renewable supply, storage, flexible demand and critical-load priority before safety validation."}


AGENT_CLASSES = (RiskForecastAgent, EnergyResourceAgent, DemandManagementAgent,
                 MarketTradingAgent, CriticalFacilityAgent, CoordinatorAgent)


class SafetyLayer:
    """Independent deterministic validator. No proposal directly modifies state."""

    def validate(self, ctx: TickContext, plan: dict[str, Any],
                 market_orders: list[dict[str, Any]]) -> dict[str, Any]:
        s = ctx.scenario
        reasons: list[str] = []
        if not isinstance(plan, dict) or not isinstance(market_orders, list):
            return {"approved": False, "reasons": ["Malformed dispatch or market proposal."]}
        required = ("charge_kw", "discharge_kw", "curtailed_kw", "grid_import_kw",
                    "grid_export_kw", "unmet_kw", "renewable_spill_kw", "critical_served_kw")
        if any(key not in plan or not isinstance(plan[key], (int, float))
               or not math.isfinite(plan[key]) for key in required):
            return {"approved": False, "reasons": ["Plan contains missing or nonfinite numerical values."]}
        if any(plan[key] < -_EPS for key in required):
            reasons.append("Dispatch quantities must be nonnegative.")
        charge, discharge = plan["charge_kw"], plan["discharge_kw"]
        next_kwh = ctx.battery_kwh + (charge * s.efficiency - discharge / s.efficiency) * s.step_hours
        if charge > ctx.available_charge_kw + _EPS or discharge > ctx.available_discharge_kw + _EPS:
            reasons.append("Battery power or available-energy limit exceeded.")
        if charge > _EPS and discharge > _EPS:
            reasons.append("Simultaneous battery charge and discharge is prohibited.")
        if not s.min_soc * s.battery_capacity_kwh - _EPS <= next_kwh <= s.max_soc * s.battery_capacity_kwh + _EPS:
            reasons.append("Battery state of charge would leave its safe range.")
        if charge > max(0.0, ctx.generation_kw - (ctx.demand_kw - plan["curtailed_kw"])) + _EPS:
            reasons.append("Battery charging must use available renewable surplus.")
        if plan["grid_import_kw"] > ctx.import_limit_kw + _EPS or plan["grid_export_kw"] > s.grid_export_limit_kw + _EPS:
            reasons.append("Grid exchange limit exceeded.")
        if plan["grid_import_kw"] > _EPS and plan["grid_export_kw"] > _EPS:
            reasons.append("Simultaneous grid import and export is prohibited.")
        if plan["curtailed_kw"] > ctx.demand_kw * s.flexible_fraction + _EPS:
            reasons.append("Flexible-load limit exceeded.")
        if plan["unmet_kw"] > ctx.demand_kw - plan["curtailed_kw"] + _EPS:
            reasons.append("Unmet demand exceeds remaining demand.")
        served = ctx.demand_kw - plan["curtailed_kw"] - plan["unmet_kw"]
        if abs(plan["critical_served_kw"] - min(ctx.critical_kw, max(0.0, served))) > _EPS:
            reasons.append("Critical-load priority violated.")
        if plan["renewable_spill_kw"] > ctx.generation_kw + _EPS:
            reasons.append("Renewable spill exceeds available generation.")
        balance = (ctx.generation_kw + discharge + plan["grid_import_kw"] + plan["unmet_kw"]
                   - (ctx.demand_kw - plan["curtailed_kw"] + charge + plan["grid_export_kw"]
                      + plan["renewable_spill_kw"]))
        if abs(balance) > _EPS:
            reasons.append("Energy balance is inconsistent.")
        sell, buy, critical_buy, community_buy = 0.0, 0.0, 0.0, 0.0
        for order in market_orders:
            if not isinstance(order, dict):
                reasons.append("Malformed market order.")
                continue
            quantity, price = order.get("quantity_kwh"), order.get("limit_price")
            if (not isinstance(quantity, (int, float)) or not math.isfinite(quantity)
                    or quantity <= 0 or not isinstance(price, (int, float))
                    or not math.isfinite(price) or price < 0):
                reasons.append("Market quantities and prices must be finite and valid.")
                continue
            identity = (order.get("participant"), order.get("side"))
            if identity == ("solar_coop", "sell"):
                sell += quantity
            elif identity in (("critical", "buy"), ("community", "buy")):
                buy += quantity
                if identity[0] == "critical":
                    critical_buy += quantity
                else:
                    community_buy += quantity
            else:
                reasons.append("Invalid market participant or order side.")
        if sell > min(ctx.generation_kw, max(0.0, served)) * s.step_hours + _EPS:
            reasons.append("Market sale exceeds locally available energy serving load.")
        if abs(sell - buy) > _EPS:
            reasons.append("Local market proposals must conserve energy.")
        if critical_buy > plan["critical_served_kw"] * s.step_hours + _EPS:
            reasons.append("Critical participant buy exceeds served demand.")
        if community_buy > max(0.0, served - plan["critical_served_kw"]) * s.step_hours + _EPS:
            reasons.append("Community participant buy exceeds served demand.")
        return {"approved": not reasons, "reasons": reasons,
                "energy_balance_error_kw": balance, "next_battery_kwh": next_kwh}


def _observations(observations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float]:
    if not observations:
        raise ValueError("Historical observations are required; no measurements are fabricated.")
    clean: list[dict[str, Any]] = []
    for item in observations:
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("Observation values must be finite nonnegative numbers.")
        timestamp = item.get("observed_at")
        if isinstance(timestamp, datetime):
            timestamp = timestamp.isoformat()
        if not isinstance(timestamp, str):
            raise ValueError("Every observation needs an original observed_at timestamp.")
        try:
            datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("observed_at must be an ISO 8601 timestamp.") from exc
        clean.append({**item, "observed_at": timestamp, "value": float(value)})
    units = {item.get("unit") for item in clean}
    if len(units) > 1:
        raise ValueError("Normalize observation units before passing a series to the simulator.")
    mean = sum(item["value"] for item in clean) / len(clean)
    if mean <= 0:
        raise ValueError("Observation series needs a positive mean for explicit regional scaling.")
    return clean, mean


def _profile(s: Scenario, step: int, observation: dict[str, Any], mean: float,
             rng: random.Random, forecast: float | None) -> tuple[TickContext, dict[str, Any]]:
    hour = (step * s.step_hours) % 24
    load_profile = 0.82 + 0.28 * math.exp(-((hour - 19) / 3) ** 2) + 0.14 * math.exp(-((hour - 8) / 2.5) ** 2)
    demand_factor = {"high_demand": 1.5, "battery_stress": 1.25, "renewable_surplus": 0.7}.get(s.name, 1.0)
    generation_factor = {"low_renewable": 0.2, "battery_stress": 0.4}.get(s.name, 1.0)
    # Profiles and noise are model assumptions, independent of policy/agent actions.
    solar = s.solar_capacity_kw * max(0.0, math.sin(math.pi * (hour - 6) / 12)) if 6 < hour < 18 else 0.0
    solar *= generation_factor * rng.uniform(0.9, 1.0)
    wind = s.wind_capacity_kw * generation_factor * max(0.05, min(1.0,
           0.45 + 0.2 * math.sin(2 * math.pi * (hour + 2) / 24) + rng.uniform(-0.12, 0.12)))
    demand = s.base_load_kw * observation["value"] / mean * load_profile * demand_factor
    forecast_demand = demand if forecast is None else s.base_load_kw * forecast / mean * load_profile * demand_factor
    tariff_factor = 1.45 if 17 <= hour < 22 else (0.8 if hour < 6 else 1.0)
    ctx = TickContext(s, step, hour, demand, solar, wind, 0.0,
                      0.0 if s.name == "grid_outage" else s.grid_import_limit_kw,
                      s.import_price * tariff_factor, forecast_demand)
    provenance = {"input_value": observation["value"], "input_unit": observation.get("unit"),
                  "input_source_id": observation.get("source_id"),
                  "regional_series_mean": mean, "regional_scale_kw_per_input_unit": s.base_load_kw / mean,
                  "simulation_hour": hour, "load_profile_factor": load_profile,
                  "demand_scenario_factor": demand_factor, "generation_scenario_factor": generation_factor,
                  "value_kind": "modelled_simulation", "renewable_kind": "modelled_profile",
                  "forecast_available": forecast is not None}
    return ctx, provenance


def generate_steps(scenario: Scenario, observations: list[dict[str, Any]], policy: str = "gridmind",
                   forecast_values: list[float] | None = None) -> Iterator[dict[str, Any]]:
    """Yield safe modelled states. Identical inputs/seed give paired exogenous traces.

    Forecast values use the same original units as the observation series. Missing
    forecast entries use contemporaneous context, never an invented prediction.
    Playback pacing is the caller's responsibility; replay_speed does not alter physics.
    """
    if policy not in ("gridmind", "baseline"):
        raise ValueError("policy must be gridmind or baseline")
    data, mean = _observations(observations)
    if forecast_values is not None and any(isinstance(value, bool) or not isinstance(value, (int, float))
                                          or not math.isfinite(value) or value < 0 for value in forecast_values):
        raise ValueError("Forecasts must be finite nonnegative values in observation units.")
    rng = random.Random(scenario.seed)
    battery = scenario.initial_soc * scenario.battery_capacity_kwh
    safety_layer = SafetyLayer()
    agents = [agent() for agent in AGENT_CLASSES]
    for step in range(scenario.steps):
        observation = data[step % len(data)]
        forecast = forecast_values[step] if forecast_values is not None and step < len(forecast_values) else None
        ctx, provenance = _profile(scenario, step, observation, mean, rng, forecast)
        provenance["replay_cycle"] = step // len(data)
        ctx.battery_kwh = battery
        decisions: list[dict[str, Any]] = []
        risk = min(1.0, max(0.0, ctx.forecast_demand_kw - ctx.generation_kw
                           - ctx.import_limit_kw - ctx.available_discharge_kw)
                   / max(ctx.forecast_demand_kw, 1e-9))
        if policy == "baseline":
            net = ctx.demand_kw - ctx.generation_kw
            plan = _dispatch(ctx, min(max(0.0, -net), ctx.available_charge_kw),
                             min(max(0.0, net), ctx.available_discharge_kw))
            market_orders: list[dict[str, Any]] = []
            decisions.append({"agent": "Baseline policy", "action": "greedy_dispatch", "status": "proposed",
                              "reason": "Use renewable supply, then storage, then the grid; no demand flexibility or local market."})
        else:
            for agent in agents:
                try:
                    proposal = agent.propose(ctx)
                    if not isinstance(proposal, dict):
                        raise TypeError("Agent proposal must be a dictionary")
                    ctx.proposals[agent.name] = proposal
                    decisions.append({"agent": agent.name, "status": "proposed", **proposal})
                except Exception as exc:
                    ctx.failed_agents.append(agent.name)
                    decisions.append({"agent": agent.name, "action": "agent_failure", "status": "error",
                                      "error_type": type(exc).__name__,
                                      "reason": "Agent exception detected; deterministic fallback requested."})
            coordinated = ctx.proposals.get(CoordinatorAgent.name, {})
            plan = coordinated.get("plan", _dispatch(ctx))
            market_orders = coordinated.get("market_orders", [])
            proposed_risk = ctx.proposals.get(RiskForecastAgent.name, {}).get("risk", risk)
            if isinstance(proposed_risk, (int, float)) and math.isfinite(proposed_risk) and 0 <= proposed_risk <= 1:
                risk = proposed_risk
            else:
                ctx.failed_agents.append(RiskForecastAgent.name)
        safety = safety_layer.validate(ctx, plan, market_orders)
        if ctx.failed_agents:
            safety["approved"] = False
            safety["reasons"] = safety.get("reasons", []) + [f"Agent unavailable: {name}" for name in ctx.failed_agents]
        if not safety["approved"]:
            plan, market_orders = _dispatch(ctx), []
            fallback = safety_layer.validate(ctx, plan, market_orders)
            if not fallback["approved"]:
                raise RuntimeError(f"Deterministic fallback failed safety validation: {fallback['reasons']}")
            safety.update({"executed_fallback": True, "fallback_approved": True,
                           "next_battery_kwh": fallback["next_battery_kwh"],
                           "energy_balance_error_kw": fallback["energy_balance_error_kw"]})
        else:
            safety["executed_fallback"] = False
        battery = safety["next_battery_kwh"]
        state = {"demand_kw": ctx.demand_kw, "solar_kw": ctx.solar_kw, "wind_kw": ctx.wind_kw,
                 "generation_kw": ctx.generation_kw, "battery_soc": battery / scenario.battery_capacity_kwh,
                 "battery_kwh": battery, "critical_demand_kw": ctx.critical_kw,
                 "forecast_demand_kw": ctx.forecast_demand_kw, "risk": risk,
                 "tariff": ctx.tariff, "export_tariff": scenario.export_price,
                 "grid_import_limit_kw": ctx.import_limit_kw,
                 "energy_balance_error_kw": safety["energy_balance_error_kw"],
                 **plan, **provenance}
        yield {"step": step, "timestamp": observation["observed_at"],
               "input_observation_id": observation.get("id"), "state": state,
               "decisions": decisions, "safety": safety, "market_orders": market_orders, "policy": policy}


def metrics_for_steps(steps: list[dict[str, Any]], scenario: Scenario) -> dict[str, float | int]:
    """Community-level physical/cost metrics; DB settlement supplies P2P metrics."""
    dt = scenario.step_hours
    def energy(key: str) -> float:
        return sum(step["state"][key] * dt for step in steps)
    generated, spilled = energy("generation_kw"), energy("renewable_spill_kw")
    return {
        "unmet_demand_kwh": energy("unmet_kw"),
        "critical_served_kwh": energy("critical_served_kw"),
        "critical_unmet_kwh": energy("critical_demand_kw") - energy("critical_served_kw"),
        "operating_cost": sum((item["state"]["grid_import_kw"] * item["state"]["tariff"]
                               - item["state"]["grid_export_kw"] * item["state"]["export_tariff"]) * dt for item in steps),
        "renewable_utilization_pct": 100 * (generated - spilled) / generated if generated > 0 else 0.0,
        "peak_grid_import_kw": max((item["state"]["grid_import_kw"] for item in steps), default=0.0),
        "battery_throughput_kwh": energy("charge_kw") + energy("discharge_kw"),
        "flexibility_kwh": energy("curtailed_kw"),
        "safety_rejections": sum(not item["safety"]["approved"] for item in steps),
        "agent_failures": sum(decision.get("status") == "error" for item in steps for decision in item["decisions"]),
        "gross_demand_kwh": energy("demand_kw"),
        "served_demand_kwh": energy("demand_kw") - energy("curtailed_kw") - energy("unmet_kw"),
        "grid_import_kwh": energy("grid_import_kw"), "grid_export_kwh": energy("grid_export_kw"),
        "renewable_generation_kwh": generated, "renewable_spill_kwh": spilled,
        "energy_balance_max_abs_kw": max((abs(item["state"]["energy_balance_error_kw"]) for item in steps), default=0.0),
    }
