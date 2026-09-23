"""Audit regressions for feasible fallback, finite evidence, and worker paths."""

import json

import pandas as pd
import pytest

from agent import Agent
from beesmart.agent.models import ArmStats, Channel
from environment import make_environment


CHANNELS = {"push": {"cost_per_contact": 0, "conversion_multiplier": 0.5},
            "sms": {"cost_per_contact": 4, "conversion_multiplier": 0.65}}


def make_test_environment(profile):
    model = pd.DataFrame([{"tariff_plan_code_from": "tariff_a", "tariff_plan_code_to": "tariff_b",
                           "arpu_segment": arpu, "arpu_change_pct": 0.5, "conversion_rate": 1.0}
                          for arpu in ("LOW", "MID", "HIGH")])
    tariffs = pd.DataFrame({"tariff_plan_code": ["tariff_a", "tariff_b"]})
    return make_environment(profile, model, tariffs, CHANNELS, 100000, 15000,
                            lambda *_: (0.0, 0.0), seed=42)[0]


def test_observed_oversized_cell_does_not_hide_an_unobserved_legal_final_segment(tmp_path):
    profile = pd.DataFrame({"ID_NUMBER": range(6001), "current_tariff": "tariff_a",
                            "arpu_segment": ["MID"] * 6000 + ["LOW"], "data_segment": "HEAVY",
                            "call_segment": "LOW", "predicted_arpu": 3000.0})
    env = make_test_environment(profile)
    agent = Agent(history_path=tmp_path / "no-history", policy_path=tmp_path / "no-policy")
    campaigns = agent.act(env)
    assert agent.diagnostics["n_pilots"] == 1
    assert len(campaigns) == 1
    assert campaigns[0]["filter_arpu_segment"] == "LOW"
    assert campaigns[0]["channel"] == "push"
    assert agent.diagnostics["validation"]["ok"]
    assert agent.diagnostics["final_contacts"] == 1
    assert "no_feasible_final_plan" not in agent.diagnostics["flags"]


def test_overflowing_observation_does_not_corrupt_prior_valid_statistics():
    channel = Channel("sms", 4, 0.65)
    stats = ArmStats()
    stats.update(0.13, 200, channel, 1)
    original = (stats.estimate(), stats.contacts, stats.pilot_numbers.copy())
    with pytest.raises(ValueError, match="finite"):
        stats.update(1e308, 200, channel, 2)
    assert (stats.estimate(), stats.contacts, stats.pilot_numbers) == original


def test_underflowing_channel_precision_cannot_create_infinite_confidence_bounds():
    stats = ArmStats()
    with pytest.raises(ValueError, match="finite"):
        stats.update(1.0, 200, Channel("sms", 4, 1e-161), 1)
    assert stats.precision == stats.weighted_sum == stats.contacts == 0
    assert stats.pilot_numbers == []


def test_overflowing_first_pilot_stops_without_retry_and_preserves_finite_fallback(tmp_path):
    profile = pd.DataFrame({"ID_NUMBER": range(30), "current_tariff": "tariff_a",
                            "arpu_segment": "LOW", "data_segment": "HEAVY",
                            "call_segment": "LOW", "predicted_arpu": 700.0})
    env = make_test_environment(profile)
    original = env.run_pilot
    attempts = []

    def corrupt_observation(**arguments):
        attempts.append(arguments)
        result = original(**arguments)
        result["observed_lift_ratio"] = 1e308
        return result

    env.run_pilot = corrupt_observation
    agent = Agent(history_path=tmp_path / "no-history", policy_path=tmp_path / "no-policy")
    campaigns = agent.act(env)
    assert campaigns and len(attempts) == 1
    assert agent.diagnostics["validation"]["ok"]
    assert agent.diagnostics["estimates"] == []
    assert "invalid_pilot_observation" in agent.diagnostics["flags"]
    json.dumps(agent.diagnostics, allow_nan=False)


def test_worker_resolves_explicit_policy_before_switching_to_dataset_directory(tmp_path, monkeypatch, capsys):
    from beesmart.application import worker
    import local_eval

    invocation = tmp_path / "invocation"
    dataset = tmp_path / "dataset"
    invocation.mkdir()
    dataset.mkdir()
    policy = invocation / "selected-policy.json"
    policy.write_text("{}")
    captured = []

    def evaluate(recording_agent, **_kwargs):
        captured.append(recording_agent.delegate.policy_path)
        recording_agent.campaigns = [{"target_tariff": "tariff_b", "channel": "push"}]
        return {"n_pilots": 1}

    monkeypatch.setattr(local_eval, "evaluate_agent", evaluate)
    monkeypatch.chdir(invocation)
    monkeypatch.setattr("sys.argv", ["beesmart.application.worker", "42", "--data-dir", str(dataset),
                                    "--policy-path", "selected-policy.json"])
    worker.main()
    assert captured == [policy]
    assert json.loads(capsys.readouterr().out)["type"] == "result"


def test_llm_candidate_cap_keeps_rare_arpu_classes(tmp_path, synthetic_frames):
    from beesmart.agent.llm import build_context, MAX_CANDIDATES

    codes = ["tariff_a", "tariff_b", "tariff_c", *[f"tariff_x{i}" for i in range(40)]]
    rows = [{"current_tariff": code, "arpu_segment": "LOW", "predicted_arpu": 700.0}
            for code in codes for _ in range(10)]
    rows.extend({"current_tariff": "tariff_a", "arpu_segment": category, "predicted_arpu": 1.0}
                for category in ("MID", "HIGH") for _ in range(10))
    (tmp_path / "data").mkdir()
    pd.DataFrame(rows).to_csv(tmp_path / "customer_profile.csv", index=False)
    synthetic_frames["history"].to_csv(tmp_path / "data/change_tariff.csv", index=False)
    pd.DataFrame({"tariff_plan_code": codes, "price_tariff": 1000.0}).to_csv(
        tmp_path / "data/dict_tariff.csv", index=False)
    context = build_context(tmp_path)
    assert len(context.candidates) == MAX_CANDIDATES
    assert {arm[1] for arm in context.candidates.values()} == {"LOW", "MID", "HIGH"}
    assert len(set(context.candidates.values())) == len(context.candidates)
    assert all(arm[0] != arm[2] for arm in context.candidates.values())


def test_observed_cell_larger_than_remaining_contacts_uses_unobserved_fallback(tmp_path):
    # The measured cell was initially addressable, but its whole final segment
    # no longer fits after the pilot. A one-person unmeasured cell still fits.
    profile = pd.DataFrame({"ID_NUMBER": range(13), "current_tariff": "tariff_a",
                            "arpu_segment": ["MID"] * 12 + ["LOW"], "data_segment": "HEAVY",
                            "call_segment": "LOW", "predicted_arpu": 3000.0})
    env = make_test_environment(profile)
    env.remaining_contacts = 20
    agent = Agent(history_path=tmp_path / "no-history", policy_path=tmp_path / "no-policy")
    campaigns = agent.act(env)
    assert agent.diagnostics["n_pilots"] == 1
    assert agent.diagnostics["pilot_contacts"] == 12
    assert len(campaigns) == 1
    assert campaigns[0]["filter_arpu_segment"] == "LOW"
    assert campaigns[0]["channel"] == "push"
    assert agent.diagnostics["validation"]["ok"]
    assert agent.diagnostics["final_contacts"] == 1
    assert agent.diagnostics["pilot_contacts"] + agent.diagnostics["final_contacts"] <= 20
    assert "no_feasible_final_plan" not in agent.diagnostics["flags"]
