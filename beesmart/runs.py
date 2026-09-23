import asyncio
import csv
import io
import json
import os
import sys
import time
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

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
        self._load_reports()

    def _load_reports(self) -> None:
        folder = self.settings.root / "work" / "runs"
        if not folder.exists():
            return
        paths = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)[-self.settings.retained_runs:]
        for path in paths:
            if path.stat().st_size > 2_000_000:
                continue
            try:
                record = json.loads(path.read_text())
                from uuid import UUID
                if str(UUID(record["id"])) == path.stem and record["status"] in ("completed", "failed"):
                    self._runs[record["id"]] = record
            except (ValueError, KeyError, OSError, TypeError):
                continue

    def reserve_upload(self) -> None:
        if self._upload_reserved or (self._active is not None and not self._active.done()):
            raise RunBusyError
        self._upload_reserved = True

    def release_upload(self) -> None:
        self._upload_reserved = False

    def start(self, seed: int, *, dataset: dict | None = None, from_upload: bool = False) -> dict:
        if ((self._upload_reserved and not from_upload)
                or (self._active is not None and not self._active.done())):
            raise RunBusyError
        run_id = str(uuid4())
        record = {
            "id": run_id, "status": "queued", "seed": seed, "created_at": timestamp(),
            "finished_at": None, "duration_seconds": None, "error": None,
            "campaigns": [], "metrics": None, "events": [], "diagnostics": {},
            "dataset": {"source": "uploaded", **dataset} if dataset else {"source": "bundled"},
        }
        self._runs[run_id] = record
        while len(self._runs) > self.settings.retained_runs:
            self._runs.popitem(last=False)
        self._active = asyncio.create_task(self._execute(record))
        return deepcopy(record)

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
            arguments = [sys.executable, "-m", "beesmart.worker", str(record["seed"])]
            if record["dataset"]["source"] == "uploaded":
                arguments.append(record["dataset"]["id"])
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
            await asyncio.to_thread(self._save_report, deepcopy(record))

    def _save_report(self, record: dict) -> None:
        try:
            folder = self.settings.root / "work" / "runs"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{record['id']}.json"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            temporary.replace(path)
            for old in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)[:-self.settings.retained_runs]:
                old.unlink()
        except (OSError, ValueError):
            # Audit persistence cannot change an already computed campaign plan.
            pass

    async def close(self) -> None:
        if self._active is not None and not self._active.done():
            self._active.cancel()
            await asyncio.gather(self._active, return_exceptions=True)
