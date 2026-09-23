import asyncio
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from beesmart.config import Settings
from beesmart.runs import RunBusyError, RunManager
from beesmart.web import create_app


ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"X-BeeSmart-Request": "1"}


@pytest.fixture
def upload_app(tmp_path):
    (tmp_path / "static").mkdir()
    shutil.copytree(ROOT / "beesmart", tmp_path / "beesmart", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("agent.py", "environment.py", "mock_environment.py", "scoring_core.py",
                 "local_eval.py", "make_submission.py", "frozen_policy.json"):
        shutil.copy2(ROOT / name, tmp_path / name)
    return create_app(Settings(root=tmp_path))


def source_files(synthetic_frames, profile=None):
    return {
        "profile": ("../../agent.py", synthetic_frames["profile"].to_csv(index=False).encode()
                    if profile is None else profile, "text/csv"),
        "history": ("history.csv", synthetic_frames["history"].to_csv(index=False).encode(), "text/csv"),
        "tariffs": ("tariffs.csv", synthetic_frames["tariffs"].to_csv(index=False).encode(), "text/csv"),
    }


def test_upload_runs_on_uploaded_profile_and_exports_result(upload_app, tmp_path, synthetic_frames):
    profile = synthetic_frames["profile"].copy()
    original_baseline = profile["predicted_arpu"].sum()
    profile["predicted_arpu"] *= 0.5
    payload = profile.to_csv(index=False).encode()
    original_agent = (tmp_path / "agent.py").read_bytes()
    with TestClient(upload_app, client=("127.0.0.1", 50000)) as client:
        # No bundled data exists in this app; uploaded CSV must be sufficient.
        response = client.post("/api/uploads/run", files=source_files(synthetic_frames, payload),
                               data={"seed": "42"}, headers=HEADERS)
        assert response.status_code == 202, response.text
        record = response.json()
        assert record["dataset"]["source"] == "uploaded"
        assert record["dataset"]["customers"] == len(profile)
        deadline = time.monotonic() + 20
        while record["status"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(0.05)
            record = client.get(f"/api/runs/{record['id']}").json()
        assert record["status"] == "completed", record.get("error")
        assert record["metrics"]["baseline_total_arpu"] == pytest.approx(profile["predicted_arpu"].sum())
        assert record["metrics"]["baseline_total_arpu"] == pytest.approx(original_baseline * 0.5)
        assert 1 <= len(record["campaigns"]) <= 10
        assert record["metrics"]["n_pilots"] > 0
        assert record["events"]
        assert client.get(f"/api/runs/{record['id']}/submission.csv").text.startswith("campaign_name,")
        assert client.get(f"/api/runs/{record['id']}/report.json").json()["dataset"] == record["dataset"]
    assert (tmp_path / "agent.py").read_bytes() == original_agent
    assert (tmp_path / "work/datasets" / record["dataset"]["id"] / "customer_profile.csv").read_bytes() == payload
    assert not (tmp_path / "customer_profile.csv").exists()


def test_upload_rejects_invalid_inputs_and_releases_reservation(upload_app, tmp_path, monkeypatch, synthetic_frames):
    with TestClient(upload_app, client=("127.0.0.1", 50000)) as client:
        assert client.post("/api/uploads/run", json={}, headers=HEADERS).status_code == 415
        files = {"profile": ("a.csv", b"bad,data\n1,2\n")}
        assert client.post("/api/uploads/run", files=files, headers=HEADERS).status_code == 422
        assert client.post("/api/uploads/run", files=source_files(synthetic_frames, b"bad,data\n1,2\n"),
                           headers=HEADERS).status_code == 422
        assert client.post("/api/uploads/run", files=source_files(synthetic_frames),
                           data={"seed": "-1"}, headers=HEADERS).status_code == 422
        assert client.post("/api/uploads/run", files=source_files(synthetic_frames),
                           headers={**HEADERS, "Origin": "https://evil.example"}).status_code == 403
        import beesmart.upload_form as parser
        monkeypatch.setattr(parser, "MAX_UPLOAD_BYTES", 64)
        # No Content-Length: the streamed byte guard must still reject the body.
        multipart = (b'--test\r\nContent-Disposition: form-data; name="profile"; filename="x.csv"\r\n'
                     b'\r\n' + b'a' * 128 + b'\r\n--test--\r\n')
        response = client.post("/api/uploads/run", content=iter([multipart[:50], multipart[50:]]),
                               headers={**HEADERS, "Content-Type": "multipart/form-data; boundary=test"})
        assert response.status_code == 413
        assert not upload_app.state.runs._upload_reserved
        assert not list((tmp_path / "work/datasets").glob("*/customer_profile.csv"))


def test_reservation_blocks_parallel_runs_and_uploads(tmp_path):
    async def scenario():
        manager = RunManager(Settings(root=tmp_path))
        manager.reserve_upload()
        with pytest.raises(RunBusyError):
            manager.reserve_upload()
        with pytest.raises(RunBusyError):
            await manager.start(42)

        async def blocked(record):
            await asyncio.Event().wait()

        manager._execute = blocked
        await manager.start(42, dataset={"id": "unused"}, from_upload=True)
        manager.release_upload()
        with pytest.raises(RunBusyError):
            manager.reserve_upload()
        with pytest.raises(RunBusyError):
            await manager.start(43)
        await manager.close()
        manager.reserve_upload()
        manager.release_upload()

    asyncio.run(scenario())
