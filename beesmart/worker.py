"""Separate evaluation process. Only the official runner can see mock internals."""
import contextlib
import json
import sys
from pathlib import Path

from beesmart.serialization import json_safe


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root))
    seed = int(sys.argv[1])
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

        class RecordingAgent:
            def __init__(self):
                self.delegate = Agent(event_sink=event_sink)
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

