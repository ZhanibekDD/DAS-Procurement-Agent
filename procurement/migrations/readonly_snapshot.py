"""Physically read-only, bounded Linux snapshot for the offline migration planner.

Never give SQLite a live path: mode=ro can create WAL sidecars. A read lease
excludes existing writable descriptors and detects a new writer while bytes are
read into RAM. Nonempty WAL/journal files fail closed, never silently disappear.
This deliberately does not support planning against an active WAL writer.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import signal
import sqlite3
import stat
import sys
import threading

MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024


class SnapshotBlocked(ValueError):
    pass


def _sidecars_empty(path: Path) -> None:
    for suffix in ("-wal", "-journal", "-shm"):
        sidecar = Path(str(path) + suffix)
        try:
            info = sidecar.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size:
            raise SnapshotBlocked("nonempty or unsafe SQLite sidecar; consistent snapshot required")


def read_stable_bytes(path: Path) -> bytes:
    if sys.platform != "linux" or threading.current_thread() is not threading.main_thread():
        raise SnapshotBlocked("read-only planner requires Linux main thread and file leases")
    import fcntl

    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    held = False
    previous = signal.getsignal(signal.SIGIO)

    def break_lease(_signum, _frame):
        nonlocal held
        if held:
            fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_UNLCK)
            held = False
        raise SnapshotBlocked("concurrent writer requested access; read-only plan aborted")

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 100 <= info.st_size <= MAX_SNAPSHOT_BYTES:
            raise SnapshotBlocked("database is not a bounded regular SQLite file")
        signal.signal(signal.SIGIO, break_lease)
        try:
            fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_RDLCK)
            held = True
        except OSError as exc:
            raise SnapshotBlocked("read lease unavailable: database active, ownership or filesystem unsupported") from exc
        _sidecars_empty(path)
        data = bytearray()
        while len(data) < info.st_size:
            chunk = os.read(fd, min(1024 * 1024, info.st_size - len(data)))
            if not chunk:
                raise SnapshotBlocked("database changed during RAM snapshot")
            data.extend(chunk)
        after = os.fstat(fd)
        named = path.stat()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(info, k) != getattr(after, k) or getattr(info, k) != getattr(named, k) for k in fields):
            raise SnapshotBlocked("database identity changed during RAM snapshot")
        _sidecars_empty(path)
        if fcntl.fcntl(fd, fcntl.F_GETLEASE) != fcntl.F_RDLCK:
            raise SnapshotBlocked("read lease was broken; read-only plan aborted")
        return bytes(data)
    finally:
        try:
            if held:
                fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_UNLCK)
        finally:
            os.close(fd)
            signal.signal(signal.SIGIO, previous)


def select_only(action, arg1, arg2, _database, _trigger):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and arg2 == "count":
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg2 is None and arg1 in {
        "database_list", "query_only", "journal_mode", "integrity_check", "foreign_key_check"
    }:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


@contextmanager
def readonly_snapshot(path: Path):
    data = read_stable_bytes(path)
    if data[:16] != b"SQLite format 3\x00" or data[18:20] not in (b"\x01\x01", b"\x02\x02"):
        raise SnapshotBlocked("unsupported SQLite header")
    source_sha = hashlib.sha256(data).hexdigest()
    source_mode = "wal" if data[18] == 2 else "rollback"
    # SQLite documents this adaptation for deserialize(). Only PRIVATE RAM bytes
    # change; the source file and all source PRAGMAs remain entirely untouched.
    data = data[:18] + b"\x01\x01" + data[20:]
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.deserialize(data)
        conn.row_factory = sqlite3.Row
        conn.set_authorizer(select_only)
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SnapshotBlocked("RAM snapshot integrity failed")
        yield conn, {"source_sha256": source_sha, "source_journal_mode": source_mode,
                     "storage": "private RAM", "source_open_flags": "O_RDONLY|O_CLOEXEC|O_NOFOLLOW",
                     "source_guard": "Linux F_RDLCK lease; nonempty sidecars rejected"}
    finally:
        conn.close()
