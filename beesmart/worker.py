"""Separate evaluation process. Only the official runner can see mock internals."""
import contextlib
import argparse
import json
import os
import sys
from pathlib import Path

from beesmart.serialization import json_safe


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    parser = argparse.ArgumentParser()
    parser.add_argument("seed", type=int)
    parser.add_argument("--data-dir", type=Path, default=root)
    parser.add_argument("--policy-path", type=Path, default=root / "frozen_policy.json")
    args = parser.parse_args()
    seed = args.seed
    # This path is supplied only by the server, never accepted from an HTTP request.
    dataset_root = args.data_dir.resolve()
    protocol_stdout = sys.stdout

    def send(kind: str, data: dict) -> None:
        protocol_stdout.write(json.dumps(
            {"type": kind, "data": json_safe(data)}, ensure_ascii=False, allow_nan=False,
        ) + "\n")
        protocol_stdout.flush()

    def event_sink(name: str, payload: dict) -> None:
        send("event", {"event": name, "data": payload})

    with contextlib.redirect_stdout(sys.stderr):
        from agent import Agent
        from local_eval import evaluate_agent
        # Official readers use relative paths; only this isolated process changes CWD.
        os.chdir(dataset_root)

        class RecordingAgent:
            def __init__(self):
                self.delegate = Agent(event_sink=event_sink,
                                      history_path=dataset_root / "data" / "change_tariff.csv",
                                      policy_path=args.policy_path.resolve())
                self.campaigns = []

            def act(self, env):
                self.campaigns = self.delegate.act(env)
                return self.campaigns

        agent = RecordingAgent()
        result = evaluate_agent(agent, seed=seed, verbose=False)
        if not result or not 1 <= len(agent.campaigns) <= 10 or result["n_pilots"] < 1:
            raise RuntimeError("Agent did not meet mandatory output requirements")
        result["n_final_campaigns"] = len(agent.campaigns)
        send("result", {
            "campaigns": agent.campaigns, "metrics": result,
            "diagnostics": getattr(agent.delegate, "diagnostics", {}),
        })


if __name__ == "__main__":
    main()
