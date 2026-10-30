import json

import pytest

from app import apikeys
from tests.conftest import wait_ready


def new_key(client, *scopes, name="bot"):
    r = client.post("/settings/keys", json={"name": name, "scopes": list(scopes)})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def overlay_key(client, name="OBS"):
    r = client.post("/settings/keys", json={"name": name, "kind": "overlay"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def read_events(tc, n, headers=None, **params):
    """Read the first n SSE `state` events from /events."""
    tc.app.state.sse_ping = 0.2
    out = []
    q = {"client": "test-client", **params}
    with tc.stream("GET", "/events", params=q, headers=headers or {}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        data = None
        for line in r.iter_lines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
            elif line == "" and data is not None:
                out.append(data)
                data = None
                if len(out) >= n:
                    break
    return out


def test_key_lifecycle_and_scopes(client):
    r = client.post("/settings/keys", json={"name": "Streamer.bot", "scopes": ["control", "read"]}).json()
    token, key = r["token"], r["key"]
    assert token.startswith("mk_") and key["scopes"] == ["read", "control"] and key["prefix"] == token[:7]
    assert token not in client.get("/settings/keys").text  # only shown once
    assert client.get("/state", headers=auth(token)).status_code == 200
    assert client.post("/genre", json={"genre": "lofi"}, headers=auth(token)).status_code == 200
    assert client.post("/prompt", json={"text": "x"}, headers=auth(token)).status_code == 403  # no request scope
    for method, path in (("GET", "/settings"), ("GET", "/settings/keys"), ("POST", "/settings/keys"),
                         ("DELETE", "/library/0123456789abcdef"), ("POST", "/download/x"),
                         ("PUT", "/settings/ai"), ("POST", "/backups"), ("GET", "/backups"),
                         ("POST", "/backups/restore"), ("GET", "/users"), ("POST", "/users/invite")):
        assert client.request(method, path, headers=auth(token), json={}).status_code == 403, path
    assert client.get("/state", headers=auth("mk_wrong")).status_code == 401
    assert client.get("/state", headers={"Authorization": "Basic abc"}).status_code == 401
    assert client.delete(f"/settings/keys/{key['id']}").status_code == 200
    assert client.get("/state", headers=auth(token)).status_code == 401  # revoked
    assert client.post("/settings/keys", json={"name": "x", "scopes": ["admin"]}).status_code == 400
    assert client.get("/settings/keys").json()["keys"] == []


def test_key_skips_origin_check_but_browser_writes_still_need_same_origin(client):
    token = new_key(client, "control")
    evil = {"origin": "http://evil.example"}
    assert client.post("/repeat", json={"mode": "all"}, headers={**evil, **auth(token)}).status_code == 200
    assert client.post("/repeat", json={"mode": "all"}, headers=evil).status_code == 403


def test_rate_limit(client, monkeypatch):
    monkeypatch.setitem(apikeys.RATE_LIMITS, "control", (2, 60.0))
    token = new_key(client, "control")
    codes = [client.post("/repeat", json={"mode": "off"}, headers=auth(token)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_overlay_key_reads_but_cant_control(client):
    token = overlay_key(client)
    assert client.get("/state", headers=auth(token)).status_code == 200
    assert client.post("/genre", json={"genre": "lofi"}, headers=auth(token)).status_code == 403


def test_sse_snapshot_and_push_on_api_skip(client):
    token = new_key(client, "read", "control")
    s = client.post("/genre", json={"genre": "chiptune"}).json()
    wait_ready(client, s["now"]["id"])
    first = read_events(client, 1, headers=auth(token))[0]
    assert first["state"]["now"]["id"] == s["now"]["id"] and "server_time" in first
    v0 = first["version"]
    # Something else (e.g. a channel-points bot) skips: the stream pushes the new state.
    client.post("/skip", json={}, headers=auth(token))
    snap = read_events(client, 1, headers=auth(token))[0]
    assert snap["version"] > v0 and snap["state"]["now"]["id"] == s["up_next"]["id"]


def test_speaker_claim_rules_and_release_on_disconnect(client):
    p = client.player
    assert client.post("/speaker/claim", json={"client": "tab1", "kind": "site"}).json()["speaker"]["client"] == "tab1"
    # another site tab doesn't steal it, unless forced ("Play here")
    assert client.post("/speaker/claim", json={"client": "tab2", "kind": "site"}).json()["speaker"]["client"] == "tab1"
    assert client.post("/speaker/claim", json={"client": "tab2", "kind": "site", "force": True}).json()["speaker"]["client"] == "tab2"
    # an overlay always takes over
    token = overlay_key(client)
    r = client.post("/speaker/claim", json={"client": "obs", "kind": "overlay"}, headers=auth(token)).json()
    assert r["speaker"] == {"client": "obs", "kind": "overlay", "since": pytest.approx(r["speaker"]["since"])}
    # its stream closing releases it
    read_events(client, 1, headers=auth(token), client="obs", kind="overlay")
    assert p.speaker is None


def test_playback_reports_and_remote_commands(client):
    assert client.post("/control", json={"action": "pause"}).status_code == 409  # nobody plays yet
    client.post("/speaker/claim", json={"client": "obs", "kind": "overlay"})
    assert client.post("/playback", json={"client": "other", "state": "playing", "position": 1}).status_code == 409
    client.post("/playback", json={"client": "obs", "track_id": "t1", "state": "playing", "position": 12.5, "volume": 0.7})
    c1 = client.post("/control", json={"action": "seek", "t": 30}).json()["command"]
    c2 = client.post("/control", json={"action": "toggle"}).json()["command"]
    assert (c1["seq"], c1["action"], c1["t"], c2["seq"]) == (1, "seek", 30, 2)
    assert client.post("/control", json={"action": "seek"}).status_code == 422
    snap = client.player.live()
    assert snap["playback"]["position"] == 12.5 and snap["playback"]["volume"] == 0.7
    assert snap["command"]["seq"] == 2 and snap["speaker"]["client"] == "obs"


def test_overlay_page_served(client):
    r = client.get("/overlay")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_overlay_key_can_only_do_the_end_of_song_handover(client):
    token = overlay_key(client)
    s = client.post("/genre", json={"genre": "chiptune"}).json()
    assert client.post("/skip", json={}, headers=auth(token)).status_code == 403  # free skip: no
    r = client.post("/skip", json={"from_id": s["now"]["id"]}, headers=auth(token))
    assert r.status_code == 200 and r.json()["skipped"] is True


def test_connected_players_listed_and_assignable(client):
    p = client.player
    p.connect_client("obs-1", "overlay", "Stream PC", True)
    p.connect_client("obs-2", "overlay", None, False)  # display-only
    p.connect_client("tab", "site", None, True)
    names = {c["client"]: c for c in p.live()["clients"]}
    assert names["obs-1"]["name"] == "Stream PC" and names["obs-2"]["audio"] is False and "streams" not in names["tab"]
    assert client.post("/speaker/assign", json={"id": "obs-1"}).json()["speaker"]["client"] == "obs-1"
    assert client.post("/speaker/assign", json={"id": "obs-2"}).status_code == 400
    assert client.post("/speaker/assign", json={"id": "gone"}).status_code == 404
    assert client.post("/speaker/assign", json={"id": "tab"}).json()["speaker"]["kind"] == "site"
    p.disconnect_client("tab")  # the speaker went away
    assert p.speaker is None and "tab" not in {c["client"] for c in p.live()["clients"]}
    # a reconnect overlapping the old stream keeps the client registered
    p.connect_client("obs-1", "overlay", "Stream PC", True)
    p.disconnect_client("obs-1")
    assert "obs-1" in {c["client"] for c in p.live()["clients"]}


def test_revoking_a_key_ends_its_open_stream_and_speaker_role(client):
    import threading

    r = client.post("/settings/keys", json={"name": "OBS", "kind": "overlay"}).json()
    token, key_id = r["token"], r["key"]["id"]
    client.post("/speaker/claim", json={"client": "obs", "kind": "overlay"}, headers=auth(token))
    assert client.player.speaker["client"] == "obs"
    client.app.state.sse_ping = 5.0  # long ping: the revoke itself must end the stream
    timer = threading.Timer(0.5, lambda: client.delete(f"/settings/keys/{key_id}"))
    events = 0
    with client.stream("GET", "/events", params={"client": "obs", "kind": "overlay"}, headers=auth(token)) as resp:
        timer.start()
        for line in resp.iter_lines():  # returns when the server closes the stream
            if line.startswith("data: "):
                events += 1
    assert events >= 1 and client.player.speaker is None
    assert client.get("/state", headers=auth(token)).status_code == 401


def test_overlay_and_api_keys_are_separate_kinds(client):
    ov = client.post("/settings/keys", json={"name": "Stream PC", "kind": "overlay", "scopes": ["control"]}).json()
    assert ov["token"].startswith("mo_") and ov["key"]["kind"] == "overlay"
    assert ov["key"]["scopes"] == ["speaker"]  # fixed: requested scopes are ignored for overlays
    api_token = new_key(client, "read")
    assert api_token.startswith("mk_")
    # API keys can't play audio, and only overlay keys open an overlay
    assert client.post("/settings/keys", json={"name": "x", "scopes": ["speaker"]}).status_code == 400
    r = client.get("/events", params={"client": "obs", "kind": "overlay"}, headers=auth(api_token))
    assert r.status_code == 403 and "overlay key" in r.json()["detail"]
    assert read_events(client, 1, headers=auth(ov["token"]), client="obs", kind="overlay")
    kinds = {k["name"]: k["kind"] for k in client.get("/settings/keys").json()["keys"]}
    assert kinds == {"Stream PC": "overlay", "bot": "api"}


def test_old_speaker_keys_become_overlay_keys(client):
    lib = client.library
    with lib.db.connect() as db:  # a key from before key kinds existed
        db.execute("INSERT INTO api_keys (id, name, hash, scopes, prefix, created, last_used) "
                   "VALUES ('old1', 'Old overlay', 'h', 'read,control,speaker', 'mk_abcd', 0, NULL)")
    keys = apikeys.ApiKeys(lib.db)
    assert {k["id"]: k["kind"] for k in keys.list()}["old1"] == "overlay"
