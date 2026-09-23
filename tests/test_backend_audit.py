"""Regressions found during the backend audit; no actual provider requests."""

import asyncio
from contextlib import asynccontextmanager, suppress
import io
import json
from pathlib import Path
import secrets
import threading
from types import SimpleNamespace

import pandas as pd
import pytest
from starlette.requests import Request

from beesmart.agent import intelligence
from beesmart.config import Settings
from beesmart.agent.intelligence import IntelligenceService
from beesmart.agent.llm import LLMError, PlanningContext, PolicyResponse
from beesmart.application.runs import RunManager
from beesmart.application.uploads import UploadStore, UploadValidationError
from beesmart.api.app import create_app


ROOT = Path(__file__).resolve().parents[1]


def test_corrupt_ledger_cannot_overflow_public_spend_or_reset_budget(tmp_path, monkeypatch):
    folder = tmp_path / "work/llm"
    folder.mkdir(parents=True)
    ledger = {"version": 1, "estimated_micro_usd": 10 ** 1000,
              "reserved_micro_usd": 0, "attempts": 1, "completed": 1}
    before = json.dumps(ledger)
    (folder / "spend.json").write_text(before)

    def forbidden(*_args, **_kwargs):
        pytest.fail("Corrupt spend must block both context preparation and network")

    monkeypatch.setattr(intelligence, "build_context", forbidden)
    monkeypatch.setattr(intelligence, "request_policy", forbidden)
    settings = Settings(root=tmp_path, agent_provider="openai", openai_api_key=secrets.token_urlsafe(40))
    service = IntelligenceService(settings)
    assert service.info()["status"] == "storage_error"
    with pytest.raises(LLMError) as error:
        asyncio.run(service.prepare(tmp_path, service.initial_record()))
    assert error.value.code == "budget_storage"
    assert (folder / "spend.json").read_text() == before


@pytest.mark.parametrize("blocked_write", [1, 2])
def test_llm_disk_writes_leave_loop_live_and_finish_before_repeated_cancel(tmp_path, monkeypatch, blocked_write):
    context = PlanningContext({}, {"h001": ("tariff_a", "LOW", "tariff_b")}, "audit-context")
    policy = {"schema_version": 1, "source": "openai", "alternative_targets_by_cell": {},
              "rationales_by_cell": {}, "priority_arms": [["tariff_a", "LOW", "tariff_b"]]}
    entered, release = threading.Event(), threading.Event()
    actual_write = intelligence.private_json
    writes, requests = [], []

    def gated_write(path, value):
        writes.append(path)
        if len(writes) == blocked_write:
            entered.set()
            if not release.wait(2):
                raise AssertionError("The event loop was blocked by storage I/O")
        actual_write(path, value)

    async def fake_request(*args, **kwargs):
        requests.append(True)
        return PolicyResponse(policy, "Проверим локальным пилотом.", 100, 50)

    monkeypatch.setattr(intelligence, "private_json", gated_write)
    monkeypatch.setattr(intelligence, "build_context", lambda _: context)
    monkeypatch.setattr(intelligence, "request_policy", fake_request)
    settings = Settings(root=tmp_path, agent_provider="openai", openai_api_key=secrets.token_urlsafe(40))
    service = IntelligenceService(settings)

    async def scenario():
        metadata = service.initial_record()
        task = asyncio.create_task(service.prepare(tmp_path, metadata))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert service._lock.locked()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not service._lock.locked()
        restarted = IntelligenceService(settings)
        if blocked_write == 1:
            assert not requests
            assert restarted.info()["reserved_usd"] == .02
            assert metadata["reserved_usd"] == .02
            assert restarted.info()["completed_requests"] == 0
        else:
            assert len(requests) == 1
            assert restarted.info()["reserved_usd"] == 0
            assert restarted.info()["completed_requests"] == 1
            assert metadata["reserved_usd"] == 0 and metadata["status"] == "completed"
            cached = restarted.initial_record()
            assert (await restarted.prepare(tmp_path, cached)).is_file()
            assert cached["cache_hit"] and len(requests) == 1

    asyncio.run(scenario())


def test_repeated_upload_cancel_keeps_files_and_reservation_until_store_finishes(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    streams = {role: io.BytesIO(b"fabricated CSV") for role in ("profile", "history", "tariffs")}
    runs = SimpleNamespace(reserved=False, _finish_persistence=RunManager._finish_persistence)
    facts = {"discarded": False, "files_open_in_thread": False}

    def reserve():
        runs.reserved = True

    def unreserve():
        runs.reserved = False

    async def start(*_args, **_kwargs):
        pytest.fail("An interrupted upload must not start a worker")

    runs.reserve_upload, runs.release_upload, runs.start = reserve, unreserve, start

    def create(files):
        entered.set()
        if not release.wait(2):
            raise AssertionError("Saving thread did not receive its release signal")
        facts["files_open_in_thread"] = all(not stream.closed for stream in files.values())
        return {"id": "a" * 32}

    def discard(_identifier):
        assert runs.reserved
        assert all(not stream.closed for stream in streams.values())
        facts["discarded"] = True

    @asynccontextmanager
    async def fake_form(_request):
        try:
            yield {role: SimpleNamespace(file=stream) for role, stream in streams.items()}, 42
        finally:
            for stream in streams.values():
                stream.close()

    monkeypatch.setattr("beesmart.api.app.upload_form", fake_form)
    app = create_app(Settings(root=ROOT, storage_dir=tmp_path))
    app.state.runs = runs
    app.state.uploads = SimpleNamespace(create=create, discard=discard)
    routes = [child for route in app.routes
              for child in getattr(getattr(route, "original_router", None), "routes", [route])]
    endpoint = next(route.endpoint for route in routes if getattr(route, "path", None) == "/api/uploads/run")
    request = Request({"type": "http", "method": "POST", "path": "/api/uploads/run", "headers": [], "app": app})

    async def scenario():
        task = asyncio.create_task(endpoint(request))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert runs.reserved
            assert all(not stream.closed for stream in streams.values())
        finally:
            release.set()
            with suppress(asyncio.CancelledError):
                await task
        assert facts == {"discarded": True, "files_open_in_thread": True}
        assert not runs.reserved
        assert all(stream.closed for stream in streams.values())

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [2_000_000_000_000_000_000, 1e307, 1e306, 1e-320])
def test_uploaded_baseline_rejects_arithmetic_overflow(tmp_path, synthetic_frames, value):
    frames = {role: frame.copy() for role, frame in synthetic_frames.items()}
    frames["profile"] = frames["profile"].iloc[:12].copy()
    frames["profile"]["predicted_arpu"] = value
    payloads = {role: io.BytesIO(frame.to_csv(index=False).encode()) for role, frame in frames.items()}
    with pytest.raises(UploadValidationError, match="суммарный predicted_arpu"):
        UploadStore(tmp_path / "datasets").create(payloads)
    assert not list((tmp_path / "datasets").iterdir())


@pytest.mark.parametrize("value,baseline", [(100_000_000, 1_200_000_000), (.001, .012)])
def test_representable_baseline_is_preserved(tmp_path, synthetic_frames, value, baseline):
    frames = {role: frame.copy() for role, frame in synthetic_frames.items()}
    frames["profile"] = frames["profile"].iloc[:12].copy()
    frames["profile"]["predicted_arpu"] = value
    payloads = {role: io.BytesIO(frame.to_csv(index=False).encode()) for role, frame in frames.items()}
    store = UploadStore(tmp_path / "datasets")
    result = store.create(payloads)
    profile = pd.read_csv(store.path(result["id"]) / "customer_profile.csv")
    assert profile["predicted_arpu"].sum() == pytest.approx(baseline)


def test_integer_history_change_cannot_wrap_loss_into_positive_effect(tmp_path, synthetic_frames):
    frames = {role: frame.copy() for role, frame in synthetic_frames.items()}
    for name in ("AVG_ARPU_PREV_3M", "AVG_ARPU_NEXT_3M"):
        frames["history"][name] = frames["history"][name].astype("int64")
    frames["history"].loc[0, "AVG_ARPU_PREV_3M"] = 8_000_000_000_000_000_000
    frames["history"].loc[0, "AVG_ARPU_NEXT_3M"] = -8_000_000_000_000_000_000
    payloads = {role: io.BytesIO(frame.to_csv(index=False).encode()) for role, frame in frames.items()}
    with pytest.raises(UploadValidationError, match="разность ARPU"):
        UploadStore(tmp_path / "datasets").create(payloads)
    assert not list((tmp_path / "datasets").iterdir())
