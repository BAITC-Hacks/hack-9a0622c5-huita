"""Uploaded bytes cannot escape their dataset; valid original CSVs still work."""

import io
from pathlib import Path

import pandas as pd
import pytest

from beesmart.uploads import FILENAMES, MAX_BYTES, MAX_ROWS, UploadStore, UploadValidationError


def frames():
    profile = pd.DataFrame({"ID_NUMBER": range(1, 13), "current_tariff": ["tariff_a"] * 12,
                            "arpu_segment": ["MID"] * 12, "data_segment": ["HEAVY"] * 12,
                            "call_segment": ["LOW"] * 12, "predicted_arpu": [1500.0] * 12})
    history = pd.DataFrame({"ID_NUMBER": [1, 1, 2], "tariff_plan_code_from": ["tariff_a"] * 3,
                            "tariff_plan_code_to": ["tariff_b"] * 3,
                            "AVG_ARPU_PREV_3M": [1000.0, -37.41, 1000.0],
                            "AVG_ARPU_NEXT_3M": [1500.0, 0.0, 2000.0]})
    tariffs = pd.DataFrame({"tariff_plan_code": ["tariff_a", "tariff_b"], "price_tariff": [1000.0, 2000.0]})
    return {"profile": profile, "history": history, "tariffs": tariffs}


def streams(data=None):
    return {role: io.BytesIO(frame.to_csv(index=False).encode()) for role, frame in (frames() if data is None else data).items()}


def test_store_uses_generated_id_and_fixed_structure(tmp_path):
    store = UploadStore(tmp_path)
    metadata = store.create(streams())
    assert metadata == {"id": metadata["id"], "customers": 12, "history_rows": 3, "tariffs": 2}
    directory = store.path(metadata["id"])
    assert directory is not None and directory.parent == tmp_path
    assert all((directory / filename).is_file() for filename in FILENAMES.values())
    assert not list(tmp_path.glob(".upload-*"))


def test_missing_optional_values_and_repeated_history_events_are_allowed(tmp_path):
    data = frames()
    data["profile"].loc[0, ["current_tariff", "arpu_segment", "data_segment", "call_segment"]] = None
    metadata = UploadStore(tmp_path).create(streams(data))
    assert metadata["customers"] == 12


@pytest.mark.parametrize("role,column,values", [
    ("profile", "ID_NUMBER", [1] * 12),
    ("profile", "predicted_arpu", [float("inf")] * 12),
    ("profile", "predicted_arpu", [-1] * 12),
    ("profile", "predicted_arpu", [0] * 12),
    ("profile", "arpu_segment", ["UNKNOWN"] * 12),
    ("profile", "current_tariff", ["missing_tariff"] * 12),
    ("profile", "ratio", [0.1] * 12),
    ("history", "AVG_ARPU_PREV_3M", [0, 1, 2]),
    ("history", "tariff_plan_code_to", ["missing_tariff"] * 3),
    ("tariffs", "price_tariff", ["free", "2000"]),
    ("tariffs", "price_tariff", [True, False]),
    ("tariffs", "tariff_plan_code", ["tariff_a", "tariff_a"]),
    ("tariffs", "tariff_plan_code", ["tariff_a", "=HYPERLINK(1)"]),
])
def test_invalid_dataset_cleans_up_atomically(tmp_path, role, column, values):
    data = frames()
    data[role] = data[role].assign(**{column: values})
    with pytest.raises(UploadValidationError):
        UploadStore(tmp_path).create(streams(data))
    assert list(tmp_path.iterdir()) == []


def test_role_names_cannot_be_file_paths(tmp_path):
    files = streams()
    files["../../outside.py"] = files.pop("profile")
    with pytest.raises(UploadValidationError):
        UploadStore(tmp_path).create(files)
    assert list(tmp_path.iterdir()) == []


def test_byte_and_row_quotas_remove_partial_files(tmp_path, monkeypatch):
    store = UploadStore(tmp_path)
    monkeypatch.setitem(MAX_BYTES, "profile", 10)
    with pytest.raises(UploadValidationError, match="размер"):
        store.create(streams())
    assert not list(tmp_path.iterdir())
    monkeypatch.setitem(MAX_BYTES, "profile", 32 * 1024 * 1024)
    monkeypatch.setitem(MAX_ROWS, "profile", 10)
    with pytest.raises(UploadValidationError, match="строк"):
        store.create(streams())
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("content", [b"", b"not,csv\n\xff\xff", b"a,a\n1,2\n", b"a\n\x00\n"])
def test_malformed_csv_is_rejected_without_leaving_data(tmp_path, content):
    files = streams()
    files["profile"] = io.BytesIO(content)
    with pytest.raises(UploadValidationError):
        UploadStore(tmp_path).create(files)
    assert not list(tmp_path.iterdir())


def test_client_filename_is_ignored_and_path_lookup_rejects_traversal(tmp_path):
    store = UploadStore(tmp_path)
    files = streams()
    files["profile"].name = "../../agent.py"
    metadata = store.create(files)
    assert store.path(metadata["id"]) is not None
    assert store.path("../../agent.py") is None
    assert store.path("/tmp") is None
    store.discard("../outside")


def test_retention_keeps_five_and_protects_active_set(tmp_path):
    store = UploadStore(tmp_path)
    first = store.create(streams())["id"]
    store.set_active(first)
    identifiers = [store.create(streams())["id"] for _ in range(6)]
    assert store.path(first) is not None
    assert store.path(identifiers[-1]) is not None
    assert len(list(tmp_path.iterdir())) == 5
    assert store.path(identifiers[0]) is None
    with pytest.raises(UploadValidationError, match="используется"):
        store.discard(first)
    store.set_active(None)
    store.discard(first)
    assert store.path(first) is None


def test_symlink_dataset_cannot_be_accessed_or_deleted(tmp_path):
    store = UploadStore(tmp_path / "store")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = store.root / ("a" * 32)
    link.symlink_to(outside, target_is_directory=True)
    assert store.path(link.name) is None
    store.discard(link.name)
    assert outside.is_dir() and link.is_symlink()


def test_pilotable_but_unaddressable_homogeneous_audience_is_rejected(tmp_path):
    data = frames()
    data["profile"] = pd.concat([data["profile"]] * 500, ignore_index=True)
    data["profile"]["ID_NUMBER"] = range(1, 6001)
    store = UploadStore(tmp_path)
    with pytest.raises(UploadValidationError, match="полный сегмент"):
        store.create(streams(data))
    assert not list(tmp_path.iterdir())
    # A real categorical split supplies two complete addressable segments.
    data["profile"].loc[:2999, "data_segment"] = "LITE"
    assert store.create(streams(data))["customers"] == 6000


def test_excessive_columns_are_rejected_before_pandas_parsing(tmp_path, monkeypatch):
    files = streams()
    files["profile"] = io.BytesIO((",".join(f"column_{i}" for i in range(129)) + "\n").encode())

    def forbidden_parse(*args, **kwargs):
        raise AssertionError("Excessively wide CSV must be rejected before pandas allocates columns")

    monkeypatch.setattr(pd, "read_csv", forbidden_parse)
    with pytest.raises(UploadValidationError, match="128 колонок"):
        UploadStore(tmp_path).create(files)
    assert not list(tmp_path.iterdir())


def test_original_organizer_csvs_pass_when_present(tmp_path):
    root = Path(__file__).resolve().parents[1]
    if not all((root / filename).is_file() for filename in FILENAMES.values()):
        pytest.skip("Organizer datasets are kept local and are not committed")
    handles = {role: (root / filename).open("rb") for role, filename in FILENAMES.items()}
    try:
        metadata = UploadStore(tmp_path).create(handles)
    finally:
        for handle in handles.values():
            handle.close()
    assert metadata["customers"] == len(pd.read_csv(root / FILENAMES["profile"]))
    assert metadata["history_rows"] == len(pd.read_csv(root / FILENAMES["history"]))
    assert metadata["tariffs"] == len(pd.read_csv(root / FILENAMES["tariffs"]))
