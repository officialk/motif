"""Playlists as a queue: previous/next, jumping, shuffle in place, Shuffle+, Liked songs."""

from app.player import Track
from tests.conftest import wait_ready
from tests.test_repeat_settings import make_playlist


def ai_playlist(client, n=3, prompt="calm lofi"):
    """An AI playlist (Shuffle+ is offered for these) with n saved songs."""
    pl = make_playlist(client, n)
    with client.library.db.connect() as db:
        db.execute("UPDATE playlists SET source = 'ai', prompt = ? WHERE id = ?", (prompt, pl["id"]))
    return client.get(f"/playlists/{pl['id']}").json()["playlist"]


def test_queue_lists_the_whole_playlist_with_the_current_song(client):
    pl = make_playlist(client, 3)
    order = pl["song_ids"]
    client.post(f"/playlists/{pl['id']}/play", json={"start": order[1]})
    q = client.get("/queue").json()
    assert q["radio"] is False and q["repeats"] is False
    assert [e["id"] for e in q["items"]] == order and q["current"] == 1  # earlier songs stay visible
    s = client.post("/queue/jump", json={"pos": 2}).json()
    assert s["now"]["library_id"] == order[2] and s["at_end"] is True
    assert client.post("/queue/jump", json={"pos": 9}).status_code == 409
    client.post("/repeat", json={"mode": "all"})
    assert client.get("/queue").json()["repeats"] is True


def test_previous_goes_back_in_the_queue(client):
    pl = make_playlist(client, 3)
    order = pl["song_ids"]
    client.post(f"/playlists/{pl['id']}/play", json={"start": order[1]})
    r = client.post("/previous").json()
    assert r["moved"] is True and r["now"]["library_id"] == order[0]
    assert r["up_next"]["library_id"] == order[1]
    assert client.post("/previous").json()["moved"] is False  # first song, repeat off: the page restarts it
    client.post("/repeat", json={"mode": "all"})
    assert client.post("/previous").json()["now"]["library_id"] == order[2]  # wraps


def test_previous_on_a_radio_replays_the_last_song_and_next_returns(client):
    s = client.post("/genre", json={"genre": "chiptune"}).json()
    wait_ready(client, s["now"]["id"])
    first = client.get("/state").json()["now"]
    s = client.post("/skip", json={"from_id": first["id"]}).json()
    wait_ready(client, s["now"]["id"])
    left = client.get("/state").json()["now"]
    r = client.post("/previous").json()
    assert r["moved"] is True and r["now"]["hash"] == first["hash"]
    assert r["up_next"]["hash"] == left["hash"]  # forward again goes to the song we left


def test_shuffle_switches_in_place(client):
    pl = make_playlist(client, 4)
    order = pl["song_ids"]
    s = client.post(f"/playlists/{pl['id']}/play", json={"start": order[2]}).json()
    now = s["now"]["id"]
    s = client.post(f"/playlists/{pl['id']}/shuffle", json={"mode": "on"}).json()
    assert s["now"]["id"] == now and s["mode"]["playlist"]["shuffle"] is True  # the song keeps playing
    q = client.get("/queue").json()
    assert sorted(e["id"] for e in q["items"]) == sorted(order) and q["items"][q["current"]]["id"] == order[2]
    s = client.post(f"/playlists/{pl['id']}/shuffle", json={"mode": "off"}).json()
    assert [e["id"] for e in client.get("/queue").json()["items"]] == order
    assert s["now"]["playlist_pos"] == 2 and s["up_next"]["library_id"] == order[3]
    assert client.post("/playlists/other/shuffle", json={"mode": "on"}).status_code == 409


def test_shuffle_plus_is_a_radio_on_the_playlist(client):
    pl = ai_playlist(client, 2)
    client.player.claude_ratio = 0.0  # only the playlist's own songs
    client.post("/repeat", json={"mode": "off"})
    s = client.post(f"/playlists/{pl['id']}/play", json={"plus": True}).json()
    assert s["mode"]["playlist"]["plus"] is True and s["mode"]["playlist"]["shuffle"] is True
    seen = []
    for _ in range(5):  # more songs than the playlist has: it never stops
        assert s["up_next"] is not None and s["stopped"] is False
        seen.append(s["now"]["library_id"])
        s = client.post("/skip", json={"from_id": s["now"]["id"]}).json()
    assert set(seen) == set(pl["song_ids"])
    assert client.get("/queue").json()["radio"] is True
    assert client.post("/queue/jump", json={"pos": 0}).status_code == 409  # no queue to jump in


def test_shuffle_plus_new_songs_play_but_are_not_saved(client):
    pl = ai_playlist(client, 2, prompt="rainy jazz")
    client.player.claude_ratio = 1.0  # every next song is new
    s = client.post(f"/playlists/{pl['id']}/play", json={"plus": True}).json()
    nxt = s["up_next"]
    assert nxt["library_id"] is None and nxt["mood"] == "rainy jazz"  # made from the playlist's prompt
    wait_ready(client, nxt["id"])
    s = client.post("/skip", json={"from_id": s["now"]["id"]}).json()
    assert s["now"]["id"] == nxt["id"]
    assert client.get(f"/playlists/{pl['id']}").json()["playlist"]["count"] == 2  # not saved


def test_shuffle_plus_next_while_a_new_song_is_being_made_plays_a_playlist_song(client):
    pl = ai_playlist(client, 2)
    s = client.post(f"/playlists/{pl['id']}/play", json={"plus": True}).json()
    p = client.player
    with p.lock:  # a new song still being composed (never finished here)
        pending = Track(genre=None, mood="slow")
        p._register(pending)
        p.up_next = pending
    s = client.post("/skip", json={"from_id": s["now"]["id"], "manual": True}).json()
    assert s["now"]["library_id"] in pl["song_ids"]  # no waiting
    assert s["up_next"]["id"] == pending.id  # the new song comes after it


def test_stopped_playlist_resumes_when_play_is_pressed(client):
    pl = make_playlist(client, 1)
    s = client.post(f"/playlists/{pl['id']}/play", json={}).json()
    s = client.post("/skip", json={"from_id": s["now"]["id"]}).json()
    assert s["stopped"] is True
    client.post("/speaker/claim", json={"client": "tab", "kind": "site"})
    client.post("/playback", json={"client": "tab", "track_id": s["now"]["id"], "state": "playing", "position": 0})
    assert client.get("/state").json()["stopped"] is False


def test_liked_songs_play_like_a_playlist(client):
    pl = make_playlist(client, 2)
    for sid in pl["song_ids"]:
        client.post("/like", json={"id": sid})
    liked = client.get("/playlists/liked").json()["playlist"]
    assert liked["name"] == "Liked songs" and liked["count"] == 2
    s = client.post("/playlists/liked/play", json={}).json()
    assert s["mode"]["playlist"]["id"] == "liked" and s["now"]["library_id"] == liked["song_ids"][0]
    assert client.post("/playlists/liked/generate", json={}).status_code == 404  # can't be generated into


def test_library_edits_notify_open_pages(client):
    pl = make_playlist(client, 1)
    rev = client.player.library_rev
    client.post("/like", json={"id": pl["song_ids"][0]})
    assert client.player.library_rev > rev
    rev = client.player.library_rev
    client.post(f"/playlists/{pl['id']}/play", json={})  # playing isn't an edit
    assert client.player.library_rev == rev


def test_api_docs_group_every_endpoint(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert all(op.get("tags") for ops in paths.values() for op in ops.values())
    assert "/now" not in paths and "/export/{song_id}" not in paths
    genres = client.get("/genres").json()
    assert genres["details"][0].keys() == {"id", "name", "tempo_bpm"}
