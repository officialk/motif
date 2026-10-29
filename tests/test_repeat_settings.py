import sqlite3

import pytest

from app.generator import ai
from tests.conftest import wait_ready
from tests.test_playlists import played_tracks


def make_playlist(client, n=2):
    tracks = played_tracks(client, n)
    pl = client.post("/playlists", json={"name": "Set"}).json()["playlist"]
    for t in tracks:
        client.post(f"/playlists/{pl['id']}/songs", json={"id": t})
    return client.get(f"/playlists/{pl['id']}").json()["playlist"]


def test_repeat_one_replays_the_same_song(client):
    s = client.post("/genre", json={"genre": "chiptune"}).json()
    wait_ready(client, s["now"]["id"])
    s = client.post("/repeat", json={"mode": "one"}).json()
    assert s["mode"]["repeat"] == "one"
    assert s["up_next"]["id"] != s["now"]["id"]
    assert client.player.get(s["up_next"]["id"]).spec == client.player.get(s["now"]["id"]).spec
    s = client.post("/skip", json={"from_id": s["now"]["id"]}).json()
    assert s["up_next"]["hash"] == s["now"]["hash"]
    s = client.post("/repeat", json={"mode": "off"}).json()
    assert s["up_next"].get("hash") != s["now"]["hash"]  # a new song (may still be composing)
    assert client.post("/repeat", json={"mode": "loop"}).status_code == 422


def test_repeat_all_wraps_playlist_and_off_ends_it(client):
    pl = make_playlist(client, 2)
    order = pl["song_ids"]
    client.post("/repeat", json={"mode": "all"})
    s = client.post(f"/playlists/{pl['id']}/play", json={}).json()
    s = client.post("/skip", json={"from_id": s["now"]["id"]}).json()
    assert s["now"]["library_id"] == order[1] and s["up_next"]["library_id"] == order[0]  # wraps

    s = client.post("/repeat", json={"mode": "off"}).json()
    assert s["up_next"]["library_id"] is None  # last song: the radio takes over afterwards
    s = client.post("/skip", json={"from_id": s["now"]["id"]}).json()
    assert s["mode"]["playlist"] is None and s["now"]["genre"] == "chiptune"


def test_repeat_one_in_playlist_keeps_position(client):
    pl = make_playlist(client, 2)
    client.post("/repeat", json={"mode": "one"})
    s = client.post(f"/playlists/{pl['id']}/play", json={}).json()
    assert s["up_next"]["library_id"] == s["now"]["library_id"] and s["up_next"]["playlist_pos"] == 0


def test_ai_settings_roundtrip_and_validation(client):
    s = client.get("/settings").json()
    assert s["ai"]["backend"] == "off" and set(s["ai"]["keys"]) == {"anthropic", "openai"}
    assert all(isinstance(v, bool) for v in s["ai"]["keys"].values())
    assert s["storage"]["s3"] is False
    try:
        r = client.put("/settings/ai", json={"backend": "openai", "model": "llama3.1", "ratio": 0.5,
                                              "base_url": "http://localhost:11434/v1"})
        assert r.status_code == 200 and r.json()["ai"]["backend"] == "openai"
        assert client.player.claude_ratio == 0.5
        with sqlite3.connect(client.library.db_path) as db:  # persisted
            assert "llama3.1" in db.execute("SELECT value FROM settings WHERE key = 'ai'").fetchone()[0]
        assert client.put("/settings/ai", json={"backend": "skynet"}).status_code == 400
        assert client.put("/settings/ai", json={"ratio": 3}).status_code == 400
        assert client.get("/genres").json()["ai"]["backend"] == "openai"
    finally:
        ai.set_settings(ai.AISettings.from_config())


def test_prompt_with_ai_on_lets_ai_choose(client, monkeypatch):
    def fake_make_spec(genre, mood, use_ai):
        from app.generator import procedural
        return procedural.generate(genre or "techno", mood), "openai"

    monkeypatch.setattr(client.player, "spec_maker", fake_make_spec)
    try:
        ai.set_settings(ai.AISettings(**{**ai.AISettings.from_config().__dict__, "backend": "openai"}))
        s = client.post("/prompt", json={"text": "something dark for a warehouse at 4am"}).json()
        assert s["genre"] is None and s["mode"]["prompt"] is True
        wait_ready(client, s["now"]["id"])
        s = client.get("/state").json()
        assert s["now"]["genre"] == "techno" and s["now"]["source"] == "openai"
    finally:
        ai.set_settings(ai.AISettings.from_config())


class FakeBackups:
    def __init__(self):
        self.made = []

    def backup(self, db_path):
        item = {"key": f"music/backups/music-{len(self.made)}.db", "size": db_path.stat().st_size, "created": 1.0}
        self.made.append(item)
        return item

    def list(self):
        return list(reversed(self.made))

    def restore(self, key, db_path):
        from app.storage import StorageError
        if key not in {b["key"] for b in self.made}:
            raise StorageError("unknown backup")


def test_backup_endpoints(client):
    assert client.get("/admin/backups").json() == {"enabled": False, "backups": []}
    assert client.post("/admin/backup").status_code == 400
    client.app.state.backups = FakeBackups()
    b = client.post("/admin/backup").json()["backup"]
    assert client.get("/admin/backups").json()["backups"] == [b]
    assert client.post("/admin/restore", json={"key": b["key"]}).json() == {"ok": True}
    assert client.post("/admin/restore", json={"key": "../etc/passwd"}).status_code == 400


@pytest.mark.parametrize("path", ["/state", "/library", "/playlists", "/settings", "/history"])
def test_read_endpoints_ok(client, path):
    assert client.get(path).status_code == 200


def test_foreign_origin_and_host_refused(client):
    evil = {"origin": "http://evil.example"}
    assert client.put("/settings/ai", json={"base_url": "http://evil.example/v1"}, headers=evil).status_code == 403
    assert client.post("/genre", json={"genre": "lofi"}, headers=evil).status_code == 403
    assert client.get("/state", headers=evil).status_code == 200  # reads are harmless
    assert client.post("/repeat", json={"mode": "off"}, headers={"origin": "http://localhost:8000"}).status_code == 200
    assert client.get("/state", headers={"host": "rebind.evil.example"}).status_code == 400


def test_single_song_playlist_stays_in_playlist_mode_while_playing(client):
    pl = make_playlist(client, 1)
    s = client.post(f"/playlists/{pl['id']}/play", json={}).json()
    assert s["mode"]["playlist"]["id"] == pl["id"] and s["up_next"]["library_id"] is None
    s = client.post("/skip", json={"from_id": s["now"]["id"]}).json()
    assert s["mode"]["playlist"] is None
