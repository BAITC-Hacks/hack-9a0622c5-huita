"""Real worker + mocked OpenAI; durable cache and billing failure boundaries."""

import asyncio
import hashlib
import json
import secrets
import stat
import threading
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from beesmart.agent import intelligence, llm
from beesmart.config import Settings
from beesmart.agent.intelligence import IntelligenceService
from beesmart.application.runs import RunManager, RunBusyError
from scripts import build_submission as exporter


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


@pytest.mark.parametrize("code", ["refused", "timeout"])
def test_llm_failure_completes_with_local_fallback(private_inputs, monkeypatch, code):
    async def failed(context, **kwargs):
        raise llm.LLMError(code, "OpenAI не сформировал гипотезы.")

    monkeypatch.setattr(intelligence, "request_policy", failed)

    async def scenario():
        manager = RunManager(private_inputs)
        queued = await manager.start(42)
        await asyncio.wait_for(manager._active, 5)
        run = manager.get(queued["id"])
        assert run["status"] == "completed" and run["error"] is None
        assert run["llm"]["status"] == "failed" and run["llm"]["error_code"] == code
        assert run["llm"]["fallback_used"] and run["llm"]["fallback_reason"]
        assert run["llm"]["reserved_usd"] == .02
        assert run["campaigns"] and run["metrics"]["n_pilots"] > 0
        assert any(event["event"] == "agent_fallback" for event in run["events"])
        assert not any(event["event"] == "agent_policy_ready" for event in run["events"])
        assert RunManager(private_inputs).get(run["id"])["llm"] == run["llm"]
        assert manager._process is None
        await manager.close()

    asyncio.run(scenario())


def test_exhausted_budget_run_exports_local_policy_without_credentials(private_inputs, monkeypatch):
    calls = mock_openai(monkeypatch)
    baseline = (ROOT / "policies/frozen_policy.json").read_bytes()

    async def scenario():
        manager = RunManager(replace(private_inputs, llm_budget_usd=0))
        await manager.start(42)
        await asyncio.wait_for(manager._active, 5)
        run = manager.latest()
        assert run["status"] == "completed" and run["llm"]["fallback_used"]
        assert run["llm"]["error_code"] == "budget_exhausted"
        assert run["llm"]["reserved_usd"] == 0
        assert not calls and manager.intelligence.info()["request_attempts"] == 0
        return run, manager.submission(run["id"])

    run, expected_csv = asyncio.run(scenario())
    assert run["llm"]["status"] == "failed" and run["status"] == "completed"
    assert run["diagnostics"]["policy_hash"] == hashlib.sha256(baseline).hexdigest()
    # Replaying a failed LLM phase needs only the exact local artifact. No API
    # credentials are present in export Settings or the generator subprocess.
    settings = replace(private_inputs, agent_provider="local", openai_api_key="")
    real_run = exporter.subprocess.run
    environments = []

    def offline_generator(*args, **kwargs):
        environments.append(kwargs["env"])
        return real_run(*args, **kwargs)

    monkeypatch.setattr(exporter.subprocess, "run", offline_generator)
    output = exporter.build_submission(settings, run["id"])
    assert (output / "submission.csv").read_text() == expected_csv
    assert (output / "policies/frozen_policy.json").read_bytes() == baseline
    assert (ROOT / "policies/frozen_policy.json").read_bytes() == baseline
    assert json.loads((output / "manifest.json").read_text())["verified"] is True
    assert len(environments) == 1 and "OPENAI_API_KEY" not in environments[0]
    assert not calls


def test_cli_reports_fallback_from_real_pipeline(private_inputs, monkeypatch, capsys):
    from beesmart.application import __main__ as cli

    calls = mock_openai(monkeypatch)
    settings = replace(private_inputs, llm_budget_usd=0)
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr("sys.argv", ["beesmart.application", "--seed", "42"])
    assert cli.main() == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "completed" and summary["llm_status"] == "failed"
    assert summary["provider"] == "openai" and summary["fallback_used"] is True
    assert summary["fallback_reason"] and summary["error"] is None
    assert summary["pilots"] > 0 and 1 <= summary["campaigns"] <= 10
    saved = json.loads(Path(summary["report"]).read_text())
    assert saved["llm"]["error_code"] == "budget_exhausted"
    assert not calls


def test_planning_deadline_cancels_request_and_leaves_time_for_worker(private_inputs, monkeypatch):
    closed = []

    async def hanging(context, **kwargs):
        try:
            await asyncio.Future()
        finally:
            closed.append(True)

    monkeypatch.setattr(intelligence, "request_policy", hanging)

    async def scenario():
        settings = replace(private_inputs, run_timeout_seconds=5)
        assert settings.planning_timeout_seconds == 1
        manager = RunManager(settings)
        await manager.start(42)
        await asyncio.wait_for(manager._active, 5)
        run = manager.latest()
        assert closed and run["status"] == "completed"
        assert run["llm"]["error_code"] == "planning_timeout"
        assert run["llm"]["fallback_used"] and run["llm"]["reserved_usd"] == .02
        assert manager.intelligence.info()["request_attempts"] == 1
        assert run["duration_seconds"] < settings.run_timeout_seconds

    asyncio.run(scenario())


def test_shutdown_cancels_request_without_fallback(private_inputs, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        closed = asyncio.Event()

        async def hanging(context, **kwargs):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                closed.set()

        monkeypatch.setattr(intelligence, "request_policy", hanging)
        manager = RunManager(private_inputs)
        await manager.start(42)
        await asyncio.wait_for(entered.wait(), 2)
        await manager.close()
        run = manager.latest()
        assert closed.is_set() and run["status"] == "failed"
        assert not run["llm"]["fallback_used"] and not run["campaigns"]
        assert run["llm"]["error_code"] == "interrupted"
        assert run["llm"]["reserved_usd"] == .02
        assert not any(event["event"] == "agent_fallback" for event in run["events"])
        assert RunManager(private_inputs).get(run["id"])["status"] == "failed"

    asyncio.run(scenario())


def test_slow_file_cleanup_cannot_launch_worker_after_total_deadline(private_inputs, monkeypatch):
    release = threading.Event()
    original = intelligence.build_context

    def slow_context(path):
        assert release.wait(3)
        return original(path)

    calls = mock_openai(monkeypatch)
    monkeypatch.setattr(intelligence, "build_context", slow_context)

    async def scenario():
        manager = RunManager(replace(private_inputs, run_timeout_seconds=.1))
        await manager.start(42)
        try:
            await asyncio.sleep(.2)
            assert not manager._active.done()  # Cancellation waits for the file thread.
            release.set()
            await asyncio.wait_for(manager._active, 3)
            run = manager.latest()
            assert run["status"] == "failed" and "лимит времени" in run["error"]
            assert not run["campaigns"] and manager._process is None
            assert not calls and manager.intelligence.info()["request_attempts"] == 0
        finally:
            release.set()
            await manager.close()

    asyncio.run(scenario())


def test_policy_write_failure_retains_completed_charge(private_inputs, monkeypatch):
    calls = mock_openai(monkeypatch)
    original = intelligence.private_json

    def failed_cache(path, value):
        if path.parent.name == "policies":
            raise OSError("Private filesystem detail")
        original(path, value)

    monkeypatch.setattr(intelligence, "private_json", failed_cache)

    async def scenario():
        manager = RunManager(private_inputs)
        await manager.start(42)
        await asyncio.wait_for(manager._active, 5)
        run = manager.latest()
        assert run["status"] == "completed" and run["llm"]["fallback_used"]
        assert run["llm"]["error_code"] == "policy_storage"
        assert run["llm"]["estimated_cost_usd"] > 0 and run["llm"]["reserved_usd"] == 0
        assert run["llm"]["input_tokens"] == 2000 and len(calls) == 1
        assert "Private filesystem" not in json.dumps(run)
        restarted = IntelligenceService(private_inputs)
        assert restarted.info()["estimated_spend_usd"] == run["llm"]["estimated_cost_usd"]
        assert restarted.info()["completed_requests"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 0, -1])
def test_run_deadline_must_be_finite_and_positive(value):
    with pytest.raises(ValueError):
        Settings(run_timeout_seconds=value)


def test_cancel_during_spawn_reaps_child_and_persists_failed_run(private_inputs, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        spawn_release = asyncio.Event()
        reaping = asyncio.Event()
        reap_release = asyncio.Event()

        class Process:
            returncode = None
            killed = False

            def kill(self):
                self.killed = True

            async def wait(self):
                reaping.set()
                await reap_release.wait()
                self.returncode = -9
                return self.returncode

        process = Process()

        async def spawn(*args, **kwargs):
            entered.set()
            await spawn_release.wait()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        settings = replace(private_inputs, agent_provider="local")
        manager = RunManager(settings)
        await manager.start(42)
        await asyncio.wait_for(entered.wait(), 2)
        manager._active.cancel()
        await asyncio.sleep(0)
        assert not manager._active.done()
        spawn_release.set()
        await asyncio.wait_for(reaping.wait(), 2)
        manager._active.cancel()  # A repeated shutdown must not skip reaping or persistence.
        await asyncio.sleep(0)
        assert process.killed and not manager._active.done()
        reap_release.set()
        await asyncio.gather(manager._active, return_exceptions=True)
        assert process.returncode == -9 and manager._process is None
        run = manager.latest()
        assert run["status"] == "failed"
        assert RunManager(settings).get(run["id"])["status"] == "failed"

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
