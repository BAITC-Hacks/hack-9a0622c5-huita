"""Disk persistence must not block the API or launch canceled requests."""

import asyncio
import json
import threading
import pytest

from beesmart.config import Settings
from beesmart.runs import RunBusyError, RunManager


def test_slow_persistence_keeps_loop_responsive_and_reserves_start(tmp_path, monkeypatch):
    async def scenario():
        manager = RunManager(Settings(root=tmp_path))
        entered, release = threading.Event(), threading.Event()
        save = manager._save_report
        executed = []

        def blocked_save(record):
            entered.set()
            if not release.wait(timeout=3):
                raise TimeoutError("Test did not release the persistence thread")
            save(record)

        async def execute(record):
            executed.append(record["id"])

        monkeypatch.setattr(manager, "_save_report", blocked_save)
        monkeypatch.setattr(manager, "_execute", execute)
        starting = asyncio.create_task(manager.start(42))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=1.5)
            # This callback executes while disk persistence remains blocked.
            responsive = asyncio.Event()
            asyncio.get_running_loop().call_soon(responsive.set)
            await asyncio.wait_for(responsive.wait(), timeout=0.2)
            assert manager._starting and not starting.done()
            with pytest.raises(RunBusyError):
                await manager.start(43)
            with pytest.raises(RunBusyError):
                manager.reserve_upload()
            assert not executed
        finally:
            release.set()
        record = await asyncio.wait_for(starting, timeout=1)
        await asyncio.sleep(0)
        assert executed == [record["id"]]
        assert not manager._starting
        await manager.close()

    asyncio.run(scenario())


def test_canceled_start_finishes_write_marks_failed_and_never_launches_worker(tmp_path, monkeypatch):
    async def scenario():
        manager = RunManager(Settings(root=tmp_path))
        entered, release = threading.Event(), threading.Event()
        save = manager._save_report
        snapshots, executed = [], []

        def blocked_first_write(record):
            snapshots.append(record["status"])
            if len(snapshots) == 1:
                entered.set()
                if not release.wait(timeout=3):
                    raise TimeoutError("Test did not release the persistence thread")
            save(record)

        async def execute(record):
            executed.append(record["id"])

        monkeypatch.setattr(manager, "_save_report", blocked_first_write)
        monkeypatch.setattr(manager, "_execute", execute)
        starting = asyncio.create_task(manager.start(42))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=1.5)
            starting.cancel()
            await asyncio.sleep(0)
            # Repeated cancellation must not abandon the ongoing write thread.
            starting.cancel()
            await asyncio.sleep(0)
            assert manager._starting and not starting.done()
            with pytest.raises(RunBusyError):
                manager.reserve_upload()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(starting, timeout=1)
        assert snapshots == ["queued", "failed"]
        assert not executed and manager._active is None
        assert not manager._starting
        record = manager.latest()
        assert record["status"] == "failed"
        assert record["finished_at"] is not None
        disk = json.loads((manager.settings.storage_path / "runs" / f"{record['id']}.json").read_text())
        assert disk["status"] == "failed"
        assert not list((manager.settings.storage_path / "runs").glob("*.tmp"))
        manager.reserve_upload()
        manager.release_upload()

    asyncio.run(scenario())


def test_report_failure_cleans_temporary_file_and_releases_start(tmp_path, monkeypatch):
    async def scenario():
        manager = RunManager(Settings(root=tmp_path))

        def fail_fsync(_):
            raise OSError("Simulated disk failure")

        monkeypatch.setattr("beesmart.runs.os.fsync", fail_fsync)
        with pytest.raises(OSError, match="Simulated"):
            await manager.start(42)
        assert not manager._starting and manager._active is None
        assert not list((manager.settings.storage_path / "runs").glob("*.tmp"))
        assert not list((manager.settings.storage_path / "runs").glob("*.json"))
        manager.reserve_upload()
        manager.release_upload()

    asyncio.run(scenario())
