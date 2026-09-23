import asyncio
import csv
import io
import json
import logging
import os
import sys
import time
from collections import OrderedDict
from contextlib import suppress
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4

from beesmart.config import Settings


CAMPAIGN_COLUMNS = [
    "campaign_name", "filter_arpu_segment", "filter_data_segment", "filter_call_segment",
    "filter_current_tariff", "target_tariff", "channel",
]


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunBusyError(Exception):
    pass


class RunManager:
    """One bounded subprocess per run keeps CPU work outside the API event loop."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._runs: OrderedDict[str, dict] = OrderedDict()
        self._active: asyncio.Task | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._upload_reserved = False
        self._starting = False
        self._load_reports()

    def _load_reports(self) -> None:
        folder = self.settings.storage_path / "runs"
        if not folder.exists():
            return
        paths = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)[-self.settings.retained_runs:]
        for path in paths:
            if path.stat().st_size > 2_000_000:
                continue
            try:
                record = json.loads(path.read_text())
                if str(UUID(record["id"])) == path.stem and record["status"] in ("queued", "running", "completed", "failed"):
                    record.setdefault("dataset", {"source": "bundled"})
                    if record["status"] in ("queued", "running"):
                        record.update(status="failed", error="Сервер перезапущен. Загрузите данные и повторите расчёт.", finished_at=timestamp())
                        self._save_report(record)
                    self._runs[record["id"]] = record
            except (ValueError, KeyError, OSError, TypeError):
                continue

    def reserve_upload(self) -> None:
        if self._upload_reserved or self._starting or (self._active is not None and not self._active.done()):
            raise RunBusyError
        self._upload_reserved = True

    def release_upload(self) -> None:
        self._upload_reserved = False

    async def start(self, seed: int, *, dataset: dict | None = None, from_upload: bool = False) -> dict:
        if (self._starting or (self._upload_reserved and not from_upload)
                or (self._active is not None and not self._active.done())):
            raise RunBusyError
        # Reservation must precede the first yield, including the disk write.
        # Worker creation stays on this event loop and follows durable queuing.
        self._starting = True
        try:
            run_id = str(uuid4())
            record = {
                "id": run_id, "status": "queued", "seed": seed, "created_at": timestamp(),
                "finished_at": None, "duration_seconds": None, "error": None,
                "campaigns": [], "metrics": None, "events": [], "diagnostics": {},
                "dataset": {"source": "uploaded", **dataset} if dataset else {"source": "bundled"},
            }
            persistence = asyncio.create_task(asyncio.to_thread(self._save_report, deepcopy(record)))
            try:
                await asyncio.shield(persistence)
            except asyncio.CancelledError:
                # Cancellation cannot stop an already-running file thread. Wait
                # before replacing the queued record, so it cannot overwrite a
                # later failed status. Never launch a worker for this request.
                try:
                    await self._finish_persistence(persistence)
                except OSError:
                    logging.getLogger(__name__).error("Canceled run could not finish its initial report write")
                record.update(status="failed", finished_at=timestamp(), duration_seconds=0.0,
                              error="Запуск отменён до начала расчёта. Повторите запрос.")
                cancellation_report = asyncio.create_task(asyncio.to_thread(self._save_report, deepcopy(record)))
                try:
                    await self._finish_persistence(cancellation_report)
                except OSError:
                    logging.getLogger(__name__).error("Canceled run could not persist its failed status")
                    # If storage is unavailable, remove a surviving queued
                    # record rather than leave it looking ready to execute.
                    cleanup = asyncio.create_task(asyncio.to_thread(
                        (self.settings.storage_path / "runs" / f"{run_id}.json").unlink, missing_ok=True))
                    with suppress(OSError):
                        await self._finish_persistence(cleanup)
                self._remember(record)
                raise
            self._remember(record)
            self._active = asyncio.create_task(self._execute(record))
            return deepcopy(record)
        finally:
            self._starting = False

    def _remember(self, record: dict) -> None:
        self._runs[record["id"]] = record
        while len(self._runs) > self.settings.retained_runs:
            self._runs.popitem(last=False)

    @staticmethod
    async def _finish_persistence(task: asyncio.Task):
        """Complete cancellation cleanup even if the caller is canceled twice."""
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    return task.result()

    def get(self, run_id: str) -> dict | None:
        record = self._runs.get(run_id)
        return deepcopy(record) if record is not None else None

    def latest(self) -> dict | None:
        return self.get(next(reversed(self._runs))) if self._runs else None

    def submission(self, run_id: str) -> str | None:
        record = self._runs.get(run_id)
        if not record or record["status"] != "completed":
            return None
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=CAMPAIGN_COLUMNS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(record["campaigns"])
        return buffer.getvalue()

    async def _execute(self, record: dict) -> None:
        started = time.perf_counter()
        record["status"] = "running"
        process = None
        try:
            # No API keys, arbitrary paths, shell commands or inherited Python hooks in worker.
            environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT") if key in os.environ}
            environment.update(PYTHONUNBUFFERED="1", PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1",
                               OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
            data_path = self.settings.data_path
            if record["dataset"]["source"] == "uploaded":
                data_path = self.settings.storage_path / "datasets" / UUID(record["dataset"]["id"]).hex
            arguments = [sys.executable, "-m", "beesmart.worker", str(record["seed"]), "--data-dir", str(data_path)]
            process = await asyncio.create_subprocess_exec(
                *arguments,
                cwd=self.settings.root, env=environment,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                limit=1_048_576,
            )
            self._process = process
            result = None
            async with asyncio.timeout(self.settings.run_timeout_seconds):
                while line := await process.stdout.readline():
                    message = json.loads(line)
                    if message.get("type") == "event" and len(record["events"]) < self.settings.max_events:
                        record["events"].append(message["data"])
                    elif message.get("type") == "result":
                        result = message["data"]
                code = await process.wait()
            if code != 0 or result is None:
                raise RuntimeError("Worker did not produce a valid result")
            record.update(result)
            record["status"] = "completed"
        except TimeoutError:
            record.update(status="failed", error="Расчёт остановлен: превышен лимит времени.")
        except asyncio.CancelledError:
            record.update(status="failed", error="Расчёт остановлен вместе с сервером.")
            raise
        except (OSError, ValueError, KeyError, RuntimeError):
            record.update(status="failed", error="Расчёт не завершён. Проверьте данные командой python local_eval.py.")
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            self._process = None
            record["finished_at"] = timestamp()
            record["duration_seconds"] = round(time.perf_counter() - started, 3)
            try:
                await asyncio.to_thread(self._save_report, deepcopy(record))
            except OSError:
                logging.getLogger(__name__).error("Run report could not be persisted; check storage availability")
                record["error"] = "Результат рассчитан, но отчёт не сохранён. Скачайте его до перезапуска сервера."

    def _save_report(self, record: dict) -> None:
        folder = self.settings.storage_path / "runs"
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / f"{record['id']}.json"
        temporary = path.with_suffix(".tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False, allow_nan=False))
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
        except BaseException:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise
        for old in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)[:-self.settings.retained_runs]:
            old.unlink()

    async def close(self) -> None:
        if self._active is not None and not self._active.done():
            self._active.cancel()
            await asyncio.gather(self._active, return_exceptions=True)
