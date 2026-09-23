"""Private LLM policy cache and durable, conservative application spend limit.

One server owns the storage lease; RunManager permits one active computation.
No network request is allowed before its maximum charge has been persisted.
"""

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path

from beesmart.config import Settings
from beesmart.llm import LLMError, PROMPT_VERSION, build_context, request_policy


RESERVATION_MICRO_USD = 20_000  # $0.02; bounded 64 KiB input + 4096 output tokens.
CACHE_ENTRIES = 128


def private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        # Persist the rename too, including across an abrupt host restart.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class IntelligenceService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.folder = settings.storage_path / "llm"
        self._ledger = {"version": 1, "estimated_micro_usd": 0,
                        "reserved_micro_usd": 0, "attempts": 0, "completed": 0}
        self._ledger_error = False
        self._lock = asyncio.Lock()
        path = self.folder / "spend.json"
        if path.exists():
            try:
                if path.stat().st_size > 4096:
                    raise ValueError
                value = json.loads(path.read_text())
                if (set(value) != set(self._ledger) or value["version"] != 1
                        or any(type(v) is not int or v < 0 for v in value.values())
                        or value["completed"] > value["attempts"]):
                    raise ValueError
                self._ledger = value
            except (ValueError, OSError, TypeError):
                self._ledger_error = True  # Never reset corrupt spend tracking to zero.

    def info(self) -> dict:
        enabled = self.settings.agent_provider == "openai"
        status = ("disabled" if not enabled else "storage_error" if self._ledger_error
                  else "ready" if self.settings.openai_api_key else "needs_configuration")
        return {
            "engine": "openai_python" if enabled else "local_python",
            "model": self.settings.openai_model if enabled else None,
            "llm_calls": enabled, "paid_calls": enabled and status == "ready",
            "evaluation": "organizer_mock", "provider": self.settings.agent_provider,
            "ready": status in ("ready", "disabled"), "status": status,
            "budget_usd": self.settings.llm_budget_usd,
            "estimated_spend_usd": self._ledger["estimated_micro_usd"] / 1_000_000,
            "reserved_usd": self._ledger["reserved_micro_usd"] / 1_000_000,
            "request_attempts": self._ledger["attempts"],
            "completed_requests": self._ledger["completed"],
        }

    def check_ready(self) -> None:
        if self.settings.agent_provider == "local":
            return
        if self._ledger_error:
            raise LLMError("budget_storage", "Журнал расходов недоступен. Проверьте приватное хранилище сервера.")
        if not self.settings.openai_api_key:
            raise LLMError("missing_api_key", "Добавьте OPENAI_API_KEY в локальный .env и перезапустите сервер; для работы без сети выберите BEESMART_AGENT_PROVIDER=local.")

    def initial_record(self) -> dict:
        return {"provider": self.settings.agent_provider,
                "model": self.settings.openai_model if self.settings.agent_provider == "openai" else None,
                "status": "pending" if self.settings.agent_provider == "openai" else "disabled",
                "cache_hit": False, "input_tokens": 0, "output_tokens": 0,
                "estimated_cost_usd": 0.0, "reserved_usd": 0.0,
                "summary": "", "hypotheses": 0, "error_code": None}

    async def prepare(self, data_path: Path, metadata: dict) -> Path:
        self.check_ready()
        if self.settings.agent_provider == "local":
            return self.settings.root / "frozen_policy.json"
        async with self._lock:
            context = await asyncio.to_thread(build_context, data_path)
            fingerprint = hashlib.sha256(
                f"{PROMPT_VERSION}:{self.settings.openai_model}:{context.fingerprint}".encode()
            ).hexdigest()
            path = self.folder / "policies" / f"{fingerprint}.json"
            cached = self._cached(path, context)
            if cached is not None:
                metadata.update(status="cached", cache_hit=True, summary=cached["_llm"]["summary"],
                                hypotheses=len(cached["priority_arms"]))
                return path

            used = self._ledger["estimated_micro_usd"] + self._ledger["reserved_micro_usd"]
            if used + RESERVATION_MICRO_USD > round(self.settings.llm_budget_usd * 1_000_000):
                raise LLMError("budget_exhausted", "Достигнут лимит расходов OpenAI для приложения. Проверьте бюджет в .env; сохранённые политики остаются доступны.")
            # Small bounded ledger writes are synchronous: a cancellation cannot
            # race persistence and let a second request spend the same budget.
            self._persist_ledger({**self._ledger,
                "reserved_micro_usd": self._ledger["reserved_micro_usd"] + RESERVATION_MICRO_USD,
                "attempts": self._ledger["attempts"] + 1})
            metadata.update(status="planning", reserved_usd=RESERVATION_MICRO_USD / 1_000_000)
            response = await request_policy(context, api_key=self.settings.openai_api_key,
                                            model=self.settings.openai_model)
            # Conservative estimate: includes the published cache-write premium,
            # ignores discounts. This is not the OpenAI account billing balance.
            cost = math.ceil(response.input_tokens * 0.125 + response.output_tokens * 0.5)
            if cost > RESERVATION_MICRO_USD:
                raise LLMError("usage_invalid", "Ответ OpenAI содержит неожиданную статистику расхода токенов.")
            self._persist_ledger({**self._ledger,
                "estimated_micro_usd": self._ledger["estimated_micro_usd"] + cost,
                "reserved_micro_usd": self._ledger["reserved_micro_usd"] - RESERVATION_MICRO_USD,
                "completed": self._ledger["completed"] + 1})
            metadata.update(status="completed", summary=response.summary,
                            input_tokens=response.input_tokens, output_tokens=response.output_tokens,
                            estimated_cost_usd=cost / 1_000_000, reserved_usd=0.0,
                            hypotheses=len(response.policy["priority_arms"]))
            value = {**response.policy, "_llm": {"model": self.settings.openai_model,
                     "prompt_version": PROMPT_VERSION, "summary": response.summary}}
            private_json(path, value)
            for old in sorted(path.parent.glob("*.json"), key=lambda item: item.stat().st_mtime)[:-CACHE_ENTRIES]:
                old.unlink(missing_ok=True)
            return path

    def _persist_ledger(self, value: dict) -> None:
        try:
            private_json(self.folder / "spend.json", value)
        except OSError:
            self._ledger_error = True
            raise LLMError("budget_storage", "Не удалось сохранить журнал расходов; новый запрос OpenAI не разрешён.") from None
        self._ledger = value

    def _cached(self, path: Path, context) -> dict | None:
        try:
            if not path.is_file() or path.stat().st_size > 32_768:
                return None
            value = json.loads(path.read_text())
            arms = value["priority_arms"]
            allowed = set(context.candidates.values())
            actual = [tuple(arm) for arm in arms]
            info = value["_llm"]
            if (value["schema_version"] != 1 or value["source"] != "openai"
                    or value["alternative_targets_by_cell"] != {}
                    or not 1 <= len(actual) <= 14 or len(set(actual)) != len(actual)
                    or not set(actual) <= allowed
                    or {arm[1] for arm in actual} != {arm[1] for arm in allowed}
                    or info["model"] != self.settings.openai_model or info["prompt_version"] != PROMPT_VERSION
                    or not isinstance(info["summary"], str) or len(info["summary"]) > 1000):
                return None
            path.touch()
            return value
        except (OSError, ValueError, KeyError, TypeError):
            return None
