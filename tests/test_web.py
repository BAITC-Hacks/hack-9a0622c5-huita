import asyncio

from fastapi.testclient import TestClient

from beesmart.api.body_limit import BodyLimitMiddleware
from beesmart.config import Settings
from beesmart.api.app import create_app


def test_overview_security_and_downloads(tmp_path):
    with TestClient(create_app(Settings(root=tmp_path)), client=("127.0.0.1", 50000)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert "frame-ancestors 'none'" in health.headers["content-security-policy"]
        overview = client.get("/api/overview")
        assert overview.status_code == 200
        assert overview.json()["limits"]["max_total_contacts"] == 15000
        assert overview.json()["dataset"]["status"] == "missing"
        assert client.get("/api/data/not-a-source").status_code == 404
        assert client.get("/api/runs/not-a-uuid").status_code == 422
        assert client.post("/api/runs", json={"seed": 42}).status_code == 403
        headers = {"X-BeeSmart-Request": "1"}
        assert client.post("/api/runs", json={"seed": -1}, headers=headers).status_code == 422
        assert client.post("/api/runs", json={"seed": True}, headers=headers).status_code == 422
        assert client.post("/api/runs", json={"seed": 42}, headers={**headers, "Origin": "https://evil.example"}).status_code == 403
        assert client.post("/api/runs", content=b"x" * 5000, headers=headers).status_code == 413


def test_remote_client_denied(tmp_path):
    with TestClient(create_app(Settings(root=tmp_path)), client=("192.0.2.10", 50000)) as client:
        assert client.get("/api/overview").status_code == 403


def test_missing_files_are_not_presented_as_ready(tmp_path):
    (tmp_path / "static").mkdir()
    with TestClient(create_app(Settings(root=tmp_path)), client=("127.0.0.1", 50000)) as client:
        overview = client.get("/api/overview").json()
        assert overview["dataset"]["status"] == "missing"
        assert not overview["runtime"]["ready"]
        response = client.post("/api/runs", json={"seed": 42}, headers={"X-BeeSmart-Request": "1"})
        assert response.status_code == 409
        assert client.get("/").json()["status"] == "ok"


def test_body_limit_checks_actual_chunked_bytes():
    async def scenario():
        entered = False
        messages = iter([
            {"type": "http.request", "body": b"a" * 3000, "more_body": True},
            {"type": "http.request", "body": b"b" * 3000, "more_body": False},
        ])
        sent = []

        async def downstream(scope, receive, send):
            nonlocal entered
            entered = True

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        await BodyLimitMiddleware(downstream)(
            {"type": "http", "method": "POST", "headers": []}, receive, send,
        )
        assert not entered
        assert sent[0]["status"] == 413

    asyncio.run(scenario())
