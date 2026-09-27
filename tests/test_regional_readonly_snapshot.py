from contextlib import closing
import hashlib
import importlib.util
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys

import pytest

from procurement.migrations import regional_mapping_v1 as migration
from procurement.migrations import readonly_snapshot as reader
from test_regional_mapping_migration import db  # identical guarded three-row fixture


def closed_wal(db):
    with closing(sqlite3.connect(db)) as c:
        assert c.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    assert db.read_bytes()[18:20] == b"\x02\x02"
    assert not Path(str(db) + "-wal").exists()
    return db


def test_closed_wal_readonly_filesystem_permissions(db):
    closed_wal(db)
    before = db.read_bytes()
    files = set(db.parent.iterdir())
    db.chmod(0o444)
    db.parent.chmod(0o555)
    try:
        result = migration.migrate(db)
        assert result["business_changes"] == 3
        assert result["reader"]["source_journal_mode"] == "wal"
        assert result["reader"]["source_sha256"] == hashlib.sha256(before).hexdigest()
        assert db.read_bytes() == before
        assert set(db.parent.iterdir()) == files
    finally:
        db.parent.chmod(0o700)
        db.chmod(0o600)


def test_planner_never_connects_sqlite_to_disk_or_runs_mutations(db, monkeypatch):
    closed_wal(db)
    original = sqlite3.connect
    opened, statements = [], []

    def tracked(path, **kwargs):
        opened.append(path)
        c = original(path, **kwargs)
        c.set_trace_callback(statements.append)
        return c

    monkeypatch.setattr(reader.sqlite3, "connect", tracked)
    assert migration.migrate(db)["business_changes"] == 3
    assert opened == [":memory:"]
    # SQLite's deserialize API emits one INTERNAL attach of the supplied RAM
    # buffer; it opens no file and is completed before the SELECT authorizer.
    assert statements[0] == "ATTACH x AS 'main'"
    assert all(s.startswith("SELECT ") or s == "PRAGMA integrity_check" for s in statements[1:]), statements
    assert not any("BEGIN" in s or "=" in s and s.startswith("PRAGMA") for s in statements)


@pytest.mark.parametrize("sql", [
    "CREATE TABLE forbidden(x)", "UPDATE suppliers SET cluster='bad'", "DELETE FROM suppliers",
    "INSERT INTO audit_log(actor) VALUES('bad')", "BEGIN IMMEDIATE", "PRAGMA journal_mode=DELETE",
    "PRAGMA query_only=1", "PRAGMA wal_checkpoint", "ATTACH DATABASE ':memory:' AS extra",
])
def test_memory_authorizer_denies_all_mutations(db, sql):
    with reader.readonly_snapshot(db) as (c, _):
        with pytest.raises(sqlite3.DatabaseError):
            c.execute(sql)


def test_active_writer_fails_closed_not_stale_plan(db):
    with closing(sqlite3.connect(db)) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("UPDATE suppliers SET cluster='cluster_2' WHERE id=2")
        c.commit()
        wal = Path(str(db) + "-wal")
        before = wal.read_bytes()
        with pytest.raises(migration.PreconditionError, match="read lease unavailable"):
            migration.migrate(db)
        assert wal.read_bytes() == before


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_nonempty_sidecars_never_ignored(db, suffix):
    sidecar = Path(str(db) + suffix)
    sidecar.write_bytes(b"must not be ignored")
    before = db.read_bytes()
    with pytest.raises(migration.PreconditionError, match="sidecar"):
        migration.migrate(db)
    assert db.read_bytes() == before
    assert sidecar.read_bytes() == b"must not be ignored"


def test_read_lease_failure_never_falls_back_to_immutable(db, monkeypatch):
    import fcntl
    original = fcntl.fcntl

    def fail(fd, command, arg=0):
        if command == fcntl.F_SETLEASE and arg == fcntl.F_RDLCK:
            raise OSError("unsupported")
        return original(fd, command, arg)

    monkeypatch.setattr(fcntl, "fcntl", fail)
    with pytest.raises(migration.PreconditionError, match="read lease unavailable"):
        migration.migrate(db)


def test_new_writer_breaks_lease_and_plan_aborts(db, monkeypatch):
    original = reader._sidecars_empty
    writers = []

    def concurrent_writer(path):
        if not writers:
            writers.append(subprocess.Popen([sys.executable, "-c",
                "import os,sys; f=os.open(sys.argv[1],os.O_RDWR); os.close(f)", str(path)]))
            signal.pause()
        original(path)

    monkeypatch.setattr(reader, "_sidecars_empty", concurrent_writer)
    try:
        with pytest.raises(migration.PreconditionError, match="concurrent writer"):
            migration.migrate(db)
    finally:
        for process in writers:
            assert process.wait(timeout=5) == 0
    assert migration.migrate(db)["business_changes"] == 3


def test_symlink_database_blocked(db, tmp_path):
    alias = tmp_path / "alias.db"
    alias.symlink_to(db)
    with pytest.raises(migration.PreconditionError, match="non-symlink"):
        migration.migrate(alias)


def test_snapshot_size_limit_and_signal_restored(db, monkeypatch):
    old = signal.getsignal(signal.SIGIO)
    monkeypatch.setattr(reader, "MAX_SNAPSHOT_BYTES", 100)
    with pytest.raises(migration.PreconditionError, match="bounded regular"):
        migration.migrate(db)
    assert signal.getsignal(signal.SIGIO) == old


def test_rehearsal_prechecks_do_not_create_wal_sidecars(db):
    closed_wal(db)
    module_path = Path(__file__).resolve().parents[1] / "scripts/rehearse_regional_mapping.py"
    spec = importlib.util.spec_from_file_location("readonly_rehearsal_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    before = db.read_bytes()
    module.check_backup(db)
    assert len(module.business(db)["suppliers"]) == 2
    assert migration.migrate(db)["business_changes"] == 3
    assert db.read_bytes() == before
    assert not Path(str(db) + "-wal").exists()
    assert not Path(str(db) + "-shm").exists()
