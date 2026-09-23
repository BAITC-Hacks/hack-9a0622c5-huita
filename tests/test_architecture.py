"""The agent and application must run without importing the HTTP framework."""

import json
import os
from pathlib import Path
import subprocess
import sys

from beesmart.application.storage import StorageLease


ROOT = Path(__file__).resolve().parents[1]


def test_calculation_imports_do_not_depend_on_fastapi_or_uvicorn():
    result = subprocess.run([sys.executable, "-c", """
import sys
from importlib.abc import MetaPathFinder
class NoWebFramework(MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {'fastapi', 'starlette', 'uvicorn'}:
            raise AssertionError('Calculation imported HTTP framework: ' + fullname)
sys.meta_path.insert(0, NoWebFramework())
from agent import Agent
from beesmart.application.__main__ import calculate
from beesmart.application.worker import main
print('independent')
"""], cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "independent"


def test_cli_uses_real_pipeline_and_cannot_share_server_storage(tmp_path, synthetic_frames):
    data, storage = tmp_path / "inputs", tmp_path / "private"
    (data / "data").mkdir(parents=True)
    for role, name in (("profile", "customer_profile.csv"), ("history", "data/change_tariff.csv"),
                       ("tariffs", "data/dict_tariff.csv")):
        synthetic_frames[role].to_csv(data / name, index=False)
    command = [sys.executable, "-m", "beesmart.application", "--data-dir", str(data),
               "--storage-dir", str(storage), "--provider", "local", "--seed", "42"]
    env = {**os.environ, "OPENAI_API_KEY": "", "BEESMART_AGENT_PROVIDER": "local"}
    lease = StorageLease(storage)
    lease.acquire()
    try:
        busy = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)
        assert busy.returncode == 2
        assert "Storage is already in use" in json.loads(busy.stdout)["error"]
        assert not list(storage.glob("runs/*.json"))
    finally:
        lease.release()
    completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr + completed.stdout
    summary = json.loads(completed.stdout)
    assert summary["status"] == "completed"
    assert summary["provider"] == "local" and summary["llm_status"] == "disabled"
    assert summary["pilots"] > 0 and 1 <= summary["campaigns"] <= 10
    report = json.loads(Path(summary["report"]).read_text())
    assert report["diagnostics"]["policy_hash"]
    assert any(event["event"] == "pilot_result" for event in report["events"])
