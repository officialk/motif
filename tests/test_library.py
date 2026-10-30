import json
import sqlite3

from app.library import Library
from tests.conftest import wait_ready


def play_one(client, genre="chiptune"):
    s = client.post("/genre", json={"genre": genre}).json()
    wait_ready(client, s["now"]["id"])
    return client.get("/state").json()["now"]


def test_like_stores_seed_and_spec_not_audio(client):
    now = play_one(client)
    r = client.post("/like", json={"id": now["id"]}).json()
    assert r["already"] is False
    entry = r["entry"]
    assert entry["id"] == now["hash"] and entry["name"] == now["name"] and entry["seed"] == now["seed"]
    with sqlite3.connect(client.library.db_path) as db:
        spec = json.loads(db.execute("SELECT spec FROM songs WHERE id = ?", (entry["id"],)).fetchone()[0])
    assert spec["seed"] == now["seed"] and spec["name"] == now["name"]
    assert not list(client.library.root.glob("**/*.mp3"))  # no audio in the library
    assert client.post("/like", json={"id": now["id"]}).json()["already"] is True
    assert [e["id"] for e in client.get("/library").json()["library"]] == [entry["id"]]


def test_like_unknown(client):
    assert client.post("/like", json={"id": "nope"}).status_code == 404


def test_download_renders_on_server(client):
    now = play_one(client)
    entry = client.post("/like", json={"id": now["id"]}).json()["entry"]
    client.player.engine.mp3_path(client.player.get(now["id"]).spec).unlink()  # force a re-render from the spec
    for fmt, magic in (("mp3", (b"ID3", b"\xff\xfb", b"\xff\xf3")), ("wav", (b"RIFF",))):
        r = client.post(f"/download/{entry['id']}", json={"format": fmt})
        assert r.status_code == 200 and r.content.startswith(magic)
        assert f".{fmt}" in r.headers["content-disposition"]
    assert client.post(f"/download/{now['id']}", json={"format": "wav"}).status_code == 200  # by track id
    assert client.post("/export/" + entry["id"], json={"format": "mp3"}).status_code in (404, 405)  # old path gone
    assert client.post("/download/0123456789abcdef", json={"format": "mp3"}).status_code == 404


def test_saved_song_replays_identically(client):
    now = play_one(client)
    original = client.player.engine.mp3_path(client.player.get(now["id"]).spec).read_bytes()
    entry = client.post("/like", json={"id": now["id"]}).json()["entry"]
    client.player.engine.mp3_path(client.player.get(now["id"]).spec).unlink()
    s = client.post("/play", json={"id": entry["id"]}).json()
    assert s["now"]["hash"] == entry["id"] and s["now"]["library_id"] == entry["id"]
    wait_ready(client, s["now"]["id"])
    assert client.get(f"/audio/{s['now']['id']}.mp3").content == original


def test_history_persisted(client):
    first = play_one(client, "lofi")
    client.post("/skip", json={"from_id": first["id"]})
    hist = client.get("/history").json()["history"]
    assert hist[0]["id"] == first["id"] and hist[0]["hash"] == first["hash"] and "spec" not in hist[0]
    client.player.tracks.clear()  # replay from persisted history after the player forgot the track
    assert client.post("/play", json={"id": first["id"]}).json()["now"]["hash"] == first["hash"]


def test_json_files_imported_once(tmp_path, spec):
    root = tmp_path / "lib"
    (root / "songs" / spec.audio_hash()).mkdir(parents=True)
    (root / "songs" / spec.audio_hash() / "spec.json").write_text(spec.model_dump_json())
    (root / "index.json").write_text(json.dumps([{"id": spec.audio_hash(), "name": "Renamed", "source": "claude"}]))
    (root / "playlists.json").write_text(json.dumps([{"name": "Old list", "songs": [spec.audio_hash(), "gone"]}]))
    old_loop = {"genre": "lofi", "loop_count": 4, "name": "Old Loop"}
    (root / "history.json").write_text(json.dumps([{"id": "t1", "name": "Old Loop", "spec": old_loop}]))
    lib = Library(root)
    assert [(e["name"], e["source"]) for e in lib.entries()] == [("Renamed", "claude")]
    assert [(p["name"], p["count"]) for p in lib.playlists()] == [("Old list", 1)]
    assert lib.history()[0]["name"] == "Old Loop" and lib.history_spec("t1") is None  # old format: not replayable
    assert not (root / "index.json").exists() and (root / "index.imported.json").exists()
    Library(root)  # second start: nothing imported twice
    assert len(lib.entries()) == 1 and len(lib.playlists()) == 1


def test_legacy_music_db_is_renamed(tmp_path, spec):
    old = Library(tmp_path / "lib", db_path=tmp_path / "lib" / "music.db")
    old.like(spec, "procedural")
    lib = Library(tmp_path / "lib")
    assert lib.db_path.name == "motif.db" and not (tmp_path / "lib" / "music.db").exists()
    assert [e["id"] for e in lib.entries()] == [spec.audio_hash()]


def test_delete_song_removes_seed_playlists_history_and_audio(client):
    first = play_one(client, "chiptune")
    entry = client.post("/like", json={"id": first["id"]}).json()["entry"]
    pl = client.post("/playlists", json={"name": "Mix", "song_id": entry["id"]}).json()["playlist"]
    s = client.post("/skip", json={"from_id": first["id"]}).json()  # first song -> history
    assert client.get("/history").json()["history"][0]["hash"] == entry["id"]
    mp3 = client.player.engine.mp3_path(client.player.get(first["id"]).spec)
    assert mp3.exists()

    r = client.delete(f"/songs/{entry['id']}")
    assert r.status_code == 200 and r.json()["deleted"] == {"saved": True, "history": 1}
    assert client.get("/library").json()["library"] == []
    assert client.get(f"/playlists/{pl['id']}").json()["playlist"]["count"] == 0
    assert all(h["hash"] != entry["id"] for h in client.get("/history").json()["history"])
    client.player.pool.submit(lambda: None).result()  # let the cache cleanup run
    assert not mp3.exists()
    assert client.delete(f"/songs/{entry['id']}").status_code == 404


def test_delete_by_track_id_keeps_the_playing_song_audible(client):
    now = play_one(client, "house")
    r = client.delete(f"/songs/{now['id']}")  # not saved, not in history yet, but playing
    assert r.status_code == 200 and r.json()["deleted"] == {"saved": False, "history": 0}
    assert client.player.engine.mp3_path(client.player.get(now["id"]).spec).exists()
    assert client.delete("/songs/nope").status_code == 404


def test_history_skips_back_to_back_repeats_and_limit(tmp_path, spec):
    lib = Library(tmp_path / "lib")
    lib.record_history("t1", spec, "procedural")
    lib.record_history("t2", spec, "procedural")  # same song again right after
    assert [h["id"] for h in lib.history()] == ["t1"]
    other = spec.model_copy(update={"seed": 99})
    lib.record_history("t3", other, "procedural")
    lib.record_history("t4", spec, "procedural")  # not back-to-back: recorded
    assert [h["id"] for h in lib.history()] == ["t4", "t3", "t1"]
    assert len(lib.history(limit=2)) == 2
