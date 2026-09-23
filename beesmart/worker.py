"""Separate evaluation process. Only the official runner can see mock internals."""
import contextlib
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from beesmart.serialization import json_safe


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    seed = int(sys.argv[1])
    dataset_root = root
    if len(sys.argv) > 2:
        dataset_id = UUID(sys.argv[2]).hex
        dataset_root = (root / "work" / "datasets" / dataset_id).resolve()
        if not dataset_root.is_relative_to((root / "work" / "datasets").resolve()):
            raise ValueError("Invalid dataset directory")
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
                                      policy_path=root / "frozen_policy.json")
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
