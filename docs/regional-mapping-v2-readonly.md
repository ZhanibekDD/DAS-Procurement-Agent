# Regional mapping v2: physically read-only planner

The mapping, three-row migration ID, journal schema, approval flags, transaction
and rollback guards are unchanged. V2 is an immutable image, not an in-place edit.

## RCA

In v1 `file:///data/procurement.db?mode=ro` plus a read-only Docker volume does
not make SQLite's WAL setup work. The production main header is WAL (bytes 18/19
are 2), with no `-wal` or `-shm`. At the first SELECT, SQLite 3.46.1 attempts
`openat(...procurement.db-wal, O_RDWR|O_CREAT, 0644)` and receives EROFS, then
O_RDONLY returns ENOENT. The result is SQLITE_CANTOPEN (14). No CREATE/UPDATE,
audit insert or BEGIN IMMEDIATE is executed by the old dry-run.

## New reader contract and explicit limits

* Open the source only O_RDONLY|O_CLOEXEC|O_NOFOLLOW. No SQLite source connection.
* Acquire a Linux read lease only for the bounded RAM read (maximum 16 MiB).
  An existing writable descriptor blocks the plan. A new writer breaks the lease,
  which is immediately released; the plan aborts. No automatic retry.
* Require absent or empty WAL/SHM/journal. Nonempty sidecars fail closed: this
  implementation does NOT claim to handle active or crash-recovery WAL snapshots.
* Compare file identity/size/mtime/ctime and lease state before returning bytes.
* Deserialize private RAM, adapting header bytes 18/19 to 1 as documented by
  SQLite. Do not modify any source bytes or source PRAGMA.
* A SELECT-only authorizer denies DDL/DML, transactions, ATTACH and mutating
  PRAGMAs on the planning connection. No disk snapshot, Online Backup API,
  checkpoint or source sidecar creation is used.
* Linux main thread, file ownership/CAP_LEASE and filesystem lease support are
  required. Unsupported/busy situations produce BLOCKED rather than unsafe
  `immutable=1` on a live file or a writable-mount fallback.
* The RAM plan is a point-in-time report, never authority to apply later without
  rerunning all guards inside the original BEGIN IMMEDIATE apply transaction.

`query_only` is not changed: it remains 0. Enforced read-only access is provided
by the source O_RDONLY descriptor, the read-only container mount and the RAM
connection's SQL authorizer, not by a PRAGMA toggle.

References: https://sqlite.org/wal.html#read_only_databases,
https://sqlite.org/c3ref/deserialize.html,
https://man7.org/linux/man-pages/man2/F_SETLEASE.2const.html.

## Rehearsal without touching production

Use `scripts/rehearse_regional_mapping.py --existing-backup` with the pinned
archive from the prior v1 rehearsal. This mode verifies its known SHA, never
opens the production DB and never creates a fresh production backup. All apply,
idempotency and rollback checks operate on a NEW restored-copy directory with
no-network containers and no published ports. Preserve diagnostic evidence.

On production, run only the v2 CLI with --dry-run, a read-only root filesystem,
network none and procurement-data:/data:ro. Retain the exited diagnostic helper;
do not use --rm, restart, cleanup or any apply/rollback flag. Require exactly 3.

The previous image and production workload remain unchanged. A later rollout
needs explicit approval for the new v2 digest and a fresh validated backup.
