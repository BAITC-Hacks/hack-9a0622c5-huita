"""Remote API boundaries and persistence, using generated data and credentials."""

import json
import secrets
import shutil
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from beesmart.application.contracts import OverviewResponse, RunRecord
from beesmart.config import LOCAL_HOSTS, Settings
from beesmart.api.app import create_app


ROOT = Path(__file__).resolve().parents[1]
API_ORIGIN = "https://api.example.test"
APP_ORIGIN = "https://app.example.test"


@pytest.fixture
def production_settings(tmp_path):
    return Settings(
        root=tmp_path / "code", environment="production",
        api_token=secrets.token_urlsafe(32), allowed_hosts=("api.example.test",),
        allowed_origins=(APP_ORIGIN,), storage_dir=tmp_path / "private-storage",
        data_dir=tmp_path / "organizer-input",
    )


def remote_client(settings):
    return TestClient(create_app(settings), base_url=API_ORIGIN,
                      client=("192.0.2.10", 50000))


def authorization(settings):
    return {"Authorization": f"Bearer {settings.api_token}"}


def test_public_schema_and_health_describe_protected_typed_api(production_settings):
    with remote_client(production_settings) as client:
        health = client.get("/api/health")
        assert health.status_code == 200 and health.json() == {"status": "ok"}
        assert health.headers["strict-transport-security"] == "max-age=31536000"
        assert "frame-ancestors 'none'" in health.headers["content-security-policy"]
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert client.get("/open.json").json() == schema
        assert not schema["paths"]["/api/health"]["get"].get("security")
        assert schema["components"]["securitySchemes"]["BeeSmartToken"]["scheme"] == "bearer"
        start = schema["paths"]["/api/runs"]["post"]
        assert start["security"] == [{"BeeSmartToken": []}]
        assert start["responses"]["202"]["content"]["application/json"]["schema"]["$ref"].endswith("/RunRecord")
        assert any(item["in"] == "header" and item["name"] == "x-beesmart-request"
                   and item["required"] for item in start["parameters"])
        assert schema["components"]["schemas"]["Campaign"]["properties"]["channel"]["enum"] == [
            "push", "sms", "digital_ads", "call",
        ]


@pytest.mark.parametrize("path", [
    "/api/overview", "/api/agent", "/api/runs/latest",
    "/api/data/customer_profile",
    "/api/runs/00000000-0000-0000-0000-000000000000/submission.csv",
    "/api/runs/00000000-0000-0000-0000-000000000000/report.json",
])
def test_private_data_requires_bearer_before_lookup(production_settings, path):
    with remote_client(production_settings) as client:
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert response.headers["cache-control"] == "no-store"
        assert set(response.json()) == {"detail"}


def test_authorized_remote_access_and_http_rejection(production_settings):
    with remote_client(production_settings) as client:
        headers = authorization(production_settings)
        overview = client.get("/api/overview", headers=headers)
        assert overview.status_code == 200
        assert OverviewResponse.model_validate(overview.json()).dataset.status == "missing"
        agent = client.get("/api/agent", headers=headers)
        assert agent.status_code == 200
        assert agent.json() == {
            "engine": "local_python", "model": None, "llm_calls": False,
            "evaluation": "organizer_mock", "paid_calls": False,
            "provider": "local", "ready": True, "status": "disabled", "budget_usd": 5.0,
            "estimated_spend_usd": 0.0, "reserved_usd": 0.0,
            "request_attempts": 0, "completed_requests": 0,
        }
        assert client.get("http://api.example.test/api/overview", headers=headers).status_code == 400
        assert client.get("https://wrong.example.test/api/overview", headers=headers).status_code == 400


def test_cross_origin_preflight_and_mutation_protection(production_settings):
    with remote_client(production_settings) as client:
        preflight = client.options("/api/uploads/run", headers={
            "Origin": APP_ORIGIN, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,x-beesmart-request",
        })
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == APP_ORIGIN
        assert "POST" in preflight.headers["access-control-allow-methods"]
        assert "access-control-allow-credentials" not in preflight.headers
        rejected = client.options("/api/uploads/run", headers={
            "Origin": "https://evil.example.test", "Access-Control-Request-Method": "POST",
        })
        assert rejected.status_code == 400
        assert "access-control-allow-origin" not in rejected.headers
        headers = {**authorization(production_settings), "X-BeeSmart-Request": "1"}
        assert client.post("/api/runs", json={"seed": 42},
                           headers={**headers, "Origin": "https://evil.example.test"}).status_code == 403
        assert client.post("/api/runs", json={"seed": 42},
                           headers=authorization(production_settings)).status_code == 403
        response = client.post("/api/runs", json={"seed": -1},
                               headers={**headers, "Origin": APP_ORIGIN})
        assert response.status_code == 422
        assert isinstance(response.json()["detail"], str)
        assert response.headers["access-control-allow-origin"] == APP_ORIGIN


def test_authentication_failure_rate_limit(production_settings):
    with remote_client(production_settings) as client:
        for _ in range(20):
            assert client.get("/api/overview").status_code == 401
        limited = client.get("/api/overview")
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "60"
        # Liveness is available even while the client's authentication is throttled.
        assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize("override", [
    {"api_token": ""},
    {"allowed_hosts": LOCAL_HOSTS},
    {"allowed_hosts": ("*",)},
    {"allowed_origins": ("http://app.example.test",)},
    {"forwarded_allow_ips": "*"},
])
def test_production_rejects_unsafe_settings(production_settings, override):
    with pytest.raises(ValueError):
        replace(production_settings, **override)


def test_storage_has_one_owner_and_releases_after_shutdown(production_settings):
    with remote_client(production_settings):
        with pytest.raises(RuntimeError, match="Storage is already in use"):
            with remote_client(production_settings):
                pytest.fail("A second API process must not share the same run storage")
    with remote_client(production_settings) as client:
        assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize("interrupted_status", ["queued", "running"])
def test_interrupted_run_recovers_as_failed(production_settings, interrupted_status):
    run_id = str(uuid4())
    record = {
        "id": run_id, "status": interrupted_status, "seed": 42,
        "created_at": "2026-01-01T00:00:00+00:00", "finished_at": None,
        "duration_seconds": None, "error": None, "campaigns": [], "metrics": None,
        "events": [], "diagnostics": {}, "dataset": {"source": "bundled"},
    }
    folder = production_settings.storage_path / "runs"
    folder.mkdir(parents=True)
    path = folder / f"{run_id}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    with remote_client(production_settings) as client:
        response = client.get(f"/api/runs/{run_id}", headers=authorization(production_settings))
        assert response.status_code == 200
        recovered = RunRecord.model_validate(response.json())
        assert recovered.status == "failed"
        assert recovered.error and recovered.finished_at
        assert client.get("/api/runs/latest", headers=authorization(production_settings)).json()["id"] == run_id
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["status"] == "failed" and saved["finished_at"]


def test_authenticated_upload_worker_uses_external_storage(production_settings, synthetic_frames):
    code_root = production_settings.root
    code_root.mkdir()
    shutil.copytree(ROOT / "beesmart", code_root / "beesmart", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "organizer", code_root / "organizer", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "policies", code_root / "policies")
    shutil.copy2(ROOT / "agent.py", code_root / "agent.py")
    files = {role: (f"{role}.csv", frame.to_csv(index=False).encode(), "text/csv")
             for role, frame in synthetic_frames.items()}
    with remote_client(production_settings) as client:
        headers = {**authorization(production_settings), "X-BeeSmart-Request": "1", "Origin": APP_ORIGIN}
        response = client.post("/api/uploads/run", files=files, data={"seed": "42"}, headers=headers)
        assert response.status_code == 202, response.text
        record = response.json()
        RunRecord.model_validate(record)
        assert record["dataset"]["source"] == "uploaded"
        deadline = time.monotonic() + 20
        while record["status"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(0.05)
            response = client.get(f"/api/runs/{record['id']}", headers=headers)
            assert response.status_code == 200, response.text
            record = response.json()
        assert record["status"] == "completed", record.get("error")
        RunRecord.model_validate(record)
        assert record["metrics"]["baseline_total_arpu"] == pytest.approx(synthetic_frames["profile"]["predicted_arpu"].sum())
        assert 1 <= len(record["campaigns"]) <= 10 and record["metrics"]["n_pilots"] > 0
        result = client.get(f"/api/runs/{record['id']}/submission.csv", headers=headers)
        assert result.status_code == 200 and result.text.startswith("campaign_name,")
        assert result.headers["content-disposition"].startswith("attachment;")
        assert "Content-Disposition" in result.headers["access-control-expose-headers"]
        assert client.get(f"/api/runs/{record['id']}/report.json", headers=headers).json()["dataset"] == record["dataset"]
        assert client.get(f"/api/runs/{record['id']}/report.json").status_code == 401
    stored = production_settings.storage_path
    assert (stored / "datasets" / record["dataset"]["id"] / "customer_profile.csv").is_file()
    assert json.loads((stored / "runs" / f"{record['id']}.json").read_text())["status"] == "completed"
    assert not (code_root / "work").exists()
    assert not (code_root / "customer_profile.csv").exists()
