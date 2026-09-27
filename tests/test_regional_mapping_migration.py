import concurrent.futures
from contextlib import closing
import hashlib
import json
import sqlite3

import pytest

from procurement.migrations import regional_mapping_v1 as migration
from procurement.regions import REGIONAL_CLUSTERS, infer_cluster


EXPECTED_MARKERS = {
    "cluster_2": "воронеж белгород курск липецк тамбов орел краснодар ростов ставропол адыге калмык".split(),
    "cluster_1": "янао ямало ноябрьск хмао ханты тюмен свердлов екатеринбург омск новосибирск томск кемеров алтай красноярск иркутск якут бурят владивосток хабаровск приморск сахалин".split(),
}
APPROVAL = {"mapping_sha": migration.MAPPING_SHA256, "expected_count": 3, "actor": "rehearsal"}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "production-copy.db"
    with closing(sqlite3.connect(path)) as c, c:
        c.executescript("""
        CREATE TABLE suppliers(id INTEGER PRIMARY KEY,tax_id TEXT,region TEXT,cluster TEXT,
                               active INTEGER,verified INTEGER,created_at TEXT);
        CREATE TABLE projects(id INTEGER PRIMARY KEY,region TEXT,cluster TEXT,status TEXT);
        CREATE TABLE lots(id INTEGER PRIMARY KEY,project_id INTEGER,region TEXT,cluster TEXT,status TEXT);
        CREATE TABLE campaigns(id INTEGER PRIMARY KEY,lot_id INTEGER);
        CREATE TABLE quotes(lot_id INTEGER,supplier_id INTEGER);
        CREATE TABLE outbox_messages(supplier_id INTEGER);
        CREATE TABLE purchase_history(supplier_id INTEGER);
        CREATE TABLE audit_log(actor TEXT,action TEXT,entity_type TEXT,entity_id TEXT,details_json TEXT,created_at TEXT);
        """)
        c.execute("INSERT INTO suppliers VALUES (1,?,?,?,?,?,?)", ("990831123456", "Кызылординская область", "", 1, 1, "legacy"))
        c.execute("INSERT INTO suppliers VALUES (2,?,?,?,?,?,?)", ("990831123457", "Воронежская область", "cluster_1", 1, 1, "2026-08-31T17:45:50+00:00"))
        c.execute("INSERT INTO projects VALUES (1,?,?,?)", ("Воронежская область", "", "draft"))
        c.execute("INSERT INTO lots VALUES (1,1,?,?,?)", ("Воронежская область", "", "draft"))
    return path


def rows(db):
    with closing(sqlite3.connect(db)) as c, c:
        return {t: c.execute(f"SELECT * FROM {t} ORDER BY id").fetchall()
                for t in ("suppliers", "projects", "lots")}


def edit(db, sql, params=()):
    with closing(sqlite3.connect(db)) as c, c:
        c.execute(sql, params)


@pytest.mark.parametrize("cluster,marker", [(c,m) for c,ms in EXPECTED_MARKERS.items() for m in ms])
def test_all_32_markers(cluster, marker):
    assert infer_cluster(marker) == cluster
    assert infer_cluster(marker.upper()) == cluster


def test_marker_coverage_and_ambiguity():
    assert sum(map(len, EXPECTED_MARKERS.values())) == 32
    assert {k: list(v) for k,v in REGIONAL_CLUSTERS.items()} == EXPECTED_MARKERS
    assert infer_cluster("Воронежская область") == "cluster_2"
    assert infer_cluster("Воронежская область / Омская область") == ""
    assert infer_cluster("Кызылординская область") == ""
    assert infer_cluster("тамбос") == ""


def test_default_dry_run_is_byte_identical_and_does_not_create_journal(db, capsys):
    before = db.read_bytes()
    assert migration.main(["--db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out)["business_changes"] == 3
    assert db.read_bytes() == before
    with closing(sqlite3.connect(db)) as c, c:
        assert not c.execute("SELECT name FROM sqlite_master WHERE name=?", (migration.JOURNAL,)).fetchall()


def test_apply_repeat_rollback_and_repeat(db):
    before = rows(db)
    assert migration.migrate(db, mode="apply", **APPROVAL)["business_changes"] == 3
    after = rows(db)
    assert after["suppliers"][0] == before["suppliers"][0]
    assert {r["cluster"] for r in migration.migrate(db)["rows"]} == {"cluster_2"}
    assert migration.migrate(db, mode="apply", **APPROVAL)["business_changes"] == 0
    with closing(sqlite3.connect(db)) as c, c:
        journal = c.execute(f"SELECT before_json,after_json FROM {migration.JOURNAL}").fetchone()
        assert len(json.loads(journal[0])) == len(json.loads(journal[1])) == 3
        assert c.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
    assert migration.migrate(db, mode="rollback", **APPROVAL)["business_changes"] == 3
    assert rows(db) == before
    assert migration.migrate(db, mode="rollback", **APPROVAL)["business_changes"] == 0
    with pytest.raises(migration.PreconditionError, match="new approved version"):
        migration.migrate(db, mode="apply", **APPROVAL)


@pytest.mark.parametrize("approval", [
    {}, {"mapping_sha": "0"*64, "expected_count": 3, "actor": "test"},
    {"mapping_sha": migration.MAPPING_SHA256, "expected_count": 2, "actor": "test"},
    {"mapping_sha": migration.MAPPING_SHA256, "expected_count": 4, "actor": "test"},
    {"mapping_sha": migration.MAPPING_SHA256, "expected_count": 3},
])
def test_write_requires_exact_approval(db, approval):
    before = db.read_bytes()
    with pytest.raises(migration.PreconditionError):
        migration.migrate(db, mode="apply", **approval)
    assert db.read_bytes() == before


@pytest.mark.parametrize("sql,params", [
    ("DELETE FROM suppliers WHERE id=2", ()),
    ("UPDATE suppliers SET tax_id='wrong' WHERE id=2", ()),
    ("UPDATE suppliers SET verified=0 WHERE id=2", ()),
    ("UPDATE suppliers SET region=? WHERE id=2", ("Кызылординская область",)),
    ("UPDATE projects SET cluster='cluster_2' WHERE id=1", ()),
    ("UPDATE lots SET project_id=999 WHERE id=1", ()),
    ("UPDATE lots SET status='sent' WHERE id=1", ()),
    ("INSERT INTO campaigns VALUES(1,1)", ()),
    ("INSERT INTO quotes VALUES(1,2)", ()),
    ("INSERT INTO outbox_messages VALUES(2)", ()),
    ("INSERT INTO purchase_history VALUES(2)", ()),
    ("INSERT INTO lots VALUES(2,1,'other','','draft')", ()),
])
def test_strict_preconditions_no_partial_write(db, sql, params):
    edit(db, sql, params)
    before = db.read_bytes()
    with pytest.raises(migration.PreconditionError):
        migration.migrate(db, mode="apply", **APPROVAL)
    assert db.read_bytes() == before


def test_all_new_without_journal_is_not_assumed_applied(db):
    for t in ("suppliers", "projects", "lots"):
        edit(db, f"UPDATE {t} SET cluster='cluster_2' WHERE id=?", (2 if t=="suppliers" else 1,))
    with pytest.raises(migration.PreconditionError, match="unjournaled"):
        migration.migrate(db, mode="apply", **APPROVAL)


def test_atomic_failure_rolls_back_first_update_and_journal(db):
    edit(db, "CREATE TRIGGER block_project BEFORE UPDATE ON projects BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    before = db.read_bytes()
    with pytest.raises(sqlite3.Error):
        migration.migrate(db, mode="apply", **APPROVAL)
    assert rows(db)["suppliers"][1][3] == "cluster_1"
    assert db.read_bytes() == before


def test_concurrent_apply_is_serialized(db):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: migration.migrate(db, mode="apply", **APPROVAL), range(2)))
    assert sorted(r["business_changes"] for r in result) == [0, 3]


def test_rollback_refuses_post_apply_drift(db):
    migration.migrate(db, mode="apply", **APPROVAL)
    edit(db, "UPDATE suppliers SET active=0 WHERE id=2")
    with pytest.raises(migration.PreconditionError):
        migration.migrate(db, mode="rollback", **APPROVAL)
    assert rows(db)["projects"][0][2] == "cluster_2"


def test_mapping_file_change_blocks_before_database_open(db, monkeypatch, tmp_path):
    fake = tmp_path / "regions.py"
    fake.write_text("changed mapping")
    monkeypatch.setattr(migration.regions, "__file__", str(fake))
    with pytest.raises(migration.PreconditionError, match="SHA"):
        migration.migrate(db, mode="apply", **APPROVAL)


def test_rollback_without_journal_rejected(db):
    with pytest.raises(migration.PreconditionError, match="journal"):
        migration.migrate(db, mode="rollback", **APPROVAL)


def test_missing_db_is_not_created(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(migration.PreconditionError):
        migration.migrate(missing)
    assert not missing.exists()
