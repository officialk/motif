"""Invites, resets, member management and per-member features."""

import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import accounts as acct, storage
from app.db import OWNED
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD
from tests.test_live_api import auth
from tests.test_storage import FakeS3


def token_of(link: str) -> str:
    return link.split("token=")[1]


def login(c, email, password):
    return TestClient(c.app).post("/auth/login", json={"email": email, "password": password})


# --- invites --------------------------------------------------------------------

def test_invite_peek_accept(login_client):
    r = login_client.post("/users/invite", json={"email": " New@Example.com", "features": {"download": False}}).json()
    assert r["invite"]["email"] == "new@example.com" and r["sent"] is False
    assert r["invite"]["features"] == {"download": False, "ai": True, "api": True, "overlay": True, "backup": True}
    listed = login_client.get("/users").json()
    assert [i["id"] for i in listed["invites"]] == [r["invite"]["id"]] and len(listed["users"]) == 1
    token = token_of(r["link"])
    m = TestClient(login_client.app)
    assert m.get(f"/auth/token/{token}").json() == {"email": "new@example.com", "kind": "invite"}
    assert m.post("/auth/token", json={"token": token, "password": "short"}).status_code == 422
    user = m.post("/auth/token", json={"token": token, "password": "member-pass-1"}).json()["user"]
    assert user["role"] == "member" and user["features"]["download"] is False
    me = m.get("/auth/me").json()["user"]  # signed in right away
    assert me["id"] == user["id"] and me["features"] == user["features"]
    assert m.post("/download/0123456789abcdef").status_code == 403
    # the link works once; the invite is gone and the member is listed
    assert m.get(f"/auth/token/{token}").status_code == 404
    assert TestClient(login_client.app).post("/auth/token", json={"token": token, "password": "other-pass-1"}).status_code == 404
    listed = login_client.get("/users").json()
    assert listed["invites"] == [] and {u["email"] for u in listed["users"]} == {ADMIN_EMAIL, "new@example.com"}
    assert login(login_client, "new@example.com", "member-pass-1").status_code == 200


def test_invite_rules(login_client):
    assert login_client.post("/users/invite", json={"email": "not-an-email"}).status_code == 422
    assert login_client.post("/users/invite", json={"email": ADMIN_EMAIL}).status_code == 409
    login_client.member("taken@example.com")
    assert login_client.post("/users/invite", json={"email": "taken@example.com"}).status_code == 409
    # re-inviting a pending email replaces its link
    first = login_client.post("/users/invite", json={"email": "again@example.com"}).json()
    second = login_client.post("/users/invite", json={"email": "again@example.com"}).json()
    assert [i["id"] for i in login_client.get("/users").json()["invites"]] == [second["invite"]["id"]]
    assert login_client.get(f"/auth/token/{token_of(first['link'])}").status_code == 404
    assert login_client.get(f"/auth/token/{token_of(second['link'])}").status_code == 200


def test_resend_keeps_features(login_client):
    r = login_client.post("/users/invite", json={"email": "new@example.com", "features": {"ai": False}}).json()
    again = login_client.post(f"/invites/{r['invite']['id']}/resend").json()
    assert again["invite"]["id"] == r["invite"]["id"] and again["invite"]["features"]["ai"] is False
    assert again["invite"]["expires"] >= r["invite"]["expires"]
    assert login_client.get(f"/auth/token/{token_of(r['link'])}").status_code == 404  # the old link stops
    m = TestClient(login_client.app)
    user = m.post("/auth/token", json={"token": token_of(again["link"]), "password": "member-pass-1"}).json()["user"]
    assert user["features"] == {"download": True, "ai": False, "api": True, "overlay": True, "backup": True}
    assert login_client.post("/invites/nope/resend").status_code == 404


def test_revoke_invite(login_client):
    r = login_client.post("/users/invite", json={"email": "new@example.com"}).json()
    assert login_client.delete(f"/invites/{r['invite']['id']}").json() == {"ok": True}
    assert login_client.get(f"/auth/token/{token_of(r['link'])}").status_code == 404
    assert login_client.get("/users").json()["invites"] == []
    assert login_client.delete(f"/invites/{r['invite']['id']}").status_code == 404


# --- resets and passwords ----------------------------------------------------------

def test_reset_link_flow_ends_sessions(login_client):
    m = login_client.member()
    uid = m.user["id"]
    r = login_client.post(f"/users/{uid}/reset").json()
    assert r["sent"] is False
    token = token_of(r["link"])
    assert m.get("/auth/me").status_code == 200  # a reset link alone signs nobody out
    stranger = TestClient(login_client.app)
    assert stranger.get(f"/auth/token/{token}").json() == {"email": "member@example.com", "kind": "reset"}
    assert stranger.post("/auth/token", json={"token": token, "password": "brand-new-pass"}).json()["user"]["id"] == uid
    assert stranger.get("/auth/me").json()["user"]["id"] == uid
    assert m.get("/auth/me").status_code == 401  # the old session is gone
    assert login(login_client, "member@example.com", "member-pass-1").status_code == 401
    assert login(login_client, "member@example.com", "brand-new-pass").status_code == 200
    assert stranger.get(f"/auth/token/{token}").status_code == 404  # once only
    assert login_client.post("/users/nope/reset").status_code == 404


def test_new_reset_link_replaces_the_old_one(login_client):
    uid = login_client.member().user["id"]
    old = token_of(login_client.post(f"/users/{uid}/reset").json()["link"])
    new = token_of(login_client.post(f"/users/{uid}/reset").json()["link"])
    assert login_client.get(f"/auth/token/{old}").status_code == 404
    assert login_client.get(f"/auth/token/{new}").status_code == 200


def test_admin_sets_a_member_password(login_client):
    m = login_client.member()
    uid = m.user["id"]
    reset = token_of(login_client.post(f"/users/{uid}/reset").json()["link"])
    assert login_client.put(f"/users/{uid}/password", json={"password": "short"}).status_code == 422
    assert login_client.put(f"/users/{uid}/password", json={"password": "set-by-admin-1"}).json() == {"ok": True}
    assert m.get("/auth/me").status_code == 401  # signed out everywhere
    assert login(login_client, "member@example.com", "member-pass-1").status_code == 401
    assert login(login_client, "member@example.com", "set-by-admin-1").status_code == 200
    assert login_client.get(f"/auth/token/{reset}").status_code == 404  # pending reset links end too
    admin_id = login_client.accounts.admin_id
    assert login_client.put(f"/users/{admin_id}/password", json={"password": "whatever-123"}).status_code == 400
    assert login_client.put("/users/nope/password", json={"password": "whatever-123"}).status_code == 404
    assert login(login_client, ADMIN_EMAIL, ADMIN_PASSWORD).status_code == 200


def test_set_password_refuses_any_admin(login_client):
    uid = login_client.member().user["id"]
    with pytest.raises(acct.AccountError, match="MOTIF_ADMIN_PASSWORD"):
        login_client.accounts.set_password(login_client.accounts.admin_id, "whatever-123", by=uid)


def test_set_password_ends_the_members_open_stream(login_client):
    m = login_client.member()
    login_client.app.state.sse_ping = 5.0
    uid = m.user["id"]
    timer = threading.Timer(0.5, lambda: login_client.put(f"/users/{uid}/password", json={"password": "new-pass-123"}))
    with m.stream("GET", "/events", params={"client": "tab"}) as r:
        timer.start()
        assert r.status_code == 200
        lines = [line for line in r.iter_lines()]  # ends when the server closes it
    assert any(line.startswith("data: ") for line in lines)


# --- admin only -----------------------------------------------------------------

def test_members_cant_reach_admin_routes(login_client):
    m = login_client.member()
    other = login_client.member("other@example.com")
    inv = login_client.post("/users/invite", json={"email": "x@example.com"}).json()["invite"]
    for method, path, body in (("GET", "/users", None), ("POST", "/users/invite", {"email": "y@example.com"}),
                               ("POST", f"/invites/{inv['id']}/resend", None), ("DELETE", f"/invites/{inv['id']}", None),
                               ("POST", f"/users/{other.user['id']}/reset", None),
                               ("PUT", f"/users/{other.user['id']}/password", {"password": "hijacked-123"}),
                               ("PATCH", f"/users/{other.user['id']}", {"features": {"ai": False}}),
                               ("DELETE", f"/users/{other.user['id']}", None)):
        assert m.request(method, path, json=body).status_code == 403, path
    assert other.get("/auth/me").json()["user"]["features"]["ai"] is True  # nothing changed
    assert login_client.get("/users").json()["invites"][0]["id"] == inv["id"]
    assert "storage" not in m.get("/settings").json()


def test_admin_features_cant_change(login_client):
    admin_id = login_client.accounts.admin_id
    assert login_client.patch(f"/users/{admin_id}", json={"features": {"ai": False}}).status_code == 400
    assert login_client.patch("/users/nope", json={"features": {"ai": False}}).status_code == 404
    assert all(login_client.get("/auth/me").json()["user"]["features"].values())


def test_delete_member_removes_their_data_keys_and_streams(login_client, spec):
    m = login_client.member()
    uid = m.user["id"]
    lib = login_client.library.for_user(uid)
    entry, _ = lib.like(spec, "procedural")
    pl = lib.create_playlist("Theirs")
    lib.add_to_playlist(pl["id"], entry["id"])
    lib.record_history("t1", spec, "procedural")
    m.put("/settings/app", json={"prompt_count": 4})
    m.put("/settings/ai", json={"openai_api_key": "sk-member"})
    token = m.post("/settings/keys", json={"name": "bot", "scopes": ["read"]}).json()["token"]
    overlay = m.post("/settings/keys", json={"name": "OBS", "kind": "overlay"}).json()["token"]
    login_client.library.for_user(login_client.accounts.admin_id).like(spec, "procedural")  # same song: the admin's stays
    player = login_client.users.player(uid)

    login_client.app.state.sse_ping = 5.0
    timer = threading.Timer(0.5, lambda: login_client.delete(f"/users/{uid}"))
    with TestClient(login_client.app).stream("GET", "/events", params={"client": "obs", "kind": "overlay"},
                                            headers=auth(overlay)) as r:
        timer.start()
        assert r.status_code == 200
        for _ in r.iter_lines():  # returns when the server ends the stream
            pass
    assert player.closed and login_client.users.existing(uid) is None
    with login_client.library.db.connect() as db:
        for table in OWNED:
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (uid,)).fetchone()[0] == 0, table
        assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (uid,)).fetchone()[0] == 0
    assert m.get("/auth/me").status_code == 401
    assert TestClient(login_client.app).get("/state", headers=auth(token)).status_code == 401
    assert [e["id"] for e in login_client.get("/library").json()["library"]] == [entry["id"]]
    assert [u["id"] for u in login_client.get("/users").json()["users"]] == [login_client.accounts.admin_id]
    assert login(login_client, "member@example.com", "member-pass-1").status_code == 401


def test_delete_rules(login_client):
    admin_id = login_client.accounts.admin_id
    assert login_client.delete(f"/users/{admin_id}").status_code == 400
    assert login_client.delete("/users/nope").status_code == 404
    with pytest.raises(acct.AccountError):
        login_client.accounts.delete_user(admin_id, by="someone-else")


# --- features -------------------------------------------------------------------

def set_features(admin, uid, **features):
    r = admin.patch(f"/users/{uid}", json={"features": features})
    assert r.status_code == 200, r.text
    return r.json()["user"]


def test_download_feature(login_client, spec):
    m = login_client.member()
    entry, _ = login_client.library.for_user(m.user["id"]).like(spec, "procedural")
    assert m.post(f"/download/{entry['id']}").status_code == 200
    set_features(login_client, m.user["id"], download=False)
    assert m.post(f"/download/{entry['id']}").status_code == 403
    assert m.get("/auth/me").json()["user"]["features"]["download"] is False
    set_features(login_client, m.user["id"], download=True)
    assert m.post(f"/download/{entry['id']}").status_code == 200


def test_ai_feature(login_client):
    m = login_client.member()
    uid = m.user["id"]
    assert m.put("/settings/ai", json={"backend": "openai", "ratio": 0.5}).status_code == 200
    player = login_client.users.player(uid)
    assert player.claude_ratio == 0.5
    user = set_features(login_client, uid, ai=False)
    assert user["features"]["ai"] is False and user["email"] == "member@example.com"
    assert player.claude_ratio == 0.0  # at once
    assert login_client.users.ai(uid)[0].backend == "off"  # saved settings are kept but never used
    assert login_client.users.ai_settings(uid).backend == "openai"
    assert "ai" not in m.get("/settings").json()
    assert m.put("/settings/ai", json={"ratio": 0.1}).status_code == 403
    assert m.post("/settings/ai/test").status_code == 403
    assert m.get("/genres").json()["ai"]["backend"] == "off"
    assert m.post("/prompt", json={"text": "slow piano"}).json()["playlist"]["source"] is None  # procedural only
    set_features(login_client, uid, ai=True)
    assert player.claude_ratio == 0.5 and m.get("/settings").json()["ai"]["backend"] == "openai"


def test_api_feature_rejects_existing_keys(login_client):
    m = login_client.member()
    uid = m.user["id"]
    token = m.post("/settings/keys", json={"name": "bot", "scopes": ["read"]}).json()["token"]
    tool = TestClient(login_client.app)
    assert tool.get("/state", headers=auth(token)).status_code == 200
    set_features(login_client, uid, api=False)
    r = tool.get("/state", headers=auth(token))
    assert r.status_code == 403 and r.json()["detail"] == "API access is turned off for this account"
    assert m.post("/settings/keys", json={"name": "bot2", "scopes": ["read"]}).status_code == 403
    assert m.post("/settings/keys", json={"name": "OBS", "kind": "overlay"}).status_code == 200  # other kind
    set_features(login_client, uid, api=True)
    assert tool.get("/state", headers=auth(token)).status_code == 200


def test_overlay_feature_ends_open_overlay_stream(login_client):
    m = login_client.member()
    uid = m.user["id"]
    token = m.post("/settings/keys", json={"name": "OBS", "kind": "overlay"}).json()["token"]
    login_client.app.state.sse_ping = 5.0  # long ping: the change itself must end the stream
    timer = threading.Timer(0.5, lambda: set_features(login_client, uid, overlay=False))
    events = 0
    with TestClient(login_client.app).stream("GET", "/events", params={"client": "obs", "kind": "overlay"},
                                            headers=auth(token)) as r:
        timer.start()
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data: "):
                events += 1
                assert json.loads(line[6:])["account"]["id"] == uid
    assert events >= 1
    r = TestClient(login_client.app).get("/state", headers=auth(token))
    assert r.status_code == 403 and r.json()["detail"] == "Overlay access is turned off for this account"
    assert m.post("/settings/keys", json={"name": "OBS 2", "kind": "overlay"}).status_code == 403
    assert m.get("/events", params={"client": "tab", "kind": "overlay"}).status_code == 403
    assert m.post("/settings/keys", json={"name": "bot", "scopes": ["read"]}).status_code == 200  # other kind


def test_api_off_ends_an_open_api_key_stream(login_client):
    m = login_client.member()
    token = m.post("/settings/keys", json={"name": "bot", "scopes": ["read"]}).json()["token"]
    login_client.app.state.sse_ping = 5.0
    timer = threading.Timer(0.5, lambda: set_features(login_client, m.user["id"], api=False))
    with TestClient(login_client.app).stream("GET", "/events", params={"client": "bot"}, headers=auth(token)) as r:
        timer.start()
        assert r.status_code == 200
        for _ in r.iter_lines():
            pass  # returns once the server ends the stream


def test_feature_change_reaches_the_page_stream(login_client):
    m = login_client.member()
    login_client.app.state.sse_ping = 5.0
    timer = threading.Timer(0.5, lambda: set_features(login_client, m.user["id"], download=False))
    seen = []
    with m.stream("GET", "/events", params={"client": "tab"}) as r:
        timer.start()
        for line in r.iter_lines():
            if line.startswith("data: "):
                seen.append(json.loads(line[6:])["account"]["features"]["download"])
                if seen[-1] is False:
                    break
    assert seen[0] is True and seen[-1] is False  # the page stream goes on with the new features


def test_backup_feature_stops_the_schedule(login_client, monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(storage, "s3_client", lambda settings, creds=None: fake)
    m = login_client.member()
    uid = m.user["id"]
    s = m.put("/settings/backup", json={"bucket": "theirs", "backup_interval_hours": 24,
                                        "aws_access_key_id": "AK", "aws_secret_access_key": "SK"}).json()
    assert s["backup"]["enabled"] is True
    sched = login_client.users._schedulers[uid]
    assert sched.is_alive() and sched.user_id == uid
    set_features(login_client, uid, backup=False)
    sched.join(timeout=5)
    assert not sched.is_alive() and uid not in login_client.users._schedulers
    assert "backup" not in m.get("/settings").json()
    for method, path, body in (("GET", "/backups", None), ("POST", "/backups", None),
                               ("POST", "/backups/restore", {"key": "theirs/backups/x.db"}),
                               ("PUT", "/settings/backup", {"bucket": "x-y-z"}), ("POST", "/settings/backup/test", None)):
        assert m.request(method, path, json=body).status_code == 403, path
    set_features(login_client, uid, backup=True)
    assert login_client.users._schedulers[uid].is_alive()  # back on: the schedule restarts
