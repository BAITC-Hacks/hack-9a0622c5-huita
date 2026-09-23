from copy import deepcopy
from pathlib import Path
from threading import Lock

import numpy as np
import pandas as pd

from beesmart.config import LIMITS, Settings
from beesmart.serialization import json_safe


class DatasetRepository:
    """Read only organizer files; cache aggregates, not request-sized copies."""

    FILES = {
        "customer_profile": "customer_profile.csv",
        "change_tariff": "data/change_tariff.csv",
        "dict_tariff": "data/dict_tariff.csv",
        "traffic": "data/traffic.csv",
        "arpu_monthly": "data/arpu_monthly.csv",
        "feature_dictionary": "feature_dictionary.csv",
        "tariff_dictionary": "tariff_dictionary.csv",
    }
    RUNTIME_FILES = (
        "environment.py", "mock_environment.py", "scoring_core.py",
        "local_eval.py", "make_submission.py", "agent.py",
    )
    PROFILE_COLUMNS = (
        "ID_NUMBER", "current_tariff", "arpu_segment", "data_segment",
        "call_segment", "predicted_arpu",
    )

    def __init__(self, settings: Settings):
        self.root = settings.root.resolve()
        self._lock = Lock()
        self._fingerprint = None
        self._summary = None

    def file_path(self, file_id: str) -> Path | None:
        relative = self.FILES.get(file_id)
        if relative is None:
            return None
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return None
        return path

    def overview(self) -> dict:
        with self._lock:
            required = [self.root / self.FILES[key] for key in (
                "customer_profile", "dict_tariff", "change_tariff",
            )]
            fingerprint = tuple(
                (str(path), path.stat().st_mtime_ns, path.stat().st_size)
                if path.is_file() else (str(path), None, None)
                for path in required
            )
            if fingerprint != self._fingerprint:
                self._summary = self._read_summary(required)
                self._fingerprint = fingerprint
            result = deepcopy(self._summary)

        missing_runtime = [name for name in self.RUNTIME_FILES if not (self.root / name).is_file()]
        result["runtime"] = {
            "ready": not missing_runtime and result["dataset"]["status"] == "ready",
            "missing_files": missing_runtime,
            "message": "Локальная мок-среда организаторов" if not missing_runtime else "Не хватает файлов среды",
        }
        result["project"] = {
            "name": "BeeSmart", "owner": "Билайн",
            "case_name": "Beeline Tariff Marketing Campaigns Case",
        }
        result["limits"] = LIMITS
        result["providers"] = [
            {"id": key, "label": label, "budget_usd": 50, "app_spend_usd": 0,
             "account_balance_usd": None, "runtime_calls": False}
            for key, label in (("openai", "OpenAI"), ("nvidia", "Brev / NVIDIA"))
        ]
        result["files"] = [
            {"id": key, "name": relative, "bytes": path.stat().st_size,
             "url": f"/api/data/{key}"}
            for key, relative in self.FILES.items()
            if (path := self.file_path(key)) is not None
        ]
        return result

    def _read_summary(self, required: list[Path]) -> dict:
        missing = [str(path.relative_to(self.root)) for path in required if not path.is_file()]
        empty = {
            "dataset": {"status": "missing", "customers": 0, "tariffs": 0,
                        "baseline_arpu": None, "cells": 0, "missing_files": missing,
                        "message": "Добавьте файлы организаторов", "quality": {}},
            "segments": [], "tariffs": [],
        }
        if missing:
            return empty
        try:
            profile = pd.read_csv(required[0], usecols=list(self.PROFILE_COLUMNS))
            tariffs = pd.read_csv(required[1])
            if "tariff_plan_code" not in tariffs or "price_tariff" not in tariffs:
                raise ValueError("В справочнике отсутствуют обязательные колонки")
            arpu = pd.to_numeric(profile["predicted_arpu"], errors="coerce")
            if profile.empty or profile["ID_NUMBER"].isna().any() or profile["ID_NUMBER"].duplicated().any():
                raise ValueError("Профиль пуст или содержит пустые/повторяющиеся ID")
            if not np.isfinite(arpu.to_numpy()).all() or (arpu < 0).any():
                raise ValueError("Некорректные значения predicted_arpu")
            profile["predicted_arpu"] = arpu
            grouped = profile.groupby(["current_tariff", "arpu_segment"], observed=True)
            segments = grouped.agg(
                customers=("ID_NUMBER", "size"), arpu_sum=("predicted_arpu", "sum"),
                arpu_mean=("predicted_arpu", "mean"),
            ).reset_index().sort_values(
                ["arpu_sum", "current_tariff", "arpu_segment"], ascending=[False, True, True],
                kind="stable",
            )
            return json_safe({
                "dataset": {
                    "status": "ready", "customers": len(profile), "tariffs": len(tariffs),
                    "baseline_arpu": float(arpu.sum()), "cells": len(segments),
                    "missing_files": [], "message": "Оригинальная синтетическая база организаторов",
                    "quality": {f"missing_{col}": int(profile[col].isna().sum())
                                for col in ("arpu_segment", "data_segment", "call_segment")},
                },
                "segments": segments.to_dict(orient="records"),
                "tariffs": tariffs.to_dict(orient="records"),
            })
        except (ValueError, OSError, KeyError, pd.errors.ParserError):
            empty["dataset"].update(status="error", message="Не удалось проверить CSV: проверьте колонки, ID и ARPU")
            return empty

