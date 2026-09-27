"""Integration test for scripts/backup.sh — no Docker required.

Runs the real script against a fixture project dir with a fake `docker`
executable on PATH that emulates `docker compose exec -T backend python3 -c`
by copying the fixture DB to the snapshot path the script asks for.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

FAKE_DOCKER = """#!/usr/bin/env python3
import os, re, shutil, sys
fixture = os.environ["FAKE_DOCKER_FIXTURE"]
code = sys.argv[-1]
m = re.search(r"connect\\('/data/backups/(app-[^']+\\.db)'\\)", code)
assert m, "fake docker: could not find snapshot path in: " + code[:200]
shutil.copy(os.path.join(fixture, "data", "app.db"),
            os.path.join(fixture, "data", "backups", m.group(1)))
print("snapshot ok")
"""


@pytest.fixture()
def fixture_proj(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / "data" / "backups").mkdir(parents=True)
    (proj / "media" / "raw").mkdir(parents=True)
    (proj / "media" / "sessions").mkdir(parents=True)
    (proj / "scripts").mkdir()

    db = proj / "data" / "app.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO t (v) VALUES ('hello')")
    con.commit()
    con.close()

    (proj / "media" / "raw" / "a.mp4").write_bytes(b"fake-video")
    (proj / "media" / "sessions" / "x.json").write_text("{}")
    shutil.copy(os.path.join(REPO, "scripts", "backup.sh"), proj / "scripts" / "backup.sh")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)

    monkeypatch.setenv("FAKE_DOCKER_FIXTURE", str(proj))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    return proj


def test_backup_archives_db_snapshot_and_media(fixture_proj, tmp_path):
    r = subprocess.run(
        ["bash", "scripts/backup.sh"], cwd=fixture_proj,
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr

    tarballs = list((fixture_proj / "data" / "backups").glob("instareel-*.tar.gz"))
    assert len(tarballs) == 1
    members = tarfile.open(tarballs[0]).getnames()

    db_members = [m for m in members if re.fullmatch(r"app-\d{8}-\d{6}\.db", m)]
    assert len(db_members) == 1, members
    assert "media/raw/a.mp4" in members
    assert "media/sessions/x.json" in members
    # The backup dir must not be archived inside itself.
    assert not any(m.startswith("data/backups") or "instareel-" in m for m in members)

    # The snapshot is a usable SQLite DB with the fixture row.
    extract = tmp_path / "restore"
    extract.mkdir()
    tarfile.open(tarballs[0]).extractall(extract)
    con = sqlite3.connect(extract / db_members[0])
    assert con.execute("SELECT v FROM t").fetchone()[0] == "hello"
    con.close()
    # The transient snapshot file is cleaned up, only the tarball remains.
    assert list((fixture_proj / "data" / "backups").glob("app-*.db")) == []


def test_backup_retention(fixture_proj):
    import time

    for _ in range(3):
        r = subprocess.run(
            ["bash", "scripts/backup.sh"], cwd=fixture_proj,
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "BACKUP_KEEP": "2"},
        )
        assert r.returncode == 0, r.stderr
        time.sleep(1.1)  # STAMP has 1s resolution — force distinct tarballs
    kept = list((fixture_proj / "data" / "backups").glob("instareel-*.tar.gz"))
    assert len(kept) == 2
