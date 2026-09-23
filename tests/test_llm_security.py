"""Security boundaries are exercised only with fabricated data and fake HTTP."""

import asyncio
import json
import secrets

import httpx
import pytest

from beesmart.config import Settings
from beesmart.agent.intelligence import IntelligenceService
from beesmart.agent.llm import LLMError, MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES, PlanningContext, request_policy
from beesmart.application.runs import RunManager


@pytest.fixture
def security_context():
    return PlanningContext(
        payload={"schema_version": 1, "candidates": [{"id": "h001", "arpu_segment": "LOW"}]},
        candidates={"h001": ("private_source", "LOW", "private_target")},
        fingerprint="fabricated-security-context",
    )


def response_payload(**changes):
    payload = {
        "status": "completed", "usage": {"input_tokens": 100, "output_tokens": 50},
        "output": [{"type": "message", "role": "assistant", "content": [{
            "type": "output_text", "text": json.dumps({
                "hypothesis_ids": ["h001"], "summary": "Проверим гипотезу локальным пилотом.",
            }),
        }]}],
    }
    payload.update(changes)
    return payload


def test_provider_endpoint_ignores_environment_and_key_stays_in_header(security_context, monkeypatch):
    generated_key = secrets.token_urlsafe(48)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unexpected.invalid/api/")
    monkeypatch.setenv("HTTPS_PROXY", "http://unexpected.invalid:9999")
    monkeypatch.setenv("ALL_PROXY", "http://unexpected.invalid:9999")
    client_type = httpx.AsyncClient
    client_options = []
    calls = []

    def client_factory(*args, **kwargs):
        client_options.append(kwargs)
        return client_type(*args, **kwargs)

    def handler(request):
        calls.append(request)
        assert request.url.host == "api.openai.com"
        assert request.url.scheme == "https"
        assert request.headers["authorization"] == "Bearer " + generated_key
        assert generated_key.encode() not in request.content
        assert b"private_source" not in request.content
        assert b"private_target" not in request.content
        assert "tools" not in json.loads(request.content)
        return httpx.Response(200, json=response_payload())

    monkeypatch.setattr("beesmart.agent.llm.httpx.AsyncClient", client_factory)
    asyncio.run(request_policy(security_context, api_key=generated_key, transport=httpx.MockTransport(handler)))
    assert len(calls) == len(client_options) == 1
    assert client_options[0]["trust_env"] is False
    assert client_options[0]["follow_redirects"] is False


def test_provider_redirect_never_forwards_authorization(security_context):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(307, headers={"location": "https://unexpected.invalid/collect"})

    with pytest.raises(LLMError) as caught:
        asyncio.run(request_policy(security_context, api_key=secrets.token_urlsafe(48),
                                   transport=httpx.MockTransport(handler)))
    assert calls == ["api.openai.com"]
    assert caught.value.code == "provider_error"


def test_error_body_is_not_read_or_disclosed(security_context, caplog):
    private_marker = secrets.token_urlsafe(48)

    class UnreadableErrorBody(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            raise AssertionError("Provider error bodies must not be consumed")
            yield b""  # Make this an async generator without reading its body.

        async def aclose(self):
            self.closed = True

    stream = UnreadableErrorBody()

    def handler(_request):
        return httpx.Response(401, stream=stream, headers={"x-private-provider-value": private_marker})

    with pytest.raises(LLMError) as caught:
        asyncio.run(request_policy(security_context, api_key=private_marker,
                                   transport=httpx.MockTransport(handler)))
    assert caught.value.code == "authentication_failed"
    assert stream.closed
    assert private_marker not in str(caught.value)
    assert private_marker not in repr(caught.value)
    assert private_marker not in caplog.text


def test_streamed_response_cap_stops_reading_and_closes_connection(security_context):
    class OversizedStream(httpx.AsyncByteStream):
        chunks = 0
        closed = False

        async def __aiter__(self):
            for _ in range(1000):
                self.chunks += 1
                yield b"x" * 4096

        async def aclose(self):
            self.closed = True

    stream = OversizedStream()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=stream)

    with pytest.raises(LLMError) as caught:
        asyncio.run(request_policy(security_context, api_key=secrets.token_urlsafe(48),
                                   transport=httpx.MockTransport(handler)))
    assert caught.value.code == "response_too_large"
    assert stream.chunks == MAX_RESPONSE_BYTES // 4096 + 1
    assert stream.closed and len(calls) == 1


def test_cancellation_closes_inflight_provider_stream_without_retry(security_context):
    async def scenario():
        reading = asyncio.Event()
        never_finishes = asyncio.Event()
        calls = []

        class SlowStream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                reading.set()
                await never_finishes.wait()
                yield b"{}"

            async def aclose(self):
                self.closed = True

        stream = SlowStream()

        def handler(request):
            calls.append(request)
            return httpx.Response(200, stream=stream)

        task = asyncio.create_task(request_policy(security_context, api_key=secrets.token_urlsafe(48),
                                                  transport=httpx.MockTransport(handler)))
        await asyncio.wait_for(reading.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed and len(calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("usage", [
    {"input_tokens": 10 ** 1000, "output_tokens": 1},
    {"input_tokens": MAX_REQUEST_BYTES + 1, "output_tokens": 1},
    {"input_tokens": 1, "output_tokens": 4097},
])
def test_usage_cannot_overflow_budget_arithmetic(security_context, usage):
    def handler(_request):
        return httpx.Response(200, json=response_payload(usage=usage))

    with pytest.raises(LLMError):
        asyncio.run(request_policy(security_context, api_key=secrets.token_urlsafe(48),
                                   transport=httpx.MockTransport(handler)))


def test_local_provider_never_builds_context_or_calls_network(tmp_path, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Local mode must have no LLM preparation or paid call")

    monkeypatch.setattr("beesmart.agent.intelligence.build_context", forbidden)
    monkeypatch.setattr("beesmart.agent.intelligence.request_policy", forbidden)
    service = IntelligenceService(Settings(root=tmp_path, agent_provider="local"))
    metadata = service.initial_record()
    assert asyncio.run(service.prepare(tmp_path, metadata)) == tmp_path / "policies/frozen_policy.json"
    assert metadata["status"] == "disabled"
    assert not service.info()["paid_calls"]
    assert not (tmp_path / "work" / "llm").exists()


def test_provider_credentials_never_reach_worker_or_run_metadata(tmp_path, monkeypatch):
    generated_key = secrets.token_urlsafe(48)
    generated_browser_token = secrets.token_urlsafe(48)
    inherited_names = ("OPENAI_API_KEY", "NVIDIA_API_KEY", "BEESMART_API_TOKEN", "HTTP_PROXY", "PYTHONPATH")
    for name in inherited_names:
        monkeypatch.setenv(name, generated_key)
    settings = Settings(root=tmp_path, agent_provider="openai", openai_api_key=generated_key,
                        api_token=generated_browser_token)
    manager = RunManager(settings)
    environments = []

    async def fake_prepare(_data_path, metadata):
        metadata.update(status="completed", hypotheses=1)
        return tmp_path / "frozen_policy.json"

    class Output:
        emitted = False

        async def readline(self):
            if self.emitted:
                return b""
            self.emitted = True
            return b'{"type":"result","data":{}}\n'

    class Process:
        returncode = 0
        stdout = Output()

        async def wait(self):
            return self.returncode

    async def fake_subprocess(*_args, **kwargs):
        environments.append(kwargs["env"])
        return Process()

    monkeypatch.setattr(manager.intelligence, "prepare", fake_prepare)
    monkeypatch.setattr("beesmart.application.runs.asyncio.create_subprocess_exec", fake_subprocess)

    async def scenario():
        record = await manager.start(42)
        await manager._active
        assert manager.get(record["id"])["status"] == "completed"
        return record["id"]

    run_id = asyncio.run(scenario())
    assert len(environments) == 1
    assert not set(inherited_names).intersection(environments[0])
    public = json.dumps({"agent": manager.intelligence.info(), "run": manager.get(run_id)})
    persisted = (settings.storage_path / "runs" / f"{run_id}.json").read_text()
    for value in (generated_key, generated_browser_token):
        assert value not in repr(settings)
        assert value not in public
        assert value not in persisted
