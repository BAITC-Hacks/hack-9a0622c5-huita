"""Causal checks: model choices and pilot observations affect actual actions.

All customer/history rows are generated here. HTTP uses an in-process transport;
the tests neither read credentials nor call a provider. The organizer environment
is a test harness, not an information source available to the participant agent.
"""

import asyncio
import json
import secrets

import httpx
import pandas as pd
import pytest

from agent import Agent
from beesmart.agent.llm import build_context, request_policy
from organizer.environment import make_environment


CLASSES = (("LOW", 700.0), ("MID", 3000.0), ("HIGH", 7000.0))
CHANNELS = {
    "push": {"cost_per_contact": 0, "conversion_multiplier": .5},
    "sms": {"cost_per_contact": 4, "conversion_multiplier": .65},
    "digital_ads": {"cost_per_contact": 22, "conversion_multiplier": .85},
    "call": {"cost_per_contact": 160, "conversion_multiplier": 1.2},
}


@pytest.fixture
def decision_data(tmp_path):
    current_tariffs = [f"tariff_source_{index}" for index in range(6)]
    targets = {"tariff_win": 2.0, "tariff_neutral": 1.0, "tariff_loss": .1}
    rows, history = [], []
    for current in current_tariffs:
        for arpu_segment, arpu in CLASSES:
            for index in range(600):
                rows.append({"ID_NUMBER": len(rows) + 1, "current_tariff": current,
                             "arpu_segment": arpu_segment, "predicted_arpu": arpu,
                             "data_segment": ("HEAVY", "LITE")[index % 2],
                             "call_segment": ("LOW", "MEDIUM", "HIGH")[index % 3]})
            for target, multiplier in targets.items():
                for _ in range(12):
                    history.append({"ID_NUMBER": len(history) + 1,
                                    "tariff_plan_code_from": current, "tariff_plan_code_to": target,
                                    "AVG_ARPU_PREV_3M": arpu, "AVG_ARPU_NEXT_3M": arpu * multiplier})
    profile = pd.DataFrame(rows)
    tariffs = pd.DataFrame({"tariff_plan_code": [*current_tariffs, *targets], "price_tariff": 1000.0})
    (tmp_path / "data").mkdir()
    profile.to_csv(tmp_path / "customer_profile.csv", index=False)
    tariffs.to_csv(tmp_path / "data/dict_tariff.csv", index=False)
    pd.DataFrame(history).to_csv(tmp_path / "data/change_tariff.csv", index=False)
    return tmp_path, profile, tariffs, build_context(tmp_path)


def selected_policy(context, target):
    available = [identifier for identifier, arm in context.candidates.items() if arm[2] == target]
    identifiers = [next(identifier for identifier in available if context.candidates[identifier][1] == segment)
                   for segment, _ in CLASSES]
    identifiers.extend(identifier for identifier in available if identifier not in identifiers)
    identifiers = identifiers[:14]
    calls = []

    def provider(request):
        calls.append(request)
        body = json.loads(request.content)
        assert "tools" not in body  # This is a planning call, not model tool use.
        return httpx.Response(200, json={"status": "completed", "usage": {
            "input_tokens": 100, "output_tokens": 100}, "output": [{"type": "message",
            "role": "assistant", "content": [{"type": "output_text", "text": json.dumps({
                "hypothesis_ids": identifiers, "summary": "Проверить выбранные гипотезы во всех классах ARPU."})}]}]})

    response = asyncio.run(request_policy(context, api_key=secrets.token_urlsafe(40),
                                          transport=httpx.MockTransport(provider)))
    assert len(calls) == 1
    assert len(response.policy["priority_arms"]) == 14
    return response.policy


def execute(decision_data, policy, *, actual_win_effect=1.0):
    folder, profile, tariffs, _ = decision_data
    policy_path = folder / "test-policy.json"
    policy_path.write_text(json.dumps(policy))
    # The historical data and frozen policy stay fixed when actual pilot effects
    # change. Only run_pilot exposes these new effects to the tested agent.
    effects = {"tariff_win": actual_win_effect, "tariff_neutral": 0.0, "tariff_loss": -.9}
    model = pd.DataFrame([{"tariff_plan_code_from": source, "tariff_plan_code_to": target,
                           "arpu_segment": segment, "arpu_change_pct": effect, "conversion_rate": 1 / 3}
                          for source in profile.current_tariff.unique()
                          for segment, _ in CLASSES for target, effect in effects.items()])
    env, _ = make_environment(profile, model, tariffs, CHANNELS, 100000, 15000,
                              fallback_predict=lambda *_: (0.0, 0.0), seed=42)
    events = []
    agent = Agent(history_path=folder / "data/change_tariff.csv", policy_path=policy_path,
                  event_sink=lambda name, payload: events.append((name, payload)))
    campaigns = agent.act(env)
    assert agent.diagnostics["validation"]["ok"]
    assert agent.diagnostics["pilot_contacts"] + agent.diagnostics["final_contacts"] <= 15000
    assert agent.diagnostics["pilot_cost"] + agent.diagnostics["final_cost"] <= 100000
    return campaigns, agent.diagnostics, events


def test_validated_model_selection_changes_executed_pilots_and_final_campaigns(decision_data):
    context = decision_data[3]
    winning_policy = selected_policy(context, "tariff_win")
    losing_policy = selected_policy(context, "tariff_loss")
    winning, win, _ = execute(decision_data, winning_policy)
    losing, loss, _ = execute(decision_data, losing_policy)
    for diagnostics, policy in ((win, winning_policy), (loss, losing_policy)):
        actual = [pilot["arm"] for pilot in diagnostics["pilots"] if pilot["reason"] == "first_wave"]
        assert actual == policy["priority_arms"]
        assert diagnostics["policy_source"] == "openai"
    assert win["pilots"] != loss["pilots"]
    assert winning != losing
    assert {campaign["target_tariff"] for campaign in winning} == {"tariff_win"}
    assert {campaign["target_tariff"] for campaign in losing} == {"tariff_loss"}
    assert "fallback_plan" not in win["flags"]
    assert "fallback_plan" in loss["flags"]
    assert execute(decision_data, winning_policy)[0] == winning


def test_identical_policy_adapts_final_action_to_observed_effects(decision_data):
    policy = selected_policy(decision_data[3], "tariff_win")
    positive_plan, positive, positive_events = execute(decision_data, policy, actual_win_effect=1.0)
    negative_plan, negative, negative_events = execute(decision_data, policy, actual_win_effect=-.9)
    assert positive["policy_hash"] == negative["policy_hash"]
    positive_initial = [p["arm"] for p in positive["pilots"] if p["reason"] == "first_wave"]
    negative_initial = [p["arm"] for p in negative["pilots"] if p["reason"] == "first_wave"]
    assert positive_initial == negative_initial == policy["priority_arms"]
    assert positive["pilots"][0]["observed_lift_ratio"] > negative["pilots"][0]["observed_lift_ratio"]
    assert positive_plan != negative_plan
    assert "fallback_plan" not in positive["flags"]
    assert "fallback_plan" in negative["flags"]
    assert len(negative_plan) == 1 and negative_plan[0]["channel"] == "push"
    assert positive["gain_low"] > 0 >= negative["gain_low"]
    for diagnostics, events in ((positive, positive_events), (negative, negative_events)):
        # Each observation is consumed before the next decision; plans are not
        # constructed once and merely replayed as staged UI events.
        event_names = [name for name, _ in events]
        for index, name in enumerate(event_names):
            if name == "pilot_result":
                assert event_names[index + 1] == "plan_selected"
        assert event_names.count("plan_selected") >= diagnostics["n_pilots"] + 1
