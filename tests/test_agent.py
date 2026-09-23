"""Behavioral tests: statistical units, public contract, and adverse outcomes."""

import json
from math import sqrt

import pandas as pd
import pytest

from agent import Agent
from beesmart.core.domain import load_domain
from beesmart.core.models import ArmStats, Channel, NOISE_STD
from beesmart.core.planner import Planner
from environment import make_environment


CHANNELS = {
    "push": {"cost_per_contact": 0, "conversion_multiplier": 0.50},
    "sms": {"cost_per_contact": 4, "conversion_multiplier": 0.65},
    "digital_ads": {"cost_per_contact": 22, "conversion_multiplier": 0.85},
    "call": {"cost_per_contact": 160, "conversion_multiplier": 1.20},
}


def synthetic_env(seed=42, effect_b=0.8, effect_c=0.15, n_per_cell=600, contacts=15000, budget=100000):
    rows = []
    for arpu in ("LOW", "MID", "HIGH"):
        for i in range(n_per_cell):
            rows.append({"ID_NUMBER": len(rows) + 1, "current_tariff": "tariff_a", "arpu_segment": arpu,
                         "data_segment": ("HEAVY", "LITE")[i % 2],
                         "call_segment": ("LOW", "MEDIUM", "HIGH")[i % 3],
                         "predicted_arpu": 3000.0 + 10 * (i % 10)})
    profile = pd.DataFrame(rows)
    tariffs = pd.DataFrame({"tariff_plan_code": ["tariff_a", "tariff_b", "tariff_c"]})
    model = pd.DataFrame([
        {"tariff_plan_code_from": "tariff_a", "tariff_plan_code_to": target, "arpu_segment": arpu,
         "arpu_change_pct": effect, "conversion_rate": 1.0}
        for arpu in ("LOW", "MID", "HIGH") for target, effect in (("tariff_b", effect_b), ("tariff_c", effect_c))
    ])
    return make_environment(profile, model, tariffs, CHANNELS, budget, contacts,
                            fallback_predict=lambda *args: (0.0, 0.0), seed=seed)


def make_agent(tmp_path, **kwargs):
    return Agent(history_path=tmp_path / "absent_history.csv", policy_path=tmp_path / "absent_policy.json", **kwargs)


def test_normalized_observations_combine_independent_information():
    observations = ArmStats()
    observations.update(0.10, 100, Channel("push", 0, 0.5), 1)
    observations.update(0.13, 200, Channel("sms", 4, 0.65), 2)
    estimate = observations.estimate()
    assert estimate.mean == pytest.approx(0.20)
    assert estimate.se == pytest.approx(NOISE_STD / sqrt(100 * 0.5**2 + 200 * 0.65**2))
    assert estimate.lower < estimate.mean < estimate.upper
    with pytest.raises(ValueError):
        ArmStats().estimate()


def test_call_lower_bound_remains_valid_at_full_conversion():
    call = Channel("call", 160, 1.2)
    for conversion in (0.1, 0.5, 0.9, 1.0):
        for change in (-0.5, 0.5):
            theta = change * conversion
            actual = change * min(conversion * 1.2, 1)
            assert call.lower_effect(theta) <= actual + 1e-12
    assert call.lower_effect(0.5) == 0.5


def test_fresh_audience_bound_counts_repeat_contacts_without_discounting_cost(tmp_path):
    env, _ = synthetic_env(n_per_cell=12)
    domain = load_domain(env, tmp_path / "none.csv", tmp_path / "none.json")
    parent = next(s for s in domain.segments if s.key == ("tariff_a", "HIGH", "", ""))
    assert parent.fresh_arpu(0) == pytest.approx(parent.arpu_sum)
    assert parent.fresh_arpu(2) == pytest.approx(parent.arpu_sum - 3090 - 3080)
    assert parent.fresh_arpu(24) == 0
    observations = ArmStats()
    observations.update(0.65, 200, Channel("sms", 4, 0.65), 1)
    candidates = Planner(domain).candidates({("tariff_a", "HIGH", "tariff_b"): observations},
                                          {("tariff_a", "HIGH"): 2}, 100000, 15000)
    sms = next(c for c in candidates if c.segment.key == parent.key and c.channel.name == "sms")
    assert sms.cost == parent.n * 4  # Recontacting does not refund SMS costs.


def test_whole_segment_planner_respects_money_and_independent_masks(tmp_path):
    env, _ = synthetic_env(n_per_cell=60)
    domain = load_domain(env, tmp_path / "none.csv", tmp_path / "none.json")
    observations = ArmStats()
    observations.update(0.65, 200, Channel("sms", 4, 0.65), 1)
    planner = Planner(domain)
    plan = planner.solve({("tariff_a", "HIGH", "tariff_b"): observations}, {}, 400, 45)
    validation = planner.validate(plan.campaigns(), 400, 45)
    assert validation["ok"]
    assert plan.contacts <= 45 and plan.cost <= 400
    assert all(c.segment.n in (10, 20, 30) for c in plan.candidates)
    duplicate = [plan.campaigns()[0], plan.campaigns()[0]]
    assert not planner.validate(duplicate, 100000, 15000)["ok"]


def test_pilot_evidence_changes_the_target_and_covers_all_arpu_classes(tmp_path):
    first, _ = synthetic_env(effect_b=0.8, effect_c=-0.4)
    second, _ = synthetic_env(effect_b=-0.4, effect_c=0.8)
    agent_a, agent_b = make_agent(tmp_path), make_agent(tmp_path)
    plan_a, plan_b = agent_a.act(first), agent_b.act(second)
    assert {c["target_tariff"] for c in plan_a} == {"tariff_b"}
    assert {c["target_tariff"] for c in plan_b} == {"tariff_c"}
    assert {p["arpu_segment"] for p in agent_a.diagnostics["pilots"][:3]} == {"LOW", "MID", "HIGH"}
    assert agent_a.diagnostics["validation"]["ok"]
    assert not any("explicit_ids" in c for c in plan_a)


def test_same_seed_and_reused_instance_reproduce_plan_despite_callback_failure(tmp_path):
    def broken_sink(*_):
        raise RuntimeError("Disconnected observer")

    agent = make_agent(tmp_path, event_sink=broken_sink)
    first, _ = synthetic_env()
    second, _ = synthetic_env()
    plan_a = agent.act(first)
    plan_b = agent.act(second)
    assert plan_a == plan_b
    json.dumps(agent.diagnostics, allow_nan=False)
    assert agent.diagnostics["validation"]["ok"]


def test_negative_evidence_produces_small_free_fallback(tmp_path):
    env, _ = synthetic_env(effect_b=-1, effect_c=-1)
    agent = make_agent(tmp_path)
    plan = agent.act(env)
    assert len(plan) == 1 and plan[0]["channel"] == "push"
    assert "fallback_plan" in agent.diagnostics["flags"]
    assert agent.diagnostics["final_cost"] == 0
    assert agent.diagnostics["validation"]["ok"]


def test_error_after_spending_is_not_retried_and_preserves_final_plan(tmp_path):
    env, _ = synthetic_env()
    original = env.run_pilot
    calls = []

    def committed_then_failed(**kwargs):
        calls.append(kwargs)
        original(**kwargs)
        raise RuntimeError("Transport failed after contact")

    env.run_pilot = committed_then_failed
    agent = make_agent(tmp_path)
    plan = agent.act(env)
    assert len(calls) == 1
    assert agent.diagnostics["pilot_contacts"] == 200
    assert "pilot_error:RuntimeError" in agent.diagnostics["flags"]
    assert "no_valid_pilot_observation" in agent.diagnostics["flags"]
    assert plan and agent.diagnostics["validation"]["ok"]


def test_tiny_contact_balance_reserves_a_valid_final_segment(tmp_path):
    env, _ = synthetic_env(n_per_cell=12, contacts=20, budget=0)
    agent = make_agent(tmp_path)
    plan = agent.act(env)
    assert plan and agent.diagnostics["validation"]["ok"]
    assert agent.diagnostics["n_pilots"] >= 1
    assert agent.diagnostics["pilot_contacts"] + agent.diagnostics["final_contacts"] <= 20
    assert all(p["channel"] == "push" for p in agent.diagnostics["pilots"])


def test_missing_optional_categories_stay_in_parent_audience(tmp_path):
    env, _ = synthetic_env(n_per_cell=12)
    env.customer_profile.loc[0, "data_segment"] = None
    env.customer_profile.loc[1, "call_segment"] = None
    domain = load_domain(env, tmp_path / "none.csv", tmp_path / "none.json")
    parent = next(s for s in domain.segments if s.key == ("tariff_a", "LOW", "", ""))
    assert parent.n == 12
    assert parent.arpu_sum == pytest.approx(env.customer_profile[env.customer_profile.arpu_segment == "LOW"].predicted_arpu.sum())
