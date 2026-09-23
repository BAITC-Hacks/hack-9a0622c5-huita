"""Semantic guardrails for array/bitmask optimizations and frozen priorities.

No wall-clock thresholds: actual timing comparisons live in benchmark_core.py.
"""

import json
from dataclasses import replace
from itertools import product
from types import SimpleNamespace

import numpy as np
import pandas as pd

from beesmart.agent.domain import CALL_VALUES, DATA_VALUES, load_domain, membership_mask
from beesmart.agent.planner import Planner


def environment():
    rows = []
    for current, arpu in product(("tariff_1", "tariff_2", "tariff_3"), ("LOW", "MID", "HIGH")):
        for index in range(12):
            rows.append({"ID_NUMBER": len(rows) + 1, "current_tariff": current, "arpu_segment": arpu,
                         "data_segment": None if index == 0 else DATA_VALUES[index % 3],
                         "call_segment": None if index == 1 else CALL_VALUES[(index // 2) % 3],
                         "predicted_arpu": 1000.25 + index * 7.125})
    for index in range(5):
        rows.append({"ID_NUMBER": len(rows) + 1, "current_tariff": "tariff_4", "arpu_segment": "HIGH",
                     "data_segment": "HEAVY", "call_segment": "LOW", "predicted_arpu": 100.0})
    profile = pd.DataFrame(rows).sample(frac=1, random_state=7)
    return SimpleNamespace(customer_profile=profile,
                           tariffs=pd.DataFrame({"tariff_plan_code": [f"tariff_{i}" for i in range(1, 22)]}),
                           channels={"push": {"cost_per_contact": 0, "conversion_multiplier": 0.5},
                                     "sms": {"cost_per_contact": 4, "conversion_multiplier": 0.65}})


def domain(tmp_path, priorities=None):
    policy = {"schema_version": 1, "source": "test_frozen_policy", "alternative_targets_by_cell": {}}
    if priorities is not None:
        policy["priority_arms"] = priorities
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy))
    return load_domain(environment(), tmp_path / "no_history.csv", path)


def test_packed_membership_preserves_non_byte_aligned_positions():
    selected = np.zeros(17, dtype=bool)
    selected[[0, 7, 8, 16]] = True
    assert membership_mask(selected) == (1 << 0) | (1 << 7) | (1 << 8) | (1 << 16)
    assert membership_mask(np.zeros(0, dtype=bool)) == 0


def test_vectorized_segments_match_independent_pandas_filter_semantics(tmp_path):
    loaded = domain(tmp_path)
    reference = {}
    for cell in loaded.cells:
        parent = loaded.profile[(loaded.profile.current_tariff == cell[0]) & (loaded.profile.arpu_segment == cell[1])]
        seen = set()
        for data, call in product(("",) + DATA_VALUES, ("",) + CALL_VALUES):
            selected = parent
            if data:
                selected = selected[selected.data_segment == data]
            if call:
                selected = selected[selected.call_segment == call]
            mask = sum(1 << int(position) for position in selected.index)
            if not 0 < len(selected) <= 5000 or mask in seen:
                continue
            seen.add(mask)
            prefix = np.concatenate(([0.0], np.cumsum(np.sort(selected.predicted_arpu.to_numpy(dtype=float))[::-1])))
            reference[cell + (data, call)] = (mask, len(selected), tuple(prefix.tolist()))
    assert {segment.key: (segment.mask, segment.n, segment.top_prefix) for segment in loaded.segments} == reference
    parent = next(segment for segment in loaded.segments if segment.key == ("tariff_1", "LOW", "", ""))
    assert parent.n == 12  # Null optional categories remain in unfiltered cells.


def test_indexed_fallback_matches_exhaustive_canonical_choice(tmp_path):
    loaded = domain(tmp_path)
    expected = min((segment.n, segment.key + (target, "push"))
                   for segment in loaded.segments for target in loaded.tariffs
                   if target != segment.cell[0])
    plan = Planner(loaded).fallback({}, {}, 1000, 15000)
    assert len(plan.candidates) == 1
    assert (plan.candidates[0].segment.n, plan.candidates[0].key) == expected


def test_validation_reapplies_profile_filters_instead_of_trusting_cached_segments(tmp_path):
    loaded = domain(tmp_path)
    original = next(segment for segment in loaded.segments if segment.key == ("tariff_1", "LOW", "", ""))
    # A stale/corrupted optimization cache must not make overlapping output legal.
    loaded.segments = [replace(segment, mask=0) for segment in loaded.segments]
    planner = Planner(loaded)
    campaign = {**original.filters, "target_tariff": "tariff_21", "channel": "sms"}
    result = planner.validate([campaign, campaign], 1000, 15000)
    assert result["overlap_count"] == 12
    assert result["final_contacts"] == 24
    assert result["final_cost"] == 96
    assert not result["ok"]
    assert planner.validate([campaign], 1000, 15000)["ok"]


def test_absent_empty_and_invalid_priority_entries_preserve_baseline_queue(tmp_path):
    baseline = domain(tmp_path).queue
    assert domain(tmp_path, []).queue == baseline
    invalid = [["unknown", "LOW", "tariff_2"], ["tariff_4", "HIGH", "tariff_2"],
               ["tariff_1", "LOW", "tariff_1"], ["tariff_1", "LOW", "unknown"],
               ["tariff_1", "UNKNOWN", "tariff_2"], ["tariff_1"], "bad", {"arm": []}]
    assert domain(tmp_path, invalid).queue == baseline


def test_frozen_priorities_influence_search_but_preserve_coverage_uniqueness_and_limit(tmp_path):
    high = ["tariff_3", "HIGH", "tariff_21"]
    low = ["tariff_2", "LOW", "tariff_20"]
    mid = ["tariff_3", "MID", "tariff_19"]
    rest = [["tariff_1", "HIGH", f"tariff_{i}"] for i in range(2, 22)]
    loaded = domain(tmp_path, [high, high, ["unknown", "LOW", "tariff_3"], low, mid, *rest])
    assert loaded.queue[:3] == [tuple(low), tuple(mid), tuple(high)]
    assert loaded.queue[3] == tuple(rest[0])
    assert len(loaded.queue) == len(set(loaded.queue)) == 14
    assert all(loaded.cells[arm[:2]].n >= 10 and arm[2] in loaded.tariffs and arm[0] != arm[2]
               for arm in loaded.queue)
