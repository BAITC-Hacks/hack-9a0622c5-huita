"""Real worker + mocked OpenAI; durable cache and billing failure boundaries."""

import asyncio
import json
import secrets
import stat
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from beesmart.agent import intelligence, llm
from beesmart.config import Settings
from beesmart.agent.intelligence import IntelligenceService
from beesmart.application.runs import RunManager, RunBusyError


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def private_inputs(tmp_path, synthetic_frames):
    data = tmp_path / "source"
    (data / "data").mkdir(parents=True)
    for role, filename in (("profile", "customer_profile.csv"), ("history", "data/change_tariff.csv"),
                           ("tariffs", "data/dict_tariff.csv")):
        synthetic_frames[role].to_csv(data / filename, index=False)
    return Settings(root=ROOT, storage_dir=tmp_path / "storage", data_dir=data,
                    agent_provider="openai", openai_api_key=secrets.token_urlsafe(40))


def mock_openai(monkeypatch):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        context = json.loads(body["input"][0]["content"])
        requests.append(body)
        # Deliberately choose the second-ranked target for every ARPU class.
        ids = [next(c["id"] for c in context["candidates"]
                    if c["arpu_segment"] == segment and c["rank_in_cell"] == 2)
               for segment in ("LOW", "MID", "HIGH")]
        return httpx.Response(200, json={"status": "completed", "usage": {
            "input_tokens": 2000, "output_tokens": 150}, "output": [{"type": "message",
            "role": "assistant", "content": [{"type": "output_text", "text": json.dumps({
                "hypothesis_ids": ids, "summary": "Проверить альтернативные переходы во всех классах ARPU."})}]}]})

    async def request(context, **kwargs):
        return await llm.request_policy(context, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(intelligence, "request_policy", request)
    return requests


def test_cache_survives_restart_and_changed_inputs_replan(private_inputs, monkeypatch):
    calls = mock_openai(monkeypatch)

    async def scenario():
        service = IntelligenceService(private_inputs)
        first = service.initial_record()
        path = await service.prepare(private_inputs.data_path, first)
        assert first["status"] == "completed" and first["estimated_cost_usd"] > 0
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        restarted = IntelligenceService(private_inputs)
        cached = restarted.initial_record()
        assert await restarted.prepare(private_inputs.data_path, cached) == path
        assert cached["status"] == "cached" and cached["cache_hit"]
        assert cached["estimated_cost_usd"] == cached["input_tokens"] == 0
        assert len(calls) == 1
        assert restarted.info()["completed_requests"] == 1
        assert restarted.info()["reserved_usd"] == 0
        assert private_inputs.openai_api_key not in path.read_text()
        tariffs = private_inputs.data_path / "data/dict_tariff.csv"
        tariffs.write_text(tariffs.read_text().replace("800.0", "801.0"))
        changed = restarted.initial_record()
        assert await restarted.prepare(private_inputs.data_path, changed) != path
        assert len(calls) == 2

    asyncio.run(scenario())


def test_exhausted_budget_blocks_network_but_allows_cached_policy(private_inputs, monkeypatch):
    calls = mock_openai(monkeypatch)

    async def scenario():
        service = IntelligenceService(private_inputs)
        path = await service.prepare(private_inputs.data_path, service.initial_record())
        stopped = IntelligenceService(replace(private_inputs, llm_budget_usd=0))
        assert await stopped.prepare(private_inputs.data_path, stopped.initial_record()) == path
        path.unlink()
        with pytest.raises(llm.LLMError) as error:
            await stopped.prepare(private_inputs.data_path, stopped.initial_record())
        assert error.value.code == "budget_exhausted"
        assert len(calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "cancel"])
def test_ambiguous_failures_preserve_reservation_across_restart(private_inputs, monkeypatch, failure):
    async def broken(context, **kwargs):
        if failure == "cancel":
            raise asyncio.CancelledError
        raise llm.LLMError("timeout", "Таймаут OpenAI.")

    monkeypatch.setattr(intelligence, "request_policy", broken)

    async def scenario():
        service = IntelligenceService(private_inputs)
        expected = asyncio.CancelledError if failure == "cancel" else llm.LLMError
        with pytest.raises(expected):
            await service.prepare(private_inputs.data_path, service.initial_record())
        state = IntelligenceService(private_inputs).info()
        assert state["reserved_usd"] == .02 and state["request_attempts"] == 1
        assert state["estimated_spend_usd"] == 0 and state["completed_requests"] == 0
        assert not list((private_inputs.storage_path / "llm").glob("policies/*.json"))

    asyncio.run(scenario())


def test_corrupt_ledger_fails_closed(private_inputs, monkeypatch):
    calls = mock_openai(monkeypatch)
    folder = private_inputs.storage_path / "llm"
    folder.mkdir(parents=True)
    (folder / "spend.json").write_text('{"version":1}')
    service = IntelligenceService(private_inputs)
    assert service.info()["status"] == "storage_error"
    with pytest.raises(llm.LLMError):
        asyncio.run(service.prepare(private_inputs.data_path, service.initial_record()))
    assert not calls


def test_full_run_uses_openai_priorities_and_reuses_policy(private_inputs, monkeypatch):
    calls = mock_openai(monkeypatch)

    async def scenario():
        manager = RunManager(private_inputs)
        try:
            queued = await manager.start(42)
            with pytest.raises(RunBusyError):
                await manager.start(43)
            await asyncio.wait_for(manager._active, 20)
            run = manager.get(queued["id"])
            assert run["status"] == "completed", run["error"]
            assert run["llm"]["status"] == "completed"
            assert run["diagnostics"]["policy_source"] == "openai"
            assert 1 <= len(run["campaigns"]) <= 10
            first_pilots = [event["data"] for event in run["events"] if event["event"] == "pilot_result"][:3]
            assert first_pilots and all(p["arm"][2] == "tariff_c" for p in first_pilots)
            again = await manager.start(43)
            await asyncio.wait_for(manager._active, 20)
            assert manager.get(again["id"])["llm"]["cache_hit"]
            assert len(calls) == 1
            assert private_inputs.openai_api_key not in json.dumps(manager.get(again["id"]))
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_llm_failure_does_not_run_local_fallback(private_inputs, monkeypatch):
    async def failed(context, **kwargs):
        raise llm.LLMError("refused", "OpenAI отказался формировать гипотезы.")

    monkeypatch.setattr(intelligence, "request_policy", failed)

    async def scenario():
        manager = RunManager(private_inputs)
        queued = await manager.start(42)
        await asyncio.wait_for(manager._active, 5)
        run = manager.get(queued["id"])
        assert run["status"] == "failed" and run["llm"]["error_code"] == "refused"
        assert not run["campaigns"] and run["metrics"] is None
        assert not any(event["event"] == "pilot_result" for event in run["events"])
        assert manager._process is None
        await manager.close()

    asyncio.run(scenario())


def test_unexpected_failure_still_finishes_the_run(private_inputs, monkeypatch):
    async def failed(context, **kwargs):
        raise OverflowError("Untrusted implementation detail")

    monkeypatch.setattr(intelligence, "request_policy", failed)

    async def scenario():
        manager = RunManager(private_inputs)
        queued = await manager.start(42)
        await asyncio.wait_for(manager._active, 5)
        run = manager.get(queued["id"])
        assert run["status"] == "failed" and run["finished_at"]
        assert run["llm"]["status"] == "failed"
        assert "Untrusted" not in run["error"]
        await manager.close()

    asyncio.run(scenario())
