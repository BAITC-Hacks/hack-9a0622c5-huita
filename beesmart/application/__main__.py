"""Run the same agent pipeline from the terminal, without FastAPI or HTTP."""

import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path

from beesmart.agent.llm import LLMError
from beesmart.application.datasets import DatasetRepository
from beesmart.application.runs import RunManager
from beesmart.application.storage import StorageLease
from beesmart.config import Settings


async def calculate(settings: Settings, seed: int) -> dict:
    """Validate data, reserve storage, prepare AI policy, execute and save a run."""
    lease = StorageLease(settings.storage_path)
    lease.acquire()
    manager = None
    try:
        overview = await asyncio.to_thread(DatasetRepository(settings).overview)
        if not overview["runtime"]["ready"]:
            raise ValueError("Нужны три корректных CSV и файлы среды organizer/.")
        manager = RunManager(settings)
        queued = await manager.start(seed)
        result = await manager.wait(queued["id"])
        if result is None:
            raise RuntimeError("Отчёт запуска недоступен.")
        return result
    finally:
        if manager is not None:
            await manager.close()
        lease.release()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="Каталог трёх CSV; по умолчанию BEESMART_DATA_DIR")
    parser.add_argument("--storage-dir", type=Path, help="Приватное хранилище; не используйте одновременно с сервером")
    parser.add_argument("--provider", choices=("local", "openai"), help="По умолчанию настройка из .env; openai может расходовать API-бюджет")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if not 0 <= args.seed <= 4_294_967_295:
        parser.error("seed должен быть целым числом от 0 до 4294967295")
    try:
        settings = Settings.from_env()
        overrides = {}
        if args.data_dir is not None:
            overrides["data_dir"] = args.data_dir.resolve()
        if args.storage_dir is not None:
            overrides["storage_dir"] = args.storage_dir.resolve()
        if args.provider is not None:
            overrides["agent_provider"] = args.provider
        settings = replace(settings, **overrides)
        record = asyncio.run(calculate(settings, args.seed))
        metrics = record["metrics"] or {}
        output = {
            "id": record["id"], "status": record["status"], "seed": record["seed"],
            "provider": record["llm"]["provider"], "model": record["llm"]["model"],
            "llm_status": record["llm"]["status"], "cache_hit": record["llm"]["cache_hit"],
            "pilots": metrics.get("n_pilots", 0), "campaigns": len(record["campaigns"]),
            "net_arpu_gain": metrics.get("net_arpu_gain"), "error": record["error"],
            "report": str(settings.storage_path / "runs" / f"{record['id']}.json"),
        }
        print(json.dumps(output, ensure_ascii=False, allow_nan=False))
        return 0 if record["status"] == "completed" else 1
    except (LLMError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2
    except OSError:
        print(json.dumps({"status": "failed", "error": "Не удалось прочитать данные или сохранить отчёт."}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
