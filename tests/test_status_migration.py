import importlib.util
import signal

import pytest

from harmonized.common import ROOT

spec = importlib.util.spec_from_file_location("migration", ROOT / "code/migrate_status_to_main.py")
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def test_status_migration_never_stops_preprocessing(monkeypatch):
    monkeypatch.setattr(migration, "process_args", lambda pid: ["python", "/project/code/preprocess_istart.py", "worker"])
    kills = []
    monkeypatch.setattr(migration.os, "killpg", lambda *args: kills.append(args))
    with pytest.raises(ValueError, match="not the status watcher"):
        migration.stop_publisher({"pid": 12345})
    assert kills == []


def test_status_migration_stops_only_verified_detached_watcher(monkeypatch):
    calls = iter([["python", "/project/code/publish_processing_status.py", "watch"], None])
    monkeypatch.setattr(migration, "process_args", lambda pid: next(calls))
    monkeypatch.setattr(migration.os, "getpgid", lambda pid: pid)
    kills = []
    monkeypatch.setattr(migration.os, "killpg", lambda *args: kills.append(args))
    migration.stop_publisher({"pid": 12345})
    assert kills == [(12345, signal.SIGTERM)]


def test_status_migration_refuses_shared_process_group(monkeypatch):
    monkeypatch.setattr(migration, "process_args", lambda pid: ["python", "/project/code/publish_processing_status.py", "watch"])
    monkeypatch.setattr(migration.os, "getpgid", lambda pid: 67890)
    kills = []
    monkeypatch.setattr(migration.os, "killpg", lambda *args: kills.append(args))
    with pytest.raises(ValueError, match="unexpected process group"):
        migration.stop_publisher({"pid": 12345})
    assert kills == []
