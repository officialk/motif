from tests.conftest import wait_ready


def test_genre_renders_now_and_prefetches_next(client):
    r = client.post("/genre", json={"genre": "house"})
    assert r.status_code == 200
    now, nxt = r.json()["now"], r.json()["up_next"]
    assert now["genre"] == nxt["genre"] == "house"
    wait_ready(client, now["id"])
    wait_ready(client, nxt["id"])

    now = client.get("/now").json()["now"]
    assert now["status"] == "ready" and now["tempo"] > 0
    assert now["music_seconds"] < now["duration"] and now["sections"][0]["start"] == 0
    assert client.get("/queue").json()["up_next"]["status"] == "ready"

    mp3 = client.get(now["mp3"])
    assert mp3.status_code == 200 and mp3.headers["content-type"] == "audio/mpeg"
    assert "wav" not in now  # WAV is only produced by /download


def test_skip_promotes_prefetched_and_records_history(client):
    s = client.post("/genre", json={"genre": "chiptune"}).json()
    first, nxt = s["now"]["id"], s["up_next"]["id"]
    wait_ready(client, first)
    r = client.post("/skip", json={"from_id": first}).json()
    assert r["skipped"] is True
    assert r["now"]["id"] == nxt
    assert r["up_next"]["id"] not in (first, nxt)
    assert client.get("/history").json()["history"][0]["id"] == first
    # a late duplicate skip from the same browser tick is ignored
    assert client.post("/skip", json={"from_id": first}).json()["skipped"] is False


def test_random_mode_changes_genre(client):
    s = client.post("/random").json()
    assert s["mode"]["shuffle"] is True
    assert s["up_next"]["genre"] != s["now"]["genre"]


def test_bad_input(client):
    assert client.post("/genre", json={"genre": "polka"}).status_code == 400
    assert client.post("/loop", json={"count": 3}).status_code in (404, 405)  # loop control removed
    assert client.get("/audio/nope.mp3").status_code == 404
    assert client.get("/audio/nope.ogg").status_code == 404


def test_prompt_creates_and_plays_a_playlist(client):
    s = client.post("/prompt", json={"text": "smoky jazz with a trumpet"}).json()
    pl = s["playlist"]
    assert s["genre"] == "jazz" and pl["name"] == "Smoky jazz with a trumpet" and pl["count"] == 8
    assert len(set(pl["song_ids"])) == 8 and pl["genres"] == ["jazz"]
    assert s["mode"]["playlist"]["id"] == pl["id"] and s["now"]["library_id"] == pl["song_ids"][0]
    assert s["up_next"]["library_id"] == pl["song_ids"][1]
    wait_ready(client, s["now"]["id"])
    assert client.player.get(s["now"]["id"]).spec.instruments.lead == 56  # trumpet from the prompt
    assert client.get("/library").json()["library"] == []  # generated songs are not "liked"
    assert client.post("/prompt", json={"text": "chill", "count": 3}).json()["playlist"]["count"] == 3
    assert client.post("/prompt", json={"text": "<<>>"}).status_code == 400
    assert client.post("/prompt", json={"text": "x", "count": 50}).status_code == 422


def test_startup_cues_last_played_or_a_random_radio(client):
    from app.main import cue_startup_song

    cue_startup_song(client.player, client.library)  # empty history -> random radio
    s = client.get("/state").json()
    assert s["now"] is not None and s["mode"]["genre"] in client.get("/genres").json()["genres"]
    wait_ready(client, s["now"]["id"])
    first = client.get("/now").json()["now"]
    client.post("/skip", json={"from_id": first["id"]})  # first song goes to history

    last = client.get("/history").json()["history"][0]["hash"]
    cue_startup_song(client.player, client.library)  # now: the last played song again
    again = client.get("/now").json()["now"]
    assert again["hash"] == last
    assert again["id"] != first["id"]
