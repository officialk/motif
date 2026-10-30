"""PostgreSQL backend. Runs only when MOTIF_TEST_DATABASE_URL points at a throwaway database, e.g.

    docker run -d --rm --name motif-pg-test -e POSTGRES_PASSWORD=motiftest -e POSTGRES_DB=motif \
        -p 127.0.0.1:55432:5432 postgres:16-alpine
    MOTIF_TEST_DATABASE_URL=postgresql://postgres:motiftest@127.0.0.1:55432/motif pytest tests/test_postgres.py

Every test starts from empty tables (they are dropped), so never point this at real data.
"""

import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import credentials
from app.db import TABLES, DatabaseError, PostgresDatabase, SQLiteDatabase, from_url
from app.engine.fluidsynth_engine import FluidSynthEngine
from app.library import Library
from app.main import create_app
from app.player import Player
from tests.conftest import short_spec_maker, wait_ready

URL = os.environ.get("MOTIF_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set MOTIF_TEST_DATABASE_URL to run PostgreSQL tests")


def _drop_all(db: PostgresDatabase) -> None:
    with db.connect() as c:
        for t in reversed(TABLES):
            c.execute(f"DROP TABLE IF EXISTS {t} CASCADE")


@pytest.fixture
def pg():
    db = PostgresDatabase(URL)
    _drop_all(db)
    db.init_schema()
    yield db
    _drop_all(db)
    db.close()


@pytest.fixture
def pg_client(pg, tmp_path, tmp_cache):
    lib = Library(tmp_path / "lib", database=pg)
    player = Player(FluidSynthEngine(cache_dir=tmp_cache), short_spec_maker,
                    on_history=lambda t: lib.record_history(t.id, t.spec, t.source), library=lib)
    with TestClient(create_app(player, lib)) as c:
        c.player, c.library = player, lib
        yield c


def test_library_roundtrip(pg, tmp_path, spec):
    lib = Library(tmp_path / "lib", database=pg)
    entry, already = lib.like(spec, "procedural")
    assert not already and lib.entries()[0]["id"] == entry["id"]
    assert isinstance(lib.entries()[0]["liked_at"], float)  # DOUBLE PRECISION, not float4
    assert lib.spec(entry["id"]) == spec
    pl = lib.create_playlist("Night set")
    pl = lib.add_to_playlist(pl["id"], entry["id"])
    pl = lib.add_to_playlist(pl["id"], entry["id"])  # ON CONFLICT DO NOTHING
    assert pl["song_ids"] == [entry["id"]] and pl["count"] == 1
    lib.unlike(entry["id"])
    assert lib.entries() == [] and lib.playlist(pl["id"])["count"] == 1  # kept for the playlist
    lib.remove_from_playlist(pl["id"], entry["id"])
    assert lib.spec(entry["id"]) is None  # orphan dropped
    lib.set_setting("ai", {"backend": "openai"})
    assert lib.get_setting("ai") == {"backend": "openai"}
    for i in range(3):
        lib.record_history(f"t{i}", spec, "procedural")
    assert [h["id"] for h in lib.history()] == ["t2", "t1", "t0"]
    assert lib.history_spec("t1")[0] == spec


def test_encrypted_secrets_in_postgres(pg, tmp_path, monkeypatch):
    monkeypatch.delenv("MOTIF_SECRET_KEY", raising=False)
    store = credentials.Credentials(pg, key_file=tmp_path / "secret.key")
    store.update({"openai_api_key": "sk-pg"}, "u1")
    assert store.get("openai_api_key", "u1") == "sk-pg" and store.get("openai_api_key", "u2") is None
    with pg.connect() as c:
        blob = bytes(c.execute("SELECT value FROM secrets").fetchone()[0])
    assert b"sk-pg" not in blob


def test_backup_and_restore_across_backends(pg, tmp_path, spec):
    lib = Library(tmp_path / "lib", database=pg)
    lib.like(spec, "procedural")
    lib.record_history("t0", spec, "procedural")
    snap = tmp_path / "snap.db"
    pg.snapshot_to(snap)
    with sqlite3.connect(snap) as c:
        assert c.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == 1

    # Postgres backup -> SQLite
    lite = SQLiteDatabase(tmp_path / "lite.db")
    lite.init_schema()
    lite.restore_from(snap)
    assert Library(tmp_path / "x", database=lite).entries()[0]["id"] == spec.audio_hash()

    # SQLite backup -> Postgres (replaces everything), and the history id keeps counting
    _drop_all(pg)
    pg.init_schema()
    pg.restore_from(snap)
    lib = Library(tmp_path / "lib", database=pg)
    assert len(lib.entries()) == 1
    lib.record_history("t1", spec, "procedural")
    assert [h["id"] for h in lib.history()] == ["t1", "t0"]


def test_local_sqlite_imported_once(pg, tmp_path, spec):
    local = tmp_path / "lib" / "motif.db"
    Library(tmp_path / "lib").like(spec, "claude")  # an existing SQLite library
    lib = Library(tmp_path / "lib", db_path=local, database=pg)
    assert [(e["id"], e["source"]) for e in lib.entries()] == [(spec.audio_hash(), "claude")]
    assert lib.get_setting("imported_from_sqlite")["path"] == str(local)
    lib.unlike(spec.audio_hash())
    lib.record_history("t", spec, "procedural")  # Postgres now has data: no second import
    assert Library(tmp_path / "lib", db_path=local, database=pg).entries() == []


def test_api_on_postgres(pg_client):
    c = pg_client
    s = c.post("/prompt", json={"text": "smoky jazz", "count": 3}).json()
    assert s["playlist"]["count"] == 3
    wait_ready(c, s["now"]["id"])
    assert c.post("/like", json={"id": s["now"]["id"]}).json()["already"] is False
    assert len(c.get("/library").json()["library"]) == 1
    assert c.app.state.library.db.dialect == "postgres"
    assert "motiftest" not in c.get("/settings").text  # no password anywhere


def test_url_handling():
    db = PostgresDatabase(URL)
    assert "motiftest" not in str(db.describe()) and "@" not in db.describe()["target"]
    with pytest.raises(DatabaseError) as e:
        from_url("postgresql://u:hunter2@127.0.0.1:1/nope", None)
    assert "hunter2" not in str(e.value)
    with pytest.raises(DatabaseError):
        PostgresDatabase("mysql://x")
