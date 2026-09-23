"""Reproduce one completed seed-42 API run as a private official submission.

This command never calls a provider or copies credentials. It deliberately fails
if the exact frozen policy has expired or today's code/data produces another plan.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from beesmart.config import Settings


COLUMNS = ["campaign_name", "filter_arpu_segment", "filter_data_segment",
           "filter_call_segment", "filter_current_tariff", "target_tariff", "channel"]
# Destination -> repository source. The official scripts retain their flat
# import layout in the private export; the participant package keeps its name.
CODE_FILES = {
    name: name for name in (
        "agent.py", "beesmart/__init__.py", "beesmart/agent/__init__.py",
        "beesmart/agent/domain.py", "beesmart/agent/models.py", "beesmart/agent/planner.py",
        "beesmart/agent/runner.py",
    )
} | {name: f"organizer/{name}" for name in (
    "environment.py", "scoring_core.py", "mock_environment.py", "local_eval.py", "make_submission.py",
)}
POLICY_FILE = "policies/frozen_policy.json"
DATA_FILES = {"customer_profile.csv": 32 * 1024 * 1024,
              "data/change_tariff.csv": 16 * 1024 * 1024,
              "data/dict_tariff.csv": 256 * 1024}
MAX_POLICY_BYTES = 32_768


class SubmissionExportError(ValueError):
    """Safe, actionable export failure with no raw customer/provider content."""


def _read_file(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise SubmissionExportError("Исходный файл превышает допустимый размер.")
    return value


def _write_file(path: Path, value: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(value)


def _exact_policy(settings: Settings, expected_hash: str) -> bytes:
    paths = [settings.root / POLICY_FILE]
    folder = settings.storage_path / "llm" / "policies"
    if folder.is_dir():
        # A healthy cache has <=128 entries. Bound scans even for corrupt storage.
        with os.scandir(folder) as entries:
            for index, entry in enumerate(entries):
                if index >= 256:
                    raise SubmissionExportError("Кеш политик превышает допустимый размер.")
                if entry.name.endswith(".json") and entry.is_file(follow_symlinks=False):
                    paths.append(Path(entry.path))
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            value = _read_file(path, MAX_POLICY_BYTES)
        except SubmissionExportError:
            continue
        if hashlib.sha256(value).hexdigest() == expected_hash:
            policy = json.loads(value)
            if (not isinstance(policy, dict) or policy.get("schema_version") != 1
                    or not isinstance(policy.get("alternative_targets_by_cell"), dict)):
                raise SubmissionExportError("Сохранённая политика имеет неподдерживаемый формат.")
            return value
    raise SubmissionExportError("Точная политика запуска недоступна. Повторите расчёт с seed 42.")


def _campaign_rows(campaigns: object) -> list[dict[str, str]]:
    if not isinstance(campaigns, list) or not 1 <= len(campaigns) <= 10:
        raise SubmissionExportError("Отчёт не содержит корректный итоговый план.")
    rows = []
    for campaign in campaigns:
        if not isinstance(campaign, dict):
            raise SubmissionExportError("Отчёт содержит некорректную кампанию.")
        row = {}
        for column in COLUMNS:
            value = campaign.get(column)
            if value is not None and not isinstance(value, str):
                raise SubmissionExportError("Отчёт содержит некорректные поля кампании.")
            row[column] = "" if value is None else value
        rows.append(row)
    return rows


def _worker_environment() -> dict[str, str]:
    environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
                   if key in os.environ}
    environment.update(PYTHONUNBUFFERED="1", PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1",
                       OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1")
    return environment


def build_submission(settings: Settings, run_id: str, *, output: Path | None = None,
                     data_dir: Path | None = None) -> Path:
    """Export only after the official generator exactly reproduces the API CSV."""
    staging = None
    try:
        identifier = str(UUID(run_id))
        report_path = settings.storage_path / "runs" / f"{identifier}.json"
        report = json.loads(_read_file(report_path, 2_000_000))
        if (not isinstance(report, dict) or report.get("id") != identifier
                or report.get("status") != "completed" or type(report.get("seed")) is not int
                or report["seed"] != 42):
            raise SubmissionExportError("Нужен завершённый API-запуск с seed 42.")
        expected_rows = _campaign_rows(report.get("campaigns"))
        policy_hash = report.get("diagnostics", {}).get("policy_hash")
        if not isinstance(policy_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", policy_hash):
            raise SubmissionExportError("В отчёте отсутствует корректный хеш политики.")
        policy = _exact_policy(settings, policy_hash)
        if data_dir is not None:
            data_root = Path(data_dir).resolve()
        elif report.get("dataset", {}).get("source") == "uploaded":
            data_root = settings.storage_path / "datasets" / UUID(report["dataset"]["id"]).hex
        else:
            data_root = settings.data_path
        destination = (Path(output) if output is not None else
                       settings.storage_path / "submissions" / identifier).resolve()
        if (destination.is_relative_to(settings.root)
                and not destination.is_relative_to(settings.root / "work")):
            raise SubmissionExportError("Внутри репозитория экспорт разрешён только в work/.")
        if destination.exists() or destination.is_symlink():
            raise SubmissionExportError("Каталог экспорта уже существует; выберите новый путь.")
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".submission-", dir=destination.parent))
        os.chmod(staging, 0o700)
        source_hashes = {}
        for filename, source in CODE_FILES.items():
            value = _read_file(settings.root / source, 2_000_000)
            _write_file(staging / filename, value)
            source_hashes[filename] = hashlib.sha256(value).hexdigest()
        for filename, limit in DATA_FILES.items():
            _write_file(staging / filename, _read_file(data_root / filename, limit))
        _write_file(staging / POLICY_FILE, policy)
        # Only the libraries required by the offline agent/official evaluator.
        from importlib.metadata import version
        dependencies = "".join(f"{name}=={version(name)}\n" for name in ("numpy", "pandas"))
        _write_file(staging / "requirements.txt", dependencies.encode())
        subprocess.run([sys.executable, "make_submission.py"], cwd=staging,
                       env=_worker_environment(), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, timeout=min(settings.run_timeout_seconds, 600), umask=0o077)
        result_path = staging / "submission.csv"
        result = csv.DictReader(io.StringIO(_read_file(result_path, 128 * 1024).decode("utf-8")))
        if result.fieldnames != COLUMNS or list(result) != expected_rows:
            raise SubmissionExportError("План не воспроизвёлся: код или данные отличаются от API-запуска. Экспорт не сохранён.")
        os.chmod(result_path, 0o600)
        manifest = {"run_id": identifier, "seed": 42, "policy_sha256": policy_hash,
                    "source_sha256": source_hashes, "verified": True}
        _write_file(staging / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())
        _write_file(staging / "README.txt", (
            "Локальный воспроизводимый экспорт BeeSmart, seed 42.\n"
            "План проверен оригинальной командой: python make_submission.py\n"
            "Для сдачи нужны agent.py, пакет beesmart/agent с __init__.py,\n"
            "policies/frozen_policy.json, requirements.txt и submission.csv.\n"
            "Три CSV исходных данных здесь только для локальной проверки;\n"
            "не добавляйте их в Git и не отправляйте как исходный код.\n"
            "Официальные environment/scoring/mock/local_eval/make_submission.py\n"
            "скопированы без изменений для воспроизведения.\n"
            "Политика зафиксирована, ключи и сетевые обращения не требуются.\n"
        ).encode("utf-8"))
        for artifact in staging.rglob("*"):
            artifact.chmod(0o700 if artifact.is_dir() else 0o600)
        staging.rename(destination)
        staging = None
        return destination
    except SubmissionExportError:
        raise
    except subprocess.TimeoutExpired:
        raise SubmissionExportError("Проверка официальным генератором превысила лимит времени.") from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.CalledProcessError):
        raise SubmissionExportError("Не удалось воспроизвести запуск. Проверьте доступность отчёта, политики и трёх CSV.") from None
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, help="UUID завершённого API-запуска с seed 42")
    parser.add_argument("--storage-dir", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    settings = Settings.from_env()
    if arguments.storage_dir is not None:
        from dataclasses import replace
        settings = replace(settings, storage_dir=arguments.storage_dir)
    try:
        destination = build_submission(settings, arguments.run, output=arguments.output,
                                       data_dir=arguments.data_dir)
    except SubmissionExportError as error:
        parser.exit(1, f"{error}\n")
    print(destination)


if __name__ == "__main__":
    main()
