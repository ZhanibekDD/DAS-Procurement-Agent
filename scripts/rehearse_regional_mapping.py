"""Explicitly authorized rehearsal only. Never restores or updates the source DB.

Run on the Docker host with permission to read the production volume. All
mutation targets are a NEW release directory and two no-network containers.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from procurement.migrations.regional_mapping_v1 import MAPPING_SHA256, migrate
from procurement.passwords import hash_password

OLD_IMAGE = "sha256:9c2ff106a6fa1d174b1ea286bc3f354bb397096c5f2de91d1d0ef3ac1f2602b9"
SOURCE_DB = Path("/var/lib/docker/volumes/procurement-data/_data/procurement.db")


def command(args):
    return subprocess.run(args, check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE).stdout.strip()


def inspect(name):
    return json.loads(command(["docker", "inspect", name]))[0]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def business(path, container=None):
    if container is not None:
        # Read live WAL state with the application's UID/SQLite runtime. Never
        # leave host-side connections open across a container lifecycle change.
        code = """import os,sqlite3,json
from contextlib import closing
with closing(sqlite3.connect('file:'+os.environ['PROCUREMENT_DB_PATH']+'?mode=ro',uri=True)) as c:
 c.row_factory=sqlite3.Row
 print(json.dumps({t:[dict(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY id')] for t in ('suppliers','projects','lots')}))
"""
        return json.loads(command(["docker", "exec", container, "python3", "-B", "-c", code]))
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        return {t: [dict(r) for r in c.execute(f"SELECT * FROM {t} ORDER BY id")]
                for t in ("suppliers", "projects", "lots")}


def check_backup(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "backup integrity"
        assert not c.execute("PRAGMA foreign_key_check").fetchall(), "backup foreign keys"


def start(name, image, data, auth, work, retain_env=False):
    fd, envfile = tempfile.mkstemp(prefix=".rehearsal-env-", dir=work)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(k + "=" + v for k, v in auth.items()) + "\n")
        command(["docker", "create", "--name", name, "--network", "none", "--restart", "no",
                 "--label", "purpose=regional-mapping-rehearsal", "--env-file", envfile,
                 "--mount", f"type=bind,source={data},target=/data", image])
    finally:
        if not retain_env:
            os.unlink(envfile)
    command(["docker", "start", name])
    for _ in range(60):
        p = subprocess.run(["docker", "exec", name, "python3", "-B", "-c",
                            "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:9200/health',timeout=2).status)"],
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode == 0 and p.stdout.strip() == "200":
            break
        time.sleep(1)
    else:
        raise RuntimeError("rehearsal container health timeout")
    i = inspect(name)
    assert i["HostConfig"]["NetworkMode"] == "none"
    assert not i["HostConfig"]["PortBindings"]
    assert len(i["Mounts"]) == 1 and i["Mounts"][0]["Source"] == str(data)


def log_counts(name):
    p = subprocess.run(["docker", "logs", name], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    text = p.stdout + p.stderr
    return {"5xx": len(re.findall(r'" 5\d\d\b', text)),
            "traceback": len(re.findall(r"Traceback", text)),
            "oom": int(inspect(name)["State"]["OOMKilled"])}


def rehearse(work, new_image, revision, existing_backup=None):
    work = Path(work)
    if work.parent.resolve() != Path("/home/dnepr/releases").resolve() or work.exists():
        raise ValueError("rehearsal requires a NEW direct child of /home/dnepr/releases")
    production = inspect("procurement")
    assert production["Image"] == OLD_IMAGE, "production image drift"
    assert any(m.get("Name") == "procurement-data" and m["Destination"] == "/data" for m in production["Mounts"])
    candidate = inspect(new_image)
    assert candidate["Config"]["Labels"]["org.opencontainers.image.revision"] == revision
    names = ["procurement-rm1-new-" + revision[:8], "procurement-rm1-rollback-" + revision[:8]]
    for name in names:
        p = subprocess.run(["docker", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert p.returncode != 0, "rehearsal container name already exists"
    work.mkdir(mode=0o700)
    evidence = {"source_commit": revision, "old_image": OLD_IMAGE,
                "new_image": candidate["Id"], "mapping_sha256": MAPPING_SHA256,
                "source_db_open_mode": "not opened" if existing_backup else "ro", "published_ports": [], "network": "none"}
    running = []
    try:
        backup = work / "production.online-backup.db"
        if existing_backup is not None:
            existing = Path(existing_backup).resolve()
            expected = Path('/home/dnepr/releases/procurement-rm1-rehearsal-c5c5dfcbd091/production.online-backup.db')
            expected_sha = 'bc3f36230f595f1cda5488b95a7daea6c23d9c86dbedc893f23b9a471e067543'
            assert existing == expected and sha(existing) == expected_sha, 'archived backup mismatch'
            shutil.copyfile(existing, backup)
            evidence.update(source_db_opened=False, fresh_backup_created=False,
                            source_existing_backup=str(existing), source_existing_backup_sha256=expected_sha)
        else:
            prod_before = business(SOURCE_DB, container="procurement")
            with closing(sqlite3.connect(SOURCE_DB.as_uri() + "?mode=ro", uri=True)) as src:
                with closing(sqlite3.connect(backup)) as dst:
                    src.backup(dst, pages=256, sleep=0.01)
        backup.chmod(0o600)
        check_backup(backup)
        evidence.update(backup_api="existing verified archive" if existing_backup else "sqlite3.Connection.backup", backup_sha256=sha(backup),
                        backup_integrity="ok", backup_foreign_key_errors=0)
        data = work / "data"
        data.mkdir(mode=0o700)
        db = data / "procurement.db"
        shutil.copyfile(backup, db)
        evidence["restore_sha256"] = sha(db)
        assert sha(db) == sha(backup), "restore SHA mismatch"
        os.chown(data, 10001, 10001)
        os.chown(db, 10001, 10001)
        db.chmod(0o600)
        baseline = business(db)
        approval = dict(mapping_sha=MAPPING_SHA256, expected_count=3, actor="isolated-rehearsal")
        dry = migrate(db)
        assert dry["business_changes"] == 3 and sha(db) == sha(backup)
        applied = migrate(db, mode="apply", **approval)
        assert applied["business_changes"] == 3
        repeated = migrate(db, mode="apply", **approval)
        assert repeated["business_changes"] == 0
        evidence.update(dry_run_changes=3, apply_business_changes=3, repeat_business_changes=0)
        auth = {"PROCUREMENT_ENV": "production", "PROCUREMENT_DB_PATH": "/data/procurement.db",
                "PROCUREMENT_API_KEY": secrets.token_urlsafe(32),
                "PROCUREMENT_AUTH_SECRET": secrets.token_urlsafe(48),
                "PROCUREMENT_ADMIN_USERNAME": "rehearsal-admin",
                "PROCUREMENT_ADMIN_PASSWORD_HASH": hash_password(secrets.token_urlsafe(32)),
                "PROCUREMENT_OUTBOX_MODE": "draft_only"}
        # Stop even a partially started rehearsal container on error; never production.
        running.append(names[0])
        start(names[0], new_image, data, auth, work, retain_env=existing_backup is not None)
        api_code = """import os,json,urllib.request
url='http://127.0.0.1:9200/api/lots/1/supplier-matches'
r=urllib.request.urlopen(urllib.request.Request(url,headers={'X-API-Key':os.environ['PROCUREMENT_API_KEY']}),timeout=5)
rows=json.load(r); ids=[x['id'] for x in rows]
assert 2 in ids and 1 not in ids
print(json.dumps({'http':r.status,'matched_supplier_ids':ids}))
"""
        evidence["new_image_matches"] = json.loads(command(["docker", "exec", names[0], "python3", "-B", "-c", api_code]))
        state = business(db, container=names[0])
        assert state["suppliers"][0] == baseline["suppliers"][0], "excluded supplier changed"
        assert state["suppliers"][1]["cluster"] == state["projects"][0]["cluster"] == state["lots"][0]["cluster"] == "cluster_2"
        evidence["new_image_health"] = 200
        evidence["new_image_logs"] = log_counts(names[0])
        assert not any(evidence["new_image_logs"].values()), "new image log error"
        command(["docker", "stop", "--time", "20", names[0]])
        running.remove(names[0])
        rolled = migrate(db, mode="rollback", **approval)
        assert rolled["business_changes"] == 3
        assert migrate(db, mode="rollback", **approval)["business_changes"] == 0
        assert business(db) == baseline, "logical rollback mismatch"
        evidence.update(rollback_business_changes=3, rollback_repeat_changes=0, exact_business_rows_restored=True)
        running.append(names[1])
        start(names[1], OLD_IMAGE, data, auth, work, retain_env=existing_backup is not None)
        assert inspect(names[1])["Image"] == OLD_IMAGE
        assert business(db, container=names[1]) == baseline
        evidence["rollback_image_health"] = 200
        evidence["rollback_image_logs"] = log_counts(names[1])
        assert not any(evidence["rollback_image_logs"].values()), "old image log error"
        command(["docker", "stop", "--time", "20", names[1]])
        running.remove(names[1])
        restored = work / "full-restore.db"
        shutil.copyfile(backup, restored)
        restored.chmod(0o600)
        check_backup(restored)
        assert sha(restored) == sha(backup)
        evidence["full_restore_sha256"] = sha(restored)
        evidence["excluded_supplier_unchanged"] = True
        end = inspect("procurement")
        evidence["production_identity_unchanged"] = all(production[k] == end[k] for k in ("Id", "Image", "RestartCount")) and production["State"]["StartedAt"] == end["State"]["StartedAt"]
        if existing_backup is None:
            evidence["production_business_rows_unchanged"] = business(SOURCE_DB, container="procurement") == prod_before
            assert evidence["production_business_rows_unchanged"]
        else:
            assert sha(existing) == expected_sha, 'archived backup changed'
        assert evidence["production_identity_unchanged"]
        evidence["status"] = "PASS"
    finally:
        evidence.setdefault("status", "FAIL")
        for name in running:
            subprocess.run(["docker", "stop", "--time", "20", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        dest = work / "evidence.json"
        with dest.open("x", encoding="utf-8") as f:
            json.dump(evidence, f, ensure_ascii=False, indent=2)
            f.write("\n")
        dest.chmod(0o600)
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workdir", required=True)
    p.add_argument("--new-image", required=True)
    p.add_argument("--source-commit", required=True)
    p.add_argument("--existing-backup", help="Use the pinned, previously verified archive; do not open production DB")
    args = p.parse_args()
    rehearse(args.workdir, args.new_image, args.source_commit, args.existing_backup)
