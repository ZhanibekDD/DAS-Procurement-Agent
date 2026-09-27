"""An allowlisted three-row migration. Default invocation performs SELECTs only.

No application Database wrapper: its connection() changes journal mode. Writes
are possible only via explicit --apply/--rollback with all approval parameters.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from procurement import regions
from procurement.migrations.readonly_snapshot import SnapshotBlocked, readonly_snapshot

MIGRATION_ID = "20260904_regional_mapping_v1"
MAPPING_SHA256 = "6359d5adc6c5255b4721c378b6849b471881b92bb55f1beb6f69c6147f48aeda"
JOURNAL = "procurement_regional_mapping_journal_v1"
TARGETS = (
    {"table": "suppliers", "id": 2, "before": "cluster_1", "after": "cluster_2",
     "identity": {"tax_id": "990831123457", "region": "Воронежская область",
                  "active": 1, "verified": 1, "created_at": "2026-08-31T17:45:50+00:00"}},
    {"table": "projects", "id": 1, "before": "", "after": "cluster_2",
     "identity": {"region": "Воронежская область", "status": "draft"}},
    {"table": "lots", "id": 1, "before": "", "after": "cluster_2",
     "identity": {"region": "Воронежская область", "status": "draft", "project_id": 1}},
)


class PreconditionError(ValueError):
    pass


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


PLAN_SHA256 = digest(TARGETS)


def check_mapping(supplied: str | None) -> None:
    actual = hashlib.sha256(Path(regions.__file__).read_bytes()).hexdigest()
    if actual != MAPPING_SHA256 or (supplied is not None and supplied != actual):
        raise PreconditionError("mapping SHA mismatch")
    if regions.infer_cluster("Воронежская область") != "cluster_2":
        raise PreconditionError("mapping semantic mismatch")


def snapshot(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    result = []
    for spec in TARGETS:
        row = conn.execute(f"SELECT * FROM {spec['table']} WHERE id=?", (spec["id"],)).fetchone()
        if row is None:
            raise PreconditionError(f"missing {spec['table']}:{spec['id']}")
        row = dict(row)
        if any(row.get(k) != v for k, v in spec["identity"].items()):
            raise PreconditionError(f"identity mismatch {spec['table']}:{spec['id']}")
        result.append({"table": spec["table"], "id": spec["id"],
                       "identity": spec["identity"], "cluster": row["cluster"],
                       "row_sha256": digest(row)})
    return result


def check_dependencies(conn: sqlite3.Connection) -> None:
    checks = {
        "campaigns": "SELECT COUNT(*) FROM campaigns WHERE lot_id=1",
        "quotes": "SELECT COUNT(*) FROM quotes WHERE lot_id=1 OR supplier_id=2",
        "outbox": "SELECT COUNT(*) FROM outbox_messages WHERE supplier_id=2",
        "purchase_history": "SELECT COUNT(*) FROM purchase_history WHERE supplier_id=2",
        "other_lots": "SELECT COUNT(*) FROM lots WHERE project_id=1 AND id<>1",
        "duplicate_tax_id": "SELECT COUNT(*) FROM suppliers WHERE tax_id='990831123457' AND id<>2",
    }
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "price_history_entries" in tables:
        checks["price_history_entries"] = "SELECT COUNT(*) FROM price_history_entries WHERE supplier_id=2"
    for label, sql in checks.items():
        if conn.execute(sql).fetchone()[0]:
            raise PreconditionError(f"dependent records present: {label}")


def read_journal(conn: sqlite3.Connection) -> dict[str, Any] | None:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (JOURNAL,)).fetchone()
    if not exists:
        return None
    row = conn.execute(f"SELECT * FROM {JOURNAL} WHERE migration_id=?", (MIGRATION_ID,)).fetchone()
    if row is None:
        return None
    row = dict(row)
    if row["mapping_sha256"] != MAPPING_SHA256 or row["plan_sha256"] != PLAN_SHA256:
        raise PreconditionError("journal version mismatch")
    for field, direction in (("before_json", "before"), ("after_json", "after")):
        try:
            records = json.loads(row[field])
            if len(records) != 3:
                raise ValueError()
            for record, spec in zip(records, TARGETS):
                if (record["table"] != spec["table"] or record["id"] != spec["id"]
                    or record["identity"] != spec["identity"] or record["cluster"] != spec[direction]
                    or len(record["row_sha256"]) != 64):
                    raise ValueError()
        except (ValueError, TypeError, KeyError):
            raise PreconditionError("invalid journal payload") from None
    return row


def update_targets(conn: sqlite3.Connection, direction: str) -> int:
    previous = "before" if direction == "after" else "after"
    changed = 0
    for spec in TARGETS:
        conditions = " AND ".join(f"{k}=?" for k in spec["identity"])
        params = [spec[direction], spec["id"], spec[previous], *spec["identity"].values()]
        cursor = conn.execute(
            f"UPDATE {spec['table']} SET cluster=? WHERE id=? AND cluster=? AND {conditions}", params)
        if cursor.rowcount != 1:
            raise PreconditionError("guarded update count mismatch")
        changed += cursor.rowcount
    if changed != 3:
        raise PreconditionError("business update count must be exactly three")
    return changed


def plan(conn: sqlite3.Connection, mode: str = "dry-run") -> dict[str, Any]:
    """SELECT-only planner, shared by a private RAM reader and guarded writer."""
    before = snapshot(conn)
    check_dependencies(conn)
    journal = read_journal(conn)
    if journal:
        status = journal["status"]
        if status not in {"applied", "rolled_back"}:
            raise PreconditionError("invalid journal status")
        expected = json.loads(journal["after_json" if status == "applied" else "before_json"])
        if before != expected:
            raise PreconditionError("post-migration row drift; manual review required")
        if mode == "apply" and status == "rolled_back":
            raise PreconditionError("rolled-back migration requires a new approved version")
        changes = 3 if mode == "rollback" and status == "applied" else 0
    else:
        if mode == "rollback":
            raise PreconditionError("rollback requires an applied journal")
        if any(r["cluster"] != spec["before"] for r, spec in zip(before, TARGETS)):
            raise PreconditionError("old cluster mismatch; unjournaled/partial changes rejected")
        status, changes = "ready", 3
    return {"migration_id": MIGRATION_ID, "mode": mode, "mapping_sha256": MAPPING_SHA256,
            "plan_sha256": PLAN_SHA256, "status": status,
            "business_changes": changes, "rows": before}


def migrate(db: str | Path, *, mode: str = "dry-run", mapping_sha: str | None = None,
            expected_count: int | None = None, actor: str | None = None) -> dict[str, Any]:
    if mode not in {"dry-run", "apply", "rollback"}:
        raise PreconditionError("unsupported mode")
    check_mapping(mapping_sha)
    if mode != "dry-run" and (mapping_sha != MAPPING_SHA256 or expected_count != 3
                              or not actor or not actor.strip() or len(actor) > 120):
        raise PreconditionError("writes require --mapping-sha, --expected-count 3 and --actor")
    path = Path(db)
    if path.is_symlink() or not path.is_file():
        raise PreconditionError("database must be an existing regular non-symlink file")
    if mode == "dry-run":
        try:
            with readonly_snapshot(path) as (reader, evidence):
                result = plan(reader)
                result["reader"] = evidence
                return result
        except SnapshotBlocked as exc:
            raise PreconditionError(str(exc)) from exc
    uri = path.resolve().as_uri() + "?mode=rw"
    conn = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        result = plan(conn, mode)
        before = result["rows"]
        changes = result["business_changes"]
        journal = read_journal(conn)
        if changes == 0:
            conn.rollback()
            return result
        now = datetime.now(timezone.utc).isoformat()
        if mode == "apply":
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {JOURNAL} (
                migration_id TEXT PRIMARY KEY, mapping_sha256 TEXT NOT NULL,
                plan_sha256 TEXT NOT NULL, status TEXT NOT NULL,
                before_json TEXT NOT NULL, after_json TEXT NOT NULL,
                actor TEXT NOT NULL, applied_at TEXT NOT NULL,
                rollback_actor TEXT, rolled_back_at TEXT)""")
            update_targets(conn, "after")
            after = snapshot(conn)
            conn.execute(f"INSERT INTO {JOURNAL} (migration_id,mapping_sha256,plan_sha256,status,before_json,after_json,actor,applied_at) VALUES (?,?,?,?,?,?,?,?)",
                         (MIGRATION_ID, MAPPING_SHA256, PLAN_SHA256, "applied", canonical(before),
                          canonical(after), actor, now))
        else:
            update_targets(conn, "before")
            after = snapshot(conn)
            if after != json.loads(journal["before_json"]):
                raise PreconditionError("rollback did not restore exact target rows")
            conn.execute(f"UPDATE {JOURNAL} SET status='rolled_back',rollback_actor=?,rolled_back_at=? WHERE migration_id=?",
                         (actor, now, MIGRATION_ID))
        conn.execute("INSERT INTO audit_log(actor,action,entity_type,entity_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                     (actor, "regional_mapping_" + mode, "migration", MIGRATION_ID,
                      canonical({"mapping_sha256": MAPPING_SHA256, "business_changes": 3,
                                 "before": before, "after": after}), now))
        conn.commit()
        result.update(status="applied" if mode == "apply" else "rolled_back", rows=after)
        return result
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", dest="mode", action="store_const", const="dry-run")
    modes.add_argument("--apply", dest="mode", action="store_const", const="apply")
    modes.add_argument("--rollback", dest="mode", action="store_const", const="rollback")
    parser.set_defaults(mode="dry-run")
    parser.add_argument("--mapping-sha")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--actor")
    args = parser.parse_args(argv)
    try:
        result = migrate(args.db, mode=args.mode, mapping_sha=args.mapping_sha,
                         expected_count=args.expected_count, actor=args.actor)
    except (PreconditionError, sqlite3.Error, OSError) as exc:
        # Do not dump SQL parameters, complete rows, paths or environment values.
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__,
                          "sqlite_errorcode": getattr(exc, "sqlite_errorcode", None),
                          "sqlite_errorname": getattr(exc, "sqlite_errorname", None),
                          "reason": str(exc) if isinstance(exc, PreconditionError) else "database operation failed"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
