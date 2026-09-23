"""Bounded CSV-only dataset storage for the local run endpoint.

No client filenames, paths, Python modules, or executable objects are accepted.
Validation uses the same CSV interpretation as the official pandas evaluator.
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import BinaryIO

import numpy as np
import pandas as pd

from beesmart.config import LIMITS


class UploadValidationError(ValueError):
    """Safe, user-facing explanation of an invalid CSV dataset."""


MAX_BYTES = {"profile": 32 * 1024 * 1024, "history": 16 * 1024 * 1024, "tariffs": 256 * 1024}
MAX_ROWS = {"profile": 100_000, "history": 200_000, "tariffs": 100}
MAX_COLUMNS = 128
FILENAMES = {"profile": "customer_profile.csv", "history": "data/change_tariff.csv", "tariffs": "data/dict_tariff.csv"}
REQUIRED_COLUMNS = {
    "profile": {"ID_NUMBER", "current_tariff", "arpu_segment", "data_segment", "call_segment", "predicted_arpu"},
    "history": {"ID_NUMBER", "tariff_plan_code_from", "tariff_plan_code_to", "AVG_ARPU_PREV_3M", "AVG_ARPU_NEXT_3M"},
    "tariffs": {"tariff_plan_code", "price_tariff"},
}
CATEGORIES = {"arpu_segment": {"LOW", "MID", "HIGH"},
              "data_segment": {"NON_USER", "LITE", "HEAVY"},
              "call_segment": {"LOW", "MEDIUM", "HIGH"}}
DATASET_ID = re.compile(r"[0-9a-f]{32}\Z")
TARIFF_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}\Z")


class UploadStore:
    """Store successful datasets atomically; preserve up to five recent sets.

    The web layer serializes uploads and execution. ``set_active`` additionally
    protects a running dataset if the store is used by another caller.
    Methods are synchronous and may be dispatched through ``asyncio.to_thread``.
    """

    def __init__(self, root: Path | str | None = None, max_datasets: int = 5):
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[1] / "work" / "datasets"
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()
        if max_datasets < 1:
            raise ValueError("max_datasets must be positive")
        self.max_datasets = max_datasets
        self._active_id: str | None = None
        self._lock = threading.RLock()

    def path(self, dataset_id: str) -> Path | None:
        if not isinstance(dataset_id, str) or not DATASET_ID.fullmatch(dataset_id):
            return None
        candidate = self.root / dataset_id
        if candidate.is_symlink() or not candidate.is_dir():
            return None
        return candidate

    def set_active(self, dataset_id: str | None) -> None:
        with self._lock:
            if dataset_id is not None and self.path(dataset_id) is None:
                raise UploadValidationError("Набор данных не найден.")
            self._active_id = dataset_id

    def discard(self, dataset_id: str) -> None:
        with self._lock:
            if dataset_id == self._active_id and dataset_id is not None:
                raise UploadValidationError("Набор данных используется текущим запуском.")
            directory = self.path(dataset_id)
            if directory is not None:
                shutil.rmtree(directory)

    def create(self, files: dict[str, BinaryIO]) -> dict:
        if set(files) != set(FILENAMES):
            raise UploadValidationError("Нужны ровно три CSV: profile, history и tariffs.")
        with self._lock:
            dataset_id = uuid.uuid4().hex
            staging = self.root / f".upload-{dataset_id}"
            destination = self.root / dataset_id
            staging.mkdir(mode=0o700)
            try:
                (staging / "data").mkdir(mode=0o700)
                for role, relative in FILENAMES.items():
                    self._copy_csv(files[role], staging / relative, role)
                frames = {role: self._read_csv(staging / name, role) for role, name in FILENAMES.items()}
                self._validate(frames)
                metadata = {"id": dataset_id, "customers": len(frames["profile"]),
                            "history_rows": len(frames["history"]), "tariffs": len(frames["tariffs"])}
                (staging / "metadata.json").write_text(json.dumps({**metadata, "created_ns": time.time_ns()}), encoding="utf-8")
                staging.rename(destination)
                self._prune(protected=dataset_id)
                return metadata
            except BaseException:
                # Includes parser failures, quota errors, disk errors, and task
                # cancellation: partial input never becomes a usable dataset.
                shutil.rmtree(staging, ignore_errors=True)
                shutil.rmtree(destination, ignore_errors=True)
                raise

    @staticmethod
    def _copy_csv(source: BinaryIO, target: Path, role: str) -> None:
        written = 0
        with target.open("xb") as output:
            while True:
                chunk = source.read(64 * 1024)
                if not isinstance(chunk, bytes):
                    raise UploadValidationError(f"{role}: ожидается бинарный CSV-файл.")
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_BYTES[role]:
                    raise UploadValidationError(f"{role}: превышен допустимый размер CSV.")
                if b"\0" in chunk:
                    raise UploadValidationError(f"{role}: CSV содержит недопустимые нулевые байты.")
                output.write(chunk)
        if written == 0:
            raise UploadValidationError(f"{role}: CSV-файл пуст.")

    @staticmethod
    def _read_csv(path: Path, role: str) -> pd.DataFrame:
        try:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                header = next(csv.reader(stream))
            if len(header) > MAX_COLUMNS:
                raise UploadValidationError(f"{role}: превышен лимит {MAX_COLUMNS} колонок.")
            if not header or any(not name for name in header) or len(set(header)) != len(header):
                raise UploadValidationError(f"{role}: заголовки колонок должны быть непустыми и уникальными.")
            # Reading one extra row establishes the limit without loading an
            # arbitrarily long table; raw byte limits also bound quoted fields.
            frame = pd.read_csv(path, encoding="utf-8-sig", nrows=MAX_ROWS[role] + 1)
        except UploadValidationError:
            raise
        except (UnicodeError, csv.Error, StopIteration, pd.errors.ParserError, pd.errors.EmptyDataError, ValueError):
            raise UploadValidationError(f"{role}: нужен корректный CSV в UTF-8 с разделителем-запятой.") from None
        if frame.empty:
            raise UploadValidationError(f"{role}: CSV не содержит строк данных.")
        if len(frame) > MAX_ROWS[role]:
            raise UploadValidationError(f"{role}: превышен лимит {MAX_ROWS[role]} строк.")
        missing = REQUIRED_COLUMNS[role] - set(frame.columns)
        if missing:
            raise UploadValidationError(f"{role}: отсутствуют обязательные колонки: {', '.join(sorted(missing))}.")
        if not isinstance(frame.index, pd.RangeIndex):
            # pandas can infer an index from surplus fields instead of raising.
            raise UploadValidationError(f"{role}: число значений в строках не соответствует заголовку.")
        return frame

    @staticmethod
    def _numeric(frame: pd.DataFrame, column: str, role: str, *, nonnegative: bool = False) -> pd.Series:
        # Reject strings that the official unmodified evaluator would leave as
        # object dtype, even if a separate coercion could have recovered them.
        if pd.api.types.is_bool_dtype(frame[column]) or not pd.api.types.is_numeric_dtype(frame[column]):
            raise UploadValidationError(f"{role}: колонка {column} должна содержать числа.")
        values = frame[column]
        if not bool(np.isfinite(values.to_numpy(dtype=float)).all()):
            raise UploadValidationError(f"{role}: колонка {column} содержит пропуски или бесконечные значения.")
        if nonnegative and bool((values < 0).any()):
            raise UploadValidationError(f"{role}: колонка {column} не может содержать отрицательные значения.")
        return values

    @classmethod
    def _validate(cls, frames: dict[str, pd.DataFrame]) -> None:
        profile, history, tariffs = (frames[name] for name in ("profile", "history", "tariffs"))
        reserved = {"ratio", "lift_ratio", "expected_lift_per_customer"} & set(profile.columns)
        if reserved:
            raise UploadValidationError("profile: обнаружены служебные колонки расчёта эффекта.")
        for role, frame in (("profile", profile), ("history", history)):
            ids = frame["ID_NUMBER"]
            if ids.isna().any() or ids.astype(str).str.strip().eq("").any():
                raise UploadValidationError(f"{role}: ID_NUMBER не может быть пустым.")
        if profile["ID_NUMBER"].duplicated().any():
            raise UploadValidationError("profile: ID_NUMBER должен быть уникальным.")
        # Repeated history IDs and negative previous ARPU exist in the official
        # dataset. History is a sequence of events, not a unique customer table.
        arpu = cls._numeric(profile, "predicted_arpu", "profile", nonnegative=True)
        if not is_positive_finite_sum(arpu):
            raise UploadValidationError("profile: суммарный predicted_arpu должен быть положительным и не переполнять числовой диапазон расчёта.")
        previous = cls._numeric(history, "AVG_ARPU_PREV_3M", "history")
        following = cls._numeric(history, "AVG_ARPU_NEXT_3M", "history")
        if not bool((previous >= 100).any()):
            raise UploadValidationError("history: нужна хотя бы одна строка с AVG_ARPU_PREV_3M ≥ 100.")
        if pd.api.types.is_integer_dtype(previous.dtype) and pd.api.types.is_integer_dtype(following.dtype):
            difference_dtype = np.result_type(previous.dtype, following.dtype)
            if np.issubdtype(difference_dtype, np.integer):
                bounds = np.iinfo(difference_dtype)
                if any(not bounds.min <= int(after) - int(before) <= bounds.max
                       for before, after in zip(previous, following) if before >= 100):
                    raise UploadValidationError("history: разность ARPU переполняет числовой диапазон расчёта.")
        cls._numeric(tariffs, "price_tariff", "tariffs", nonnegative=True)
        codes = tariffs["tariff_plan_code"]
        if codes.isna().any() or codes.duplicated().any():
            raise UploadValidationError("tariffs: коды тарифов должны быть непустыми и уникальными.")
        if not codes.map(lambda value: isinstance(value, str) and bool(TARIFF_CODE.fullmatch(value))).all():
            raise UploadValidationError("tariffs: используйте буквенные коды тарифов до 64 символов: буквы, цифры, _, . и -.")
        known = set(codes)
        if len(known) < 2:
            raise UploadValidationError("tariffs: нужны как минимум два тарифа для проверки переходов.")
        for role, frame, columns in (("profile", profile, ("current_tariff",)),
                                     ("history", history, ("tariff_plan_code_from", "tariff_plan_code_to"))):
            for column in columns:
                if role == "history" and frame[column].isna().any():
                    raise UploadValidationError(f"{role}: колонка {column} не может быть пустой.")
                # Blank current tariffs exist in the original profile. They
                # remain in the baseline but are not pilotable filter cells.
                if not set(frame[column].dropna()).issubset(known):
                    raise UploadValidationError(f"{role}: колонка {column} ссылается на неизвестный тариф.")
        for column, allowed in CATEGORIES.items():
            if not set(profile[column].dropna()).issubset(allowed):
                raise UploadValidationError(f"profile: недопустимое значение в колонке {column}.")
        pilotable = profile.dropna(subset=["current_tariff", "arpu_segment"])
        counts = pilotable.groupby(["current_tariff", "arpu_segment"], observed=True).size()
        if counts.empty or int(counts.max()) < 10:
            raise UploadValidationError("profile: нужна хотя бы одна ячейка (тариф, ARPU-сегмент) из 10 абонентов для пилота.")
        # Only whole rectangles can be addressed. A 6,000-person homogeneous
        # cell is pilotable but cannot produce any legal final campaign. Match
        # the real filters: absent optional fields include missing values;
        # present optional fields select a nonmissing category.
        base = ["current_tariff", "arpu_segment"]
        has_final_segment = False
        for extra in ([], ["data_segment"], ["call_segment"], ["data_segment", "call_segment"]):
            sizes = profile.groupby(base + extra, dropna=True, observed=True).size()
            if bool(sizes.between(1, 5000).any()):
                has_final_segment = True
                break
        if not has_final_segment:
            raise UploadValidationError("profile: нужен хотя бы один полный сегмент по разрешённым фильтрам от 1 до 5000 абонентов.")

    def _prune(self, protected: str) -> None:
        candidates = [entry for entry in self.root.iterdir() if DATASET_ID.fullmatch(entry.name)
                      and entry.is_dir() and not entry.is_symlink()]
        candidates.sort(key=lambda path: (path.stat().st_mtime_ns, path.name))
        excess = max(0, len(candidates) - self.max_datasets)
        for directory in candidates:
            if excess == 0:
                break
            if directory.name in (protected, self._active_id):
                continue
            shutil.rmtree(directory)
            excess -= 1


def is_positive_finite_sum(values: pd.Series) -> bool:
    if pd.api.types.is_integer_dtype(values.dtype):
        # The unmodified evaluator sums the original integer dtype. A wrapped
        # positive int64 sum must not be mistaken for a valid baseline.
        dtype = getattr(values.dtype, "numpy_dtype", values.dtype)
        total = sum(map(int, values))
        return 0 < total <= np.iinfo(dtype).max
    with np.errstate(over="ignore", invalid="ignore"):
        total = float(values.sum())
    # Growth is evaluated as 100 * net_gain / baseline, in that order. Leave
    # room for the clipped effect (3), channel multiplier (1.2), and percentage
    # multiplication, and for dividing the maximum contact loss by baseline.
    maximum = np.finfo(np.float64).max
    minimum_baseline = 8 * 100 * LIMITS["total_budget"] / maximum
    maximum_baseline = maximum / (8 * 100)
    return bool(np.isfinite(total) and minimum_baseline <= total <= maximum_baseline)
