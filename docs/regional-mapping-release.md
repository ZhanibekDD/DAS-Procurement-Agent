# Regional mapping v1: operator runbook

This is NOT an automatic startup migration. Do not change DMS 9599, PR8 9201,
DMS v11, or procurement canary 9206. Production rollout needs separate approval.

## Scope and prerequisites

- Supplier 2 / tax ID 990831123457 / Воронежская область: cluster_1 -> cluster_2.
- Project 1 / Воронежская область / draft: empty -> cluster_2.
- Lot 1 / project 1 / Воронежская область / draft: empty -> cluster_2.
- Supplier 1 / Кызылординская область is excluded and must remain unchanged.
- Refuse campaigns, quotes, outbox, purchase/price history for these targets,
  additional lots under project 1, identity drift, partial or unjournaled edits.
- Mapping SHA256: 6359d5adc6c5255b4721c378b6849b471881b92bb55f1beb6f69c6147f48aeda.
- Old production image: sha256:9c2ff106a6fa1d174b1ea286bc3f354bb397096c5f2de91d1d0ef3ac1f2602b9.

## Default dry-run (SELECT only)

Run in the approved migration image with the chosen DB at `/data/procurement.db`:

```sh
python3 -B -m procurement.migrations.regional_mapping_v1 --db /data/procurement.db
```

`--apply` and `--rollback` BOTH require these explicit arguments:

```sh
--mapping-sha 6359d5adc6c5255b4721c378b6849b471881b92bb55f1beb6f69c6147f48aeda \
--expected-count 3 --actor APPROVED_OPERATOR
```

Exactly 3 means business rows; the same transaction also creates/updates a
versioned journal and appends one audit event. Repeated apply = 0, repeated
rollback = 0. Apply after rollback requires a new approved version. Hashes of
entire target rows protect rollback from overwriting later user edits.

## Backup and rehearsal

Use SQLite `Connection.backup` with the SOURCE opened `mode=ro`, not a naked copy
of a live WAL main file. Backup into a new restricted directory, verify SHA256,
integrity and foreign keys, then restore that closed backup into a separate path.
Preserve an encrypted off-host copy before a real rollout. Backup contains private
application data: mode 0600 and parent 0700. Never put the DB or credentials in Git.

`scripts/rehearse_regional_mapping.py` performs this workflow only in a NEW child
of `/home/dnepr/releases`; it never writes/restores the production source. It starts
new and rollback images with `--network none`, no published ports, fresh temporary
credentials, and only a copied data directory. Both rehearsal containers are
stopped at completion. Backup and evidence are retained, not cleaned up.

Build `Dockerfile.regional-release` only after confirming its local base tag is
sha256:75f8bbea38e48c140e80e0e1d030ddad809c41b791393164973c5e69adf6396c.
Use a unique `procurement:regional-mapping-v1-<commit>` tag; refuse overwriting it.
The OCI revision must equal the source commit. Existing v11 images stay untouched.

## Proposed production window (NOT authorized)

Reserve 15 minutes after explicit approval. T+0: fence writes/outbox for procurement
only and record current image/config identity. T+0..3: fresh online backup and
hash/integrity validation. T+3..5: rerun dry-run under the fence; require exactly
3 and an unchanged target report. T+5..8: guarded apply and start the approved
immutable image with the SAME persistent volume, while traffic remains fenced.
T+8..10: health, target values, supplier-match and error checks. T+10: reopen only
if all gates pass; T+10..15 is rollback reserve. These are budgets, not measured
production durations. No calendar time is scheduled without operator approval.

## Rollback commands and limits (NOT executed in production)

Before reopening writes, stop only the NEW procurement workload and invoke the
NEW migration image against the SAME DB with the normal command plus `--rollback`
and all explicit approval arguments above. Require 3 reversed business rows and
the recorded before-row hashes. Then recreate only the procurement service from
its recorded old image/config; do not restart DMS or PR8. Recheck old values and
health before reopening. See release evidence for the actually rehearsed image ID.

If SQL transaction fails before commit, SQLite rolls it back automatically.
If journal rollback cannot pass guards, keep writes fenced; restore the verified
pre-cutover backup to a NEW data directory/volume and start the old image against
that restored copy. Do not overwrite the original volume in place. Full-copy
restore was checked with a byte-identical SHA during rehearsal.

After writes have reopened, do NOT restore the whole backup or blindly roll back
the image. New rows may use the new mapping. Fence again, inventory post-cutover
writes, then approve a new compensating plan. No global cluster label swap.
