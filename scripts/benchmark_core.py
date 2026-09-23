"""Reproducible local benchmark; timings cover Agent.act, not CSV/env setup.

Examples:
  python scripts/benchmark_core.py --phase before --runs 7
  python scripts/benchmark_core.py --phase after --runs 7
The ignored JSON report retains both phases and compares every seed's decisions.
"""

import argparse
import cProfile
import hashlib
import json
import platform
import pstats
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent import Agent
from mock_environment import make_mock_env


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def run_once(data_dir: Path, policy_path: Path, seed: int, profiler=None):
    env, _ = make_mock_env(seed=seed, data_dir=str(data_dir / "data"), profile_path=str(data_dir / "customer_profile.csv"))
    agent = Agent(history_path=data_dir / "data/change_tariff.csv", policy_path=policy_path)
    if profiler:
        profiler.enable()
    started = time.perf_counter()
    campaigns = agent.act(env)
    elapsed = time.perf_counter() - started
    if profiler:
        profiler.disable()
    diagnostics = agent.diagnostics
    semantic = {"campaigns": campaigns, "pilots": diagnostics["pilots"], "estimates": diagnostics["estimates"],
                "validation": diagnostics["validation"], "flags": diagnostics["flags"],
                "gain_low": diagnostics["gain_low"], "final_cost": diagnostics["final_cost"],
                "final_contacts": diagnostics["final_contacts"]}
    return elapsed, {"campaigns": campaigns, "semantic_hash": canonical_hash(semantic),
                     "plan_hash": canonical_hash(campaigns), "pilots": diagnostics["n_pilots"],
                     "validation": diagnostics["validation"], "flags": diagnostics["flags"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--data-dir", type=Path, default=ROOT)
    parser.add_argument("--policy", type=Path, default=ROOT / "frozen_policy.json")
    parser.add_argument("--output", type=Path, default=ROOT / "work/performance-core.json")
    args = parser.parse_args()
    if args.runs < 5:
        parser.error("At least five runs are required for a median comparison")
    data_dir = args.data_dir.resolve()
    needed = [data_dir / "customer_profile.csv", data_dir / "data/change_tariff.csv", data_dir / "data/dict_tariff.csv", args.policy]
    if not all(path.is_file() for path in needed):
        parser.error("Supply local participant data with --data-dir; datasets are never downloaded or committed")
    inputs = {str(path.relative_to(data_dir)) if path.is_relative_to(data_dir) else path.name: digest(path) for path in needed}
    # Warm up imports and pandas dispatch separately from the measured series.
    run_once(data_dir, args.policy, 42)
    timings = [run_once(data_dir, args.policy, 42)[0] for _ in range(args.runs)]
    seeds = {str(seed): run_once(data_dir, args.policy, seed)[1] for seed in range(10)}
    profiler = cProfile.Profile()
    run_once(data_dir, args.policy, 42, profiler)
    stats = pstats.Stats(profiler)
    functions = []
    for (filename, line, function), (primitive, total, own, cumulative, _) in stats.stats.items():
        if "/beesmart/core/" in filename or filename.endswith("environment.py"):
            functions.append({"function": f"{Path(filename).name}:{line}:{function}", "calls": total,
                              "own_seconds": own, "cumulative_seconds": cumulative})
    phase = {"python": platform.python_version(), "platform": platform.platform(), "runs": args.runs,
             "measurement": "Agent.act only; environment creation and CSV setup excluded; no LLM calls",
             "inputs_sha256": inputs,
             "core_sha256": {path.name: digest(path) for path in sorted((ROOT / "beesmart/core").glob("*.py"))},
             "timings_seconds": timings, "median_seconds": statistics.median(timings),
             "minimum_seconds": min(timings), "maximum_seconds": max(timings),
             "seeds": seeds, "profile_top": sorted(functions, key=lambda row: -row["cumulative_seconds"])[:25]}
    report = json.loads(args.output.read_text()) if args.output.exists() else {}
    if args.phase in report:
        report.setdefault("previous_measurements", []).append({"phase": args.phase, **report[args.phase]})
    report[args.phase] = phase
    if "before" in report and "after" in report:
        before, after = report["before"], report["after"]
        comparison = {"same_inputs": before["inputs_sha256"] == after["inputs_sha256"],
                      "all_plans_equal": all(before["seeds"][str(seed)]["plan_hash"] == after["seeds"][str(seed)]["plan_hash"] for seed in range(10)),
                      "all_decisions_equal": all(before["seeds"][str(seed)]["semantic_hash"] == after["seeds"][str(seed)]["semantic_hash"] for seed in range(10)),
                      "median_speedup": before["median_seconds"] / after["median_seconds"]}
        report["comparison"] = comparison
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"phase": args.phase, "median_seconds": phase["median_seconds"],
                      "profile_top": phase["profile_top"][:7], "comparison": report.get("comparison"),
                      "report": str(args.output)}, ensure_ascii=False, indent=2))
    if args.phase == "after" and "comparison" in report and not all(report["comparison"][key] for key in ("same_inputs", "all_plans_equal", "all_decisions_equal")):
        raise SystemExit("Semantic or input regression: inspect benchmark report")


if __name__ == "__main__":
    main()
