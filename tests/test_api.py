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


def test_prompt_station(client):
    s = client.post("/prompt", json={"text": "smoky jazz with a trumpet"}).json()
    assert s["genre"] == "jazz" and s["mode"]["prompt"] is True and s["mode"]["mood"] == "smoky jazz with a trumpet"
    wait_ready(client, s["now"]["id"])
    assert client.player.get(s["now"]["id"]).spec.instruments.lead == 56
    assert s["up_next"]["genre"] == "jazz" and s["up_next"]["mood"] == s["mode"]["mood"]
    assert client.post("/prompt", json={"text": "<<>>"}).status_code == 400
