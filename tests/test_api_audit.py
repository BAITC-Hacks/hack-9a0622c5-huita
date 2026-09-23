"""Regression checks for malformed persisted reports and server-installed data."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from beesmart.config import Settings
from beesmart.web import create_app


ROOT = Path(__file__).resolve().parents[1]


def source_settings(tmp_path, frames):
    data = tmp_path / "source"
    (data / "data").mkdir(parents=True)
    for role, filename in (("profile", "customer_profile.csv"), ("history", "data/change_tariff.csv"),
                           ("tariffs", "data/dict_tariff.csv")):
        frames[role].to_csv(data / filename, index=False)
    return Settings(root=ROOT, data_dir=data, storage_dir=tmp_path / "state")


@pytest.mark.parametrize("broken", ["history", "category", "price"])
def test_invalid_server_dataset_is_not_ready_and_never_500(tmp_path, synthetic_frames, broken):
    if broken == "history":
        synthetic_frames["history"] = synthetic_frames["history"].drop(columns="AVG_ARPU_NEXT_3M")
    elif broken == "category":
        synthetic_frames["profile"]["arpu_segment"] = "UNKNOWN"
    else:
        synthetic_frames["tariffs"]["price_tariff"] = "not a price"
    settings = source_settings(tmp_path, synthetic_frames)
    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        response = client.get("/api/overview")
        assert response.status_code == 200
        assert response.json()["dataset"]["status"] == "error"
        assert not response.json()["runtime"]["ready"]
        started = client.post("/api/runs", json={"seed": 42}, headers={"X-BeeSmart-Request": "1"})
        assert started.status_code == 409
        assert client.get("/api/runs/latest").status_code == 404


def test_truncated_report_does_not_poison_latest_or_restart(tmp_path):
    settings = Settings(root=ROOT, storage_dir=tmp_path)
    directory = tmp_path / "runs"
    directory.mkdir()
    good_id, bad_id = str(uuid4()), str(uuid4())
    good = {"id": good_id, "status": "failed", "seed": 42, "created_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00", "duration_seconds": 1.0,
            "error": "Test failure", "dataset": {"source": "bundled"}, "events": [],
            "campaigns": [], "metrics": None, "diagnostics": {}}
    (directory / f"{good_id}.json").write_text(json.dumps(good))
    (directory / f"{bad_id}.json").write_text(json.dumps({"id": bad_id, "status": "completed"}))
    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        latest = client.get("/api/runs/latest")
        assert latest.status_code == 200
        assert latest.json()["id"] == good_id
        assert client.get(f"/api/runs/{bad_id}").status_code == 404


def test_cached_overview_invalidates_after_fixing_a_file(tmp_path, synthetic_frames):
    settings = source_settings(tmp_path, synthetic_frames)
    history_path = settings.data_path / "data/change_tariff.csv"
    valid_history = history_path.read_bytes()
    history_path.write_bytes(b"wrong,header\n1,2\n")
    with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
        assert not client.get("/api/overview").json()["runtime"]["ready"]
        history_path.write_bytes(valid_history)
        assert client.get("/api/overview").json()["runtime"]["ready"]
