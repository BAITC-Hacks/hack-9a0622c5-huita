"""Public input validation, categorical audiences, and historical search order."""

import hashlib
import json
from dataclasses import dataclass
from itertools import product
from math import isfinite
from pathlib import Path

import numpy as np
import pandas as pd

from .models import ArmKey, Cell, Channel, Segment

ARPU_VALUES = ("LOW", "MID", "HIGH")
DATA_VALUES = ("HEAVY", "LITE", "NON_USER")
CALL_VALUES = ("HIGH", "LOW", "MEDIUM")
CHANNEL_NAMES = ("push", "sms", "digital_ads", "call")
REQUIRED = ("ID_NUMBER", "current_tariff", "arpu_segment", "data_segment", "call_segment", "predicted_arpu")


@dataclass
class Domain:
    profile: pd.DataFrame
    tariffs: tuple[str, ...]
    channels: dict[str, Channel]
    cells: dict[tuple[str, str], Cell]
    segments: list[Segment]
    queue: list[ArmKey]
    policy_hash: str
    flags: list[str]
    policy_source: str


def historical_ranks(path: Path) -> tuple[dict, bool]:
    """A search-order proxy only; never used as the measured effect."""
    try:
        columns = ["tariff_plan_code_from", "tariff_plan_code_to", "AVG_ARPU_PREV_3M", "AVG_ARPU_NEXT_3M"]
        history = pd.read_csv(path, usecols=columns)
        previous = pd.to_numeric(history[columns[2]], errors="coerce")
        following = pd.to_numeric(history[columns[3]], errors="coerce")
        valid = np.isfinite(previous) & np.isfinite(following) & (previous >= 100)
        history = history.loc[valid].copy()
        history["ratio"] = ((following[valid] - previous[valid]) / previous[valid]).clip(-1, 3)
        pairs = history.groupby(columns[:2], sort=True)["ratio"].agg(["median", "count"])
        totals = history.groupby(columns[0], sort=True).size()
        ranks = {(str(source), str(target)): (max(0.0, float(row["median"])) * int(row["count"]) / int(totals[source]),
                                                int(row["count"]))
                 for (source, target), row in pairs.iterrows()}
        return ranks, False
    except (OSError, ValueError, KeyError, pd.errors.ParserError):
        return {}, True


def read_policy(path: Path) -> tuple[dict, str, str, bool, list]:
    baseline = {"schema_version": 1, "source": "deterministic_baseline", "alternative_targets_by_cell": {}, "rationales_by_cell": {}}
    try:
        raw = path.read_bytes()
        if len(raw) > 1_000_000:
            raise ValueError("Policy is too large")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("Unsupported policy schema")
        alternatives = value.get("alternative_targets_by_cell")
        if not isinstance(alternatives, dict) or not all(isinstance(k, str) and isinstance(v, list)
                and all(isinstance(t, str) for t in v) for k, v in alternatives.items()):
            raise ValueError("Invalid policy alternatives")
        priorities = value.get("priority_arms", [])
        return alternatives, hashlib.sha256(raw).hexdigest(), str(value.get("source", "frozen_policy")), False, priorities if isinstance(priorities, list) else []
    except (OSError, ValueError, TypeError):
        raw = json.dumps(baseline, sort_keys=True).encode()
        return {}, hashlib.sha256(raw).hexdigest(), "deterministic_baseline", True, []


def membership_mask(values: np.ndarray) -> int:
    """Encode row membership in linear native-array work, not big-int sums."""
    return int.from_bytes(np.packbits(values, bitorder="little").tobytes(), "little")


def valid_priority_arms(values: list, cells: dict, tariffs: tuple[str, ...]) -> list[ArmKey]:
    """Treat frozen LLM suggestions as data, with the same public constraints."""
    valid, seen, known = [], set(), set(tariffs)
    for value in values:
        if not isinstance(value, (list, tuple)) or len(value) != 3 or not all(isinstance(v, str) for v in value):
            continue
        arm = tuple(value)
        cell = cells.get(arm[:2])
        if (cell is None or cell.n < 10 or arm[2] not in known or arm[2] == arm[0] or arm in seen):
            continue
        seen.add(arm)
        valid.append(arm)
    return valid


def load_domain(env, history_path: Path, policy_path: Path) -> Domain:
    missing = set(REQUIRED) - set(env.customer_profile.columns)
    if missing:
        raise ValueError(f"Missing profile columns: {sorted(missing)}")
    profile = env.customer_profile.copy().sort_values("ID_NUMBER", kind="stable").reset_index(drop=True)
    if profile["ID_NUMBER"].isna().any() or profile["ID_NUMBER"].duplicated().any():
        raise ValueError("Profile must have unique nonempty IDs")
    tariffs = tuple(sorted(str(v) for v in env.tariffs["tariff_plan_code"].dropna().unique()))
    channels = {}
    for name in CHANNEL_NAMES:
        raw = env.channels.get(name, {})
        try:
            cost, multiplier = float(raw["cost_per_contact"]), float(raw["conversion_multiplier"])
        except (KeyError, TypeError, ValueError):
            continue
        if isfinite(cost) and cost >= 0 and isfinite(multiplier) and multiplier > 0:
            channels[name] = Channel(name, cost, multiplier)
    if not channels:
        raise ValueError("No valid public channels")
    ranks, history_fallback = historical_ranks(history_path)
    alternatives, policy_hash, source, policy_fallback, raw_priorities = read_policy(policy_path)
    flags = []
    if history_fallback:
        flags.append("history_fallback")
    if policy_fallback:
        flags.append("policy_fallback")
    # Each category comparison is computed once. Optional missing values remain
    # in the parent cell; equality filters correctly exclude them.
    arpu_values = pd.to_numeric(profile["predicted_arpu"], errors="coerce").to_numpy(dtype=float)
    data_membership = {value: profile["data_segment"].eq(value).fillna(False).to_numpy(dtype=bool) for value in DATA_VALUES}
    call_membership = {value: profile["call_segment"].eq(value).fillna(False).to_numpy(dtype=bool) for value in CALL_VALUES}
    data_masks = {value: membership_mask(selected) for value, selected in data_membership.items()}
    call_masks = {value: membership_mask(selected) for value, selected in call_membership.items()}
    cells, segments = {}, []
    groups = profile.groupby(["current_tariff", "arpu_segment"], sort=True, observed=True).indices
    for (current, arpu), positions in groups.items():
        if current not in tariffs or arpu not in ARPU_VALUES:
            continue
        values = arpu_values[positions]
        if not np.all(np.isfinite(values) & (values >= 0)):
            # Never silently remove a row which an actual public filter selects.
            flags.append(f"invalid_arpu_cell:{current}|{arpu}")
            continue
        key = (str(current), str(arpu))
        cells[key] = Cell(key, len(positions), float(values.sum()))
        parent_membership = np.zeros(len(profile), dtype=bool)
        parent_membership[positions] = True
        parent_mask = membership_mask(parent_membership)
        local_data = {value: selected[positions] for value, selected in data_membership.items()}
        local_calls = {value: selected[positions] for value, selected in call_membership.items()}
        seen = set()
        for data, call in product(("",) + DATA_VALUES, ("",) + CALL_VALUES):
            mask = parent_mask
            filters = {"filter_current_tariff": key[0], "filter_arpu_segment": key[1]}
            if data:
                mask &= data_masks[data]
                filters["filter_data_segment"] = data
            if call:
                mask &= call_masks[call]
                filters["filter_call_segment"] = call
            n = mask.bit_count()
            if not 0 < n <= 5000:
                continue
            if mask in seen:
                continue
            seen.add(mask)
            selected = np.ones(len(positions), dtype=bool)
            if data:
                selected &= local_data[data]
            if call:
                selected &= local_calls[call]
            sorted_arpu = np.sort(values[selected])[::-1]
            prefix = np.concatenate(([0.0], np.cumsum(sorted_arpu)))
            segments.append(Segment(key + (data, call), filters, mask, n, float(prefix[-1]), tuple(prefix.tolist())))
    selected_targets = {}
    for key, cell in sorted(cells.items()):
        if cell.n < 10:
            continue
        order = sorted((target for target in tariffs if target != key[0]),
                       key=lambda target: (-ranks.get((key[0], target), (0, 0))[0],
                                           -ranks.get((key[0], target), (0, 0))[1], target))
        if not order:
            continue
        primary = order[0]
        alternative = next((t for t in alternatives.get("|".join(key), []) if t in tariffs and t not in (key[0], primary)),
                           order[1] if len(order) > 1 else None)
        selected_targets[key] = (primary, alternative)

    def priority(arm: ArmKey):
        cell = cells[arm[:2]]
        return (-cell.arpu_sum * ranks.get((arm[0], arm[2]), (0, 0))[0], -cell.arpu_sum, *arm)

    primary_arms = [key + (targets[0],) for key, targets in selected_targets.items()]
    priorities = valid_priority_arms(raw_priorities, cells, tariffs)
    queue = []

    def add(arm):
        if arm is not None and arm not in queue and len(queue) < 14:
            queue.append(arm)

    for arpu in ARPU_VALUES:
        candidates = [arm for arm in primary_arms if arm[1] == arpu]
        prioritized = next((arm for arm in priorities if arm[1] == arpu), None)
        if prioritized is not None:
            add(prioritized)
        elif candidates:
            add(min(candidates, key=priority))
        else:
            flags.append(f"missing_coverage:{arpu}")
    for arm in priorities:
        add(arm)
    for primary in sorted(primary_arms, key=priority)[:4]:
        alternative = selected_targets[primary[:2]][1]
        if alternative:
            add(primary[:2] + (alternative,))
    all_arms = [key + (target,) for key, targets in selected_targets.items() for target in targets if target]
    for arm in sorted(all_arms, key=priority):
        add(arm)
    return Domain(profile, tariffs, channels, cells, segments, queue, policy_hash, flags, source)
