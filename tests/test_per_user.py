"""Per-user data, AI, backups, the old-schema migration, the shared render pool and idle players."""

import hashlib
import json as json_  # `json` is a keyword argument below
import sqlite3
import threading
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app import credentials, player as player_mod, storage
from app.accounts import Accounts
from app.apikeys import ApiKeys
from app.engine import base
from app.library import Library
from app.player import Player
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, FakeEngine, short_spec_maker, wait_ready
from tests.test_live_api import auth
from tests.test_storage import FakeS3


def admin_lib(c) -> Library:
    return c.library.for_user(c.accounts.admin_id)


def play(c) -> dict:
    s = c.post("/genre", json={"genre": "chiptune"}).json()
    wait_ready(c, s["now"]["id"])
    return c.get("/state").json()["now"]


# --- isolation --------------------------------------------------------------------

def test_library_playlists_history_and_settings_are_per_user(login_client):
    m = login_client.member()
    now = play(login_client)
    entry = login_client.post("/like", json={"id": now["id"]}).json()["entry"]
    pl = login_client.post("/playlists", json={"name": "Admin's", "song_id": entry["id"]}).json()["playlist"]
    login_client.post("/skip", json={"from_id": now["id"]})
    login_client.put("/settings/app", json={"prompt_count": 3})
    assert login_client.get("/history").json()["history"]

    assert m.get("/library").json()["library"] == []
    assert m.get("/playlists").json()["playlists"] == []
    assert m.get("/history").json()["history"] == []
    assert m.get("/settings").json()["app"]["prompt_count"] == 8
    assert m.get("/settings").json()["account"]["id"] == m.user["id"]
    # another user's ids are unknown, not reachable
    assert m.get(f"/playlists/{pl['id']}").status_code == 404
    assert m.patch(f"/playlists/{pl['id']}", json={"name": "Mine now"}).status_code == 404
    assert m.delete(f"/playlists/{pl['id']}").status_code == 404
    assert m.post(f"/playlists/{pl['id']}/play", json={}).status_code == 404
    assert m.post(f"/playlists/{pl['id']}/generate", json={}).status_code == 404
    assert m.post("/like", json={"id": now["id"]}).status_code == 404
    assert m.patch(f"/library/{entry['id']}", json={"name": "x"}).status_code == 404
    assert m.post(f"/download/{entry['id']}").status_code == 404
    assert m.delete(f"/songs/{entry['id']}").status_code == 404
    assert login_client.get(f"/playlists/{pl['id']}").json()["playlist"]["name"] == "Admin's"
    assert [e["id"] for e in login_client.get("/library").json()["library"]] == [entry["id"]]


def test_same_song_liked_by_two_users_is_kept_apart(login_client, spec):
    m = login_client.member()
    login_client.library.for_user(m.user["id"]).like(spec, "procedural")
    admin_lib(login_client).like(spec, "claude")
    assert m.delete(f"/library/{spec.audio_hash()}").status_code == 200
    assert m.get("/library").json()["library"] == []
    assert [(e["id"], e["source"]) for e in login_client.get("/library").json()["library"]] == \
        [(spec.audio_hash(), "claude")]
    assert m.delete(f"/songs/{spec.audio_hash()}").status_code == 404  # gone for them only
    assert login_client.library.saved_by_anyone(spec.audio_hash())


def test_players_and_audio_are_per_user(login_client):
    m = login_client.member()
    now = play(login_client)
    assert m.get("/state").json()["now"] is None  # their own player, nothing cued
    assert m.get(f"/audio/{now['id']}.mp3").status_code == 404
    assert login_client.get(f"/audio/{now['id']}.mp3").status_code == 200
    m.post("/repeat", json={"mode": "one"})
    assert login_client.get("/state").json()["mode"]["repeat"] == "off"
    assert login_client.users.player(m.user["id"]) is not login_client.player


def test_keys_are_per_user_and_act_as_their_owner(login_client):
    m = login_client.member()
    mine = login_client.post("/settings/keys", json={"name": "admin bot", "scopes": ["read"]}).json()
    theirs = m.post("/settings/keys", json={"name": "member bot", "scopes": ["read", "control"]}).json()
    assert [k["name"] for k in login_client.get("/settings/keys").json()["keys"]] == ["admin bot"]
    assert [k["name"] for k in m.get("/settings/keys").json()["keys"]] == ["member bot"]
    assert m.delete(f"/settings/keys/{mine['key']['id']}").status_code == 404
    assert login_client.delete(f"/settings/keys/{theirs['key']['id']}").status_code == 404
    tool = TestClient(login_client.app)
    tool.post("/repeat", json={"mode": "all"}, headers=auth(theirs["token"]))
    assert m.get("/state").json()["mode"]["repeat"] == "all"  # the member's player
    assert login_client.get("/state").json()["mode"]["repeat"] == "off"
    me = tool.get("/state", headers=auth(mine["token"]))
    assert me.status_code == 200 and me.json()["mode"]["repeat"] == "off"


def test_shared_audio_cache_keeps_what_another_player_needs(login_client):
    login_client.member()
    now = play(login_client)
    engine = login_client.player.engine
    mp3 = engine.mp3_path(login_client.player.get(now["id"]).spec)
    engine.prune(set(), max_songs=0)  # e.g. a prune from another player
    assert mp3.exists()


# --- AI is each user's own ---------------------------------------------------------

class FakeOpenAI:
    def __init__(self, monkeypatch):
        self.keys = []
        monkeypatch.setattr(httpx, "post", self.post)

    def post(self, url, headers=None, json=None, timeout=None):
        self.keys.append((headers or {}).get("Authorization"))
        n = 1
        if "songs" in str(json["messages"][-1]["content"]):
            n = int(json["messages"][-1]["content"].split('"songs": ')[1].split("}")[0].split(",")[0])
        songs = [{"genre": "jazz", "name": f"Song {i}"} for i in range(n)]
        body = {"title": "AI set", "songs": songs, "genre": "jazz", "name": "Song"}
        return httpx.Response(200, json={"choices": [{"message": {"content": json_.dumps(body)}}]},
                              request=httpx.Request("POST", url))


def test_ai_settings_never_leak_across_users(login_client, monkeypatch):
    fake = FakeOpenAI(monkeypatch)
    m = login_client.member()
    uid = m.user["id"]
    login_client.put("/settings/ai", json={"backend": "openai", "openai_api_key": "sk-admin", "ratio": 0.5})
    s = m.get("/settings").json()["ai"]
    assert s["backend"] == "off" and s["keys"]["openai"] == {"set": False, "source": None}
    assert login_client.users.ai(uid)[0].backend == "off"
    assert login_client.users.keys(uid).get("openai_api_key") is None
    assert login_client.users.player(uid).claude_ratio == 0.0

    r = m.post("/prompt", json={"text": "smoky jazz", "count": 2}).json()
    assert fake.keys == [] and r["playlist"]["source"] is None  # procedural: the admin's key is never used
    login_client.post("/prompt", json={"text": "smoky jazz", "count": 2})
    assert fake.keys == ["Bearer sk-admin"]

    m.put("/settings/ai", json={"backend": "openai", "openai_api_key": "sk-member"})
    assert m.post("/prompt", json={"text": "smoky jazz", "count": 2}).json()["playlist"]["source"] == "ai"
    assert m.post("/settings/ai/test").json()["ok"] is True
    assert fake.keys[1:] == ["Bearer sk-member", "Bearer sk-member"]
    spec, source = login_client.users.make_spec(uid)("jazz", None, True)  # radio songs too
    assert source == "openai" and fake.keys[-1] == "Bearer sk-member"
    assert login_client.get("/settings").json()["ai"]["keys"]["openai"]["set"] is True
    m.put("/settings/ai", json={"openai_api_key": ""})
    assert login_client.users.keys(login_client.accounts.admin_id).get("openai_api_key") == "sk-admin"


def test_claude_cli_is_only_for_the_env_admin(login_client):
    m = login_client.member()
    assert "claude-cli" not in m.get("/settings").json()["ai"]["backends"]
    assert m.put("/settings/ai", json={"backend": "claude-cli"}).status_code == 400
    assert "claude-cli" in login_client.get("/settings").json()["ai"]["backends"]
    assert login_client.put("/settings/ai", json={"backend": "claude-cli"}).status_code == 200
    # a saved CLI setting (e.g. a restored backup) is ignored for a member
    login_client.library.for_user(m.user["id"]).set_user_setting("ai", {"backend": "claude-cli"})
    assert login_client.users.ai_settings(m.user["id"]).backend == "off"


# --- backups are each user's own ------------------------------------------------------

@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage, "s3_client", lambda settings, creds=None: fake)
    return fake


def setup_backups(c, bucket="bkt"):
    r = c.put("/settings/backup", json={"bucket": bucket, "aws_access_key_id": "AK", "aws_secret_access_key": "SK"})
    assert r.status_code == 200, r.text


def test_backup_and_restore_replace_only_that_users_rows(login_client, s3, spec):
    m = login_client.member()
    uid = m.user["id"]
    mine, theirs = admin_lib(login_client), login_client.library.for_user(uid)
    entry, _ = theirs.like(spec, "procedural")
    pl = theirs.create_playlist("Mine")
    theirs.add_to_playlist(pl["id"], entry["id"])
    theirs.record_history("t1", spec, "procedural")
    m.put("/settings/app", json={"prompt_count": 4})
    m.put("/settings/ai", json={"openai_api_key": "sk-member"})
    key = m.post("/settings/keys", json={"name": "bot", "scopes": ["read"]}).json()
    mine.like(spec.model_copy(update={"seed": 7}), "procedural")
    setup_backups(m)

    b = m.post("/backups").json()["backup"]
    snap = login_client.library.root / "snap.db"
    snap.write_bytes(s3.objects[b["key"]]["body"])
    with sqlite3.connect(snap) as db:
        assert {r[0] for t in ("songs", "playlists", "history", "api_keys", "settings", "secrets")
                for r in db.execute(f"SELECT user_id FROM {t}")} == {uid}
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0

    # changes after the backup
    theirs.delete_song(entry["id"])
    later = theirs.create_playlist("Later")
    m.put("/settings/app", json={"prompt_count": 9})
    m.delete(f"/settings/keys/{key['key']['id']}")
    m.put("/settings/backup", json={"prefix": "motif"})  # same place, settings kept by the restore
    admin_before = (mine.entries(), mine.playlists(), mine.history())

    r = m.post("/backups/restore", json={"key": b["key"]}).json()
    assert r["ok"] and r["restored"]["songs"] == 1 and r["restored"]["playlists"] == 1
    assert [e["id"] for e in m.get("/library").json()["library"]] == [entry["id"]]
    assert [p["name"] for p in m.get("/playlists").json()["playlists"]] == ["Mine"]
    assert m.get(f"/playlists/{later['id']}").status_code == 404
    assert [h["id"] for h in m.get("/history").json()["history"]] == ["t1"]
    assert m.get("/settings").json()["app"]["prompt_count"] == 4
    assert login_client.users.keys(uid).get("openai_api_key") == "sk-member"
    assert login_client.users.keys(uid).get("aws_secret_access_key") == "SK"  # S3 keys stay
    assert m.get("/settings").json()["backup"]["bucket"] == "bkt"
    assert TestClient(login_client.app).get("/state", headers=auth(key["token"])).status_code == 200
    assert (mine.entries(), mine.playlists(), mine.history()) == admin_before  # nobody else changed
    assert login_client.get("/auth/me").status_code == 200 and m.get("/auth/me").status_code == 200


def test_backups_list_only_from_the_users_own_bucket(login_client, monkeypatch):
    buckets = {}

    def client_for(settings, creds=None):
        return buckets.setdefault(settings.bucket, FakeS3())

    monkeypatch.setattr(storage, "s3_client", client_for)
    m = login_client.member()
    setup_backups(login_client, "admin-bkt")
    setup_backups(m, "member-bkt")
    login_client.post("/backups")
    assert m.get("/backups").json()["backups"] == []
    assert len(login_client.get("/backups").json()["backups"]) == 1
    assert login_client.users.backup_settings(m.user["id"]).bucket == "member-bkt"


def old_schema_db(path, spec, secrets_dir=None):
    """A library from before accounts (the schema of the previous release, every later column included)."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE songs (id TEXT PRIMARY KEY, name TEXT NOT NULL, genre TEXT NOT NULL, tempo REAL, key TEXT,
            scale TEXT, bars INTEGER, music_seconds REAL, duration REAL, source TEXT, seed BIGINT, spec TEXT NOT NULL,
            liked_at REAL NOT NULL, liked INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE playlists (id TEXT PRIMARY KEY, name TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
            source TEXT, prompt TEXT);
        CREATE TABLE playlist_songs (playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
            song_id TEXT NOT NULL REFERENCES songs(id) ON DELETE CASCADE, pos INTEGER NOT NULL,
            PRIMARY KEY (playlist_id, song_id));
        CREATE TABLE history (n INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, hash TEXT, name TEXT, genre TEXT,
            tempo REAL, key TEXT, scale TEXT, music_seconds REAL, source TEXT, played_at REAL NOT NULL, spec TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE secrets (name TEXT PRIMARY KEY, value BLOB NOT NULL);
        CREATE TABLE api_keys (id TEXT PRIMARY KEY, name TEXT NOT NULL, hash TEXT NOT NULL UNIQUE,
            scopes TEXT NOT NULL, prefix TEXT NOT NULL, created REAL NOT NULL, last_used REAL,
            kind TEXT NOT NULL DEFAULT 'api');
    """)
    h = spec.audio_hash()
    conn.execute("INSERT INTO songs VALUES (?, 'Old song', ?, ?, ?, ?, ?, ?, ?, 'claude', ?, ?, 1.0, 1)",
                 (h, spec.genre, spec.tempo, spec.key, spec.scale, spec.bars, spec.music_seconds,
                  spec.duration_seconds, spec.seed, spec.model_dump_json()))
    conn.execute("INSERT INTO playlists VALUES ('oldpl', 'Old list', 1.0, 1.0, 'ai', 'rain')")
    conn.execute("INSERT INTO playlist_songs VALUES ('oldpl', ?, 0)", (h,))
    conn.execute("INSERT INTO history (id, hash, name, genre, played_at, spec) VALUES ('t-old', ?, 'Old song', ?, 2.0, ?)",
                 (h, spec.genre, spec.model_dump_json()))
    conn.execute("""INSERT INTO settings VALUES ('app', '{"prompt_count": 5}'), ('ai', '{"backend": "openai"}'),
                    ('storage', '{"bucket": "old-bkt", "media": true}'), ('imported_from_sqlite', '{}')""")
    conn.execute("INSERT INTO api_keys VALUES ('k-old', 'Old bot', ?, 'read', 'mk_abcd', 1.0, NULL, 'api')",
                 (hashlib.sha256(b"mk_oldtoken").hexdigest(),))
    if secrets_dir is not None:  # sealed with the key file next to the database, as the old version did
        sealed = credentials.Credentials(secrets_dir / "seal.db")._seal("openai_api_key", "sk-old")
        conn.execute("INSERT INTO secrets VALUES ('openai_api_key', ?)", (sealed,))
    conn.commit()
    conn.close()


def test_sqlite_migration_from_the_old_schema_keeps_everything(tmp_path, spec):
    root = tmp_path / "lib"
    root.mkdir()
    old_schema_db(root / "motif.db", spec, secrets_dir=root)
    lib = Library(root)
    h = spec.audio_hash()
    assert [(e["id"], e["name"], e["source"]) for e in lib.entries()] == [(h, "Old song", "claude")]
    assert lib.playlist("oldpl")["song_ids"] == [h] and lib.playlist("oldpl")["prompt"] == "rain"
    assert [x["id"] for x in lib.history()] == ["t-old"] and lib.history_spec("t-old")[0] == spec
    assert lib.user_setting("app") == {"prompt_count": 5} and lib.user_setting("ai") == {"backend": "openai"}
    assert lib.user_setting("backup")["bucket"] == "old-bkt"  # old server-wide S3 settings became the user's
    assert lib.get_setting("imported_from_sqlite") == {}  # server rows stay server-wide
    assert credentials.Credentials(lib.db).get("openai_api_key", "local") == "sk-old"
    assert ApiKeys(lib.db).verify("mk_oldtoken")["user_id"] == "local"
    with sqlite3.connect(root / "motif.db") as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    Library(root)  # a second start changes nothing
    assert len(lib.entries()) == 1 and len(lib.playlists()) == 1
    # then login mode: it all becomes the admin's
    a = Accounts(lib.db, ADMIN_EMAIL, ADMIN_PASSWORD)
    mine = lib.for_user(a.admin_id)
    assert [e["id"] for e in mine.entries()] == [h] and mine.playlist("oldpl")["count"] == 1
    assert credentials.Credentials(lib.db).get("openai_api_key", a.admin_id) == "sk-old"
    assert lib.entries() == []


def test_old_whole_database_backup_restores_into_one_user(login_client, s3, spec, tmp_path):
    m = login_client.member()
    old = tmp_path / "old-backup.db"
    old_schema_db(old, spec)
    s3.objects["motif/backups/motif-20250101-000000.db"] = {"body": old.read_bytes(),
                                                            "modified": datetime.now(timezone.utc)}
    admin_lib(login_client).like(spec, "procedural")
    setup_backups(m)
    assert [b["key"] for b in m.get("/backups").json()["backups"]] == ["motif/backups/motif-20250101-000000.db"]
    r = m.post("/backups/restore", json={"key": "motif/backups/motif-20250101-000000.db"}).json()
    assert r["ok"] and r["restored"] == {"songs": 1, "playlists": 1, "history": 1, "api_keys": 1}
    assert [e["name"] for e in m.get("/library").json()["library"]] == ["Old song"]
    pls = m.get("/playlists").json()["playlists"]
    assert [(p["name"], p["count"]) for p in pls] == [("Old list", 1)]
    assert m.get("/settings").json()["app"]["prompt_count"] == 5
    assert m.get("/settings").json()["backup"]["bucket"] == "bkt"  # their own backup settings stay
    assert [e["source"] for e in login_client.get("/library").json()["library"]] == ["procedural"]  # untouched
    assert login_client.get("/playlists").json()["playlists"] == []


def test_restored_playlist_id_taken_by_another_user_gets_a_new_id(login_client, s3, spec, tmp_path):
    m = login_client.member()
    old = tmp_path / "old-backup.db"
    old_schema_db(old, spec)
    with login_client.library.db.connect() as db:  # someone already has that playlist id
        db.execute("INSERT INTO playlists (id, name, created, updated, user_id) VALUES ('oldpl', 'Taken', 1, 1, ?)",
                   (login_client.accounts.admin_id,))
    s3.objects["motif/backups/old.db"] = {"body": old.read_bytes(), "modified": datetime.now(timezone.utc)}
    setup_backups(m)
    m.post("/backups/restore", json={"key": "motif/backups/old.db"})
    pl = m.get("/playlists").json()["playlists"][0]
    assert pl["id"] != "oldpl" and pl["name"] == "Old list" and pl["count"] == 1
    assert login_client.get("/playlists/oldpl").json()["playlist"]["name"] == "Taken"


# --- render pool and idle players ---------------------------------------------------

class SlowEngine(FakeEngine):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.running = self.peak = 0
        self.guard = threading.Lock()

    def render_wav(self, spec):
        with self.guard:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(0.15)
        with self.guard:
            self.running -= 1
        return super().render_wav(spec)

    render = base.Engine.render  # the real slot handling


@pytest.mark.parametrize("workers", [1, 2])
def test_render_worker_limit(tmp_path, spec, monkeypatch, workers):
    monkeypatch.setattr(base, "encode_mp3", lambda wav, mp3: mp3.write_bytes(b"ID3") and mp3)
    monkeypatch.setattr(base.config, "RENDER_WORKERS", workers)
    engine = SlowEngine(cache_dir=tmp_path / "c")
    specs = [spec.model_copy(update={"seed": i}) for i in range(5)]
    threads = [threading.Thread(target=engine.ensure_mp3, args=(s,)) for s in specs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert engine.peak == workers and all(engine.mp3_path(s).exists() for s in specs)
    engine.ensure_mp3(specs[0])  # cached: no new render
    assert engine.running == 0


class CountingMaker:
    def __init__(self):
        self.calls = 0

    def __call__(self, genre, mood, use_ai):
        self.calls += 1
        return short_spec_maker(genre, mood, use_ai)


def settle(p):
    for _ in range(3):
        p.pool.submit(lambda: None).result(5)


def nobody_listened_for(p, seconds):
    p.idle_since = time.monotonic() - seconds


def test_idle_player_holds_back_prefetch_until_a_client_returns(tmp_path):
    maker = CountingMaker()
    p = Player(FakeEngine(cache_dir=tmp_path / "c"), maker)
    nobody_listened_for(p, player_mod.IDLE_AFTER - 5)
    assert not p.idle()
    nobody_listened_for(p, player_mod.IDLE_AFTER + 1)
    assert p.idle()
    p.play_genre("lofi")
    assert p.now_playing.ready_event.wait(5) and p.now_playing.status == "ready"  # "now" is always prepared
    settle(p)
    nxt = p.up_next
    assert maker.calls == 1 and nxt.status == "generating" and nxt.id in p._parked
    p.connect_client("tab", "site", None, True)  # a client returns: held-back work starts
    assert nxt.ready_event.wait(5) and nxt.status == "ready" and not p._parked
    assert not p.idle() and p.idle_since is None  # never idle while a client is connected
    p.disconnect_client("tab")
    nobody_listened_for(p, player_mod.IDLE_AFTER + 1)
    p.skip(p.now_playing.id)
    settle(p)
    assert p.now_playing is nxt and p.up_next.id in p._parked
    nxt2 = p.up_next
    p.activity()  # a locked phone still reporting or fetching audio: wakes it too
    assert nxt2.ready_event.wait(5) and nxt2.status == "ready" and not p.idle()
    p.shutdown()


def test_audio_fetch_counts_as_listening(login_client):
    now = play(login_client)
    p = login_client.player
    nobody_listened_for(p, player_mod.IDLE_AFTER + 1)
    assert p.idle()
    assert login_client.get(f"/audio/{now['id']}.mp3").status_code == 200
    assert not p.idle()


def test_active_player_prefetches(tmp_path):
    maker = CountingMaker()
    p = Player(FakeEngine(cache_dir=tmp_path / "c"), maker)
    p.play_genre("lofi")
    assert p.up_next.ready_event.wait(5) and maker.calls == 2 and not p._parked
    p.shutdown()


def test_closed_player_parks_nothing(tmp_path):
    p = Player(FakeEngine(cache_dir=tmp_path / "c"), CountingMaker())
    nobody_listened_for(p, player_mod.IDLE_AFTER + 1)
    p.play_genre("lofi")
    settle(p)
    assert p._parked
    p.close()
    assert p.closed and not p._parked
    p.activity()
    assert not p._parked


def test_member_player_shares_the_engine_and_is_dropped_on_delete(login_client):
    m = login_client.member()
    p = login_client.users.player(m.user["id"])
    assert p.engine is login_client.player.engine
    assert login_client.player.engine.in_use == login_client.users.hashes_in_use
    login_client.delete(f"/users/{m.user['id']}")
    assert p.closed and login_client.users.existing(m.user["id"]) is None
