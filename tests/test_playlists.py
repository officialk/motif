from tests.conftest import wait_ready


def played_tracks(client, n, genre="chiptune"):
    """Play n songs and return their (ready) track ids."""
    s = client.post("/genre", json={"genre": genre}).json()
    ids = []
    for _ in range(n):
        wait_ready(client, s["now"]["id"])
        ids.append(s["now"]["id"])
        wait_ready(client, s["up_next"]["id"])
        s = client.post("/skip", json={"from_id": s["now"]["id"]}).json()
    return ids


def test_playlist_crud_and_saving(client):
    a, b, c = played_tracks(client, 3)
    pl = client.post("/playlists", json={"name": "  Night   drive  ", "song_id": a}).json()["playlist"]
    assert pl["name"] == "Night drive" and pl["count"] == 1
    # adding unsaved tracks saves them to the library first
    for t in (b, c, b):
        r = client.post(f"/playlists/{pl['id']}/songs", json={"id": t})
        assert r.status_code == 200
    pl = client.get(f"/playlists/{pl['id']}").json()["playlist"]
    assert pl["count"] == 3 and len(set(pl["song_ids"])) == 3  # no duplicates
    assert client.get("/library").json()["library"] == []  # adding to a playlist doesn't like a song
    assert pl["seconds"] > 0 and pl["genres"] == ["chiptune"]

    ids = pl["song_ids"]
    rev = list(reversed(ids))
    assert client.put(f"/playlists/{pl['id']}/order", json={"song_ids": rev}).json()["playlist"]["song_ids"] == rev
    assert client.put(f"/playlists/{pl['id']}/order", json={"song_ids": rev[:2]}).status_code == 400

    assert client.patch(f"/playlists/{pl['id']}", json={"name": "Coast"}).json()["playlist"]["name"] == "Coast"
    assert client.patch(f"/playlists/{pl['id']}", json={"name": "   "}).status_code == 400
    removed = client.delete(f"/playlists/{pl['id']}/songs/{rev[0]}").json()["playlist"]
    assert removed["song_ids"] == rev[1:]

    lists = client.get("/playlists").json()["playlists"]
    assert [(x["name"], x["count"]) for x in lists] == [("Coast", 2)] and "songs" not in lists[0]
    assert client.delete(f"/playlists/{pl['id']}").status_code == 200
    assert client.get(f"/playlists/{pl['id']}").status_code == 404
    assert client.library.spec(ids[0]) is None  # unliked songs leave with their last playlist


def test_renaming_a_saved_song_shows_in_playlists(client):
    (a,) = played_tracks(client, 1)
    pl = client.post("/playlists", json={"name": "One", "song_id": a}).json()["playlist"]
    song_id = pl["song_ids"][0]
    assert client.patch(f"/library/{song_id}", json={"name": "Renamed"}).json()["entry"]["name"] == "Renamed"
    assert client.get(f"/playlists/{pl['id']}").json()["playlist"]["songs"][0]["name"] == "Renamed"
    assert client.patch("/library/0123456789abcdef", json={"name": "x"}).status_code == 404
    assert client.delete("/library/0123456789abcdef").status_code == 404


def test_play_playlist_in_order_and_wrap(client):
    tracks = played_tracks(client, 3)
    pl = client.post("/playlists", json={"name": "Set"}).json()["playlist"]
    for t in tracks:
        client.post(f"/playlists/{pl['id']}/songs", json={"id": t})
    order = client.get(f"/playlists/{pl['id']}").json()["playlist"]["song_ids"]

    client.post("/repeat", json={"mode": "all"})  # wrap around at the end
    s = client.post(f"/playlists/{pl['id']}/play", json={}).json()
    assert s["mode"]["playlist"] == {"id": pl["id"], "name": "Set", "shuffle": False, "count": 3, "position": 0}
    seen = []
    for _ in range(4):
        assert s["now"]["library_id"]  # rebuilt from the saved spec (cache hit here)
        wait_ready(client, s["now"]["id"])
        assert client.get(f"/audio/{s['now']['id']}.mp3").status_code == 200
        seen.append(s["now"]["library_id"])
        s = client.post("/skip", json={"from_id": s["now"]["id"]}).json()
    assert seen == order + order[:1]  # wraps around

    # start from a given song; picking a genre leaves playlist mode
    s = client.post(f"/playlists/{pl['id']}/play", json={"start": order[2]}).json()
    assert s["now"]["library_id"] == order[2] and s["up_next"]["library_id"] == order[0]
    s = client.post("/genre", json={"genre": "house"}).json()
    assert s["mode"]["playlist"] is None and s["now"]["library_id"] is None


def test_play_playlist_shuffled_and_errors(client):
    tracks = played_tracks(client, 3)
    pl = client.post("/playlists", json={"name": "Mix"}).json()["playlist"]
    empty = client.post("/playlists", json={"name": "Empty"}).json()["playlist"]
    for t in tracks:
        client.post(f"/playlists/{pl['id']}/songs", json={"id": t})
    order = client.get(f"/playlists/{pl['id']}").json()["playlist"]["song_ids"]
    s = client.post(f"/playlists/{pl['id']}/play", json={"shuffle": True, "start": order[1]}).json()
    assert s["mode"]["playlist"]["shuffle"] is True and s["now"]["library_id"] == order[1]
    got = {s["now"]["library_id"]}
    for _ in range(2):
        s = client.post("/skip", json={"from_id": s["now"]["id"]}).json()
        got.add(s["now"]["library_id"])
    assert got == set(order)  # every song once per pass

    assert client.post(f"/playlists/{empty['id']}/play", json={}).status_code == 400
    assert client.post("/playlists/nope/play", json={}).status_code == 404
    assert client.post(f"/playlists/{pl['id']}/songs", json={"id": "nope"}).status_code == 404


def test_play_saved_song_directly(client):
    (a,) = played_tracks(client, 1)
    entry = client.post("/like", json={"id": a}).json()["entry"]
    s = client.post("/play", json={"id": entry["id"]}).json()
    assert s["now"]["library_id"] == entry["id"]
    assert s["mode"]["playlist"] is None and s["up_next"]["genre"] == entry["genre"]
