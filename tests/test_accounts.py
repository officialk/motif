"""Login mode, sessions, the env admin, local-mode data adoption and outgoing mail."""

import json
import smtplib
import time

import pytest
from fastapi.testclient import TestClient

from app import accounts as acct, config, credentials, mailer
from app.accounts import Accounts
from app.apikeys import ApiKeys
from app.db import OWNED
from app.library import Library
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD


def anon(c) -> TestClient:
    """Same app, no cookies."""
    return TestClient(c.app)


# --- local mode -----------------------------------------------------------------

def test_local_mode_works_without_login(client):
    me = client.get("/auth/me").json()
    assert me["login"] is False and me["user"]["id"] == "local" and me["user"]["role"] == "admin"
    assert all(me["user"]["features"].values())
    assert client.get("/state").status_code == 200
    assert client.post("/repeat", json={"mode": "all"}).status_code == 200
    # accounts are off: no logins, invites or members
    assert client.post("/auth/login", json={"email": "a@b.co", "password": "x"}).status_code == 400
    assert client.get("/users").status_code == 400
    assert client.post("/users/invite", json={"email": "a@b.co"}).status_code == 400
    assert client.get("/auth/token/whatever").status_code == 404


# --- login mode -----------------------------------------------------------------

def test_login_mode_needs_a_session(login_client):
    c = anon(login_client)
    for method, path in (("GET", "/state"), ("GET", "/settings"), ("GET", "/library"), ("POST", "/genre"),
                         ("GET", "/events?client=x"), ("GET", "/users"), ("PUT", "/settings/ai"),
                         ("POST", "/auth/password"), ("GET", "/backups")):
        assert c.request(method, path, json={}).status_code == 401, path
    assert c.get("/auth/me").status_code == 401
    c.cookies.set(acct.COOKIE, "made-up")
    assert c.get("/state").status_code == 401


@pytest.mark.parametrize("path", ["/", "/genres", "/openapi.json", "/docs", "/overlay", "/app.js", "/style.css"])
def test_public_paths(login_client, path):
    assert anon(login_client).get(path).status_code == 200


def test_public_token_paths_answer_without_a_session(login_client):
    c = anon(login_client)
    assert c.get("/auth/token/nope").status_code == 404  # not 401
    assert c.post("/auth/token", json={"token": "nope", "password": "long-enough"}).status_code == 404


def test_login_me_logout(login_client):
    c = anon(login_client)
    r = c.post("/auth/login", json={"email": "  Admin@Example.com ", "password": ADMIN_PASSWORD})
    assert r.status_code == 200 and r.json()["user"]["email"] == ADMIN_EMAIL
    cookie = r.headers["set-cookie"].lower()
    assert acct.COOKIE in cookie and "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert "secure" not in cookie  # plain http here
    token = c.cookies[acct.COOKIE]
    me = c.get("/auth/me").json()
    assert me["login"] is True and me["user"]["role"] == "admin" and all(me["user"]["features"].values())
    assert c.get("/settings").json()["account"]["email"] == ADMIN_EMAIL
    assert c.post("/auth/logout").json() == {"ok": True}
    assert c.get("/auth/me").status_code == 401
    stale = anon(login_client)
    stale.cookies.set(acct.COOKIE, token)
    assert stale.get("/state").status_code == 401  # session row gone
    assert login_client.accounts.session_user(token) is None


def test_secure_cookie_behind_https(login_client):
    r = anon(login_client).post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                                headers={"x-forwarded-proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower()


def test_wrong_password_and_rate_limit(login_client):
    c = anon(login_client)
    r = c.post("/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong-one"})
    assert r.status_code == 401 and r.json()["detail"] == "wrong email or password"
    unknown = c.post("/auth/login", json={"email": "nobody@example.com", "password": "wrong-one"})
    assert unknown.status_code == 401 and unknown.json() == r.json()  # same answer for unknown emails
    for _ in range(4):
        c.post("/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong-one"})
    # limited per email: even the right password waits now
    r = c.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 429 and acct.COOKIE not in c.cookies


def test_rate_limit_per_address(login_client, monkeypatch):
    monkeypatch.setitem(acct.LOGIN_LIMITS, "ip", (3, 60.0))
    c = anon(login_client)
    for i in range(3):
        assert c.post("/auth/login", json={"email": f"x{i}@example.com", "password": "nope"}).status_code == 401
    assert c.post("/auth/login", json={"email": "y@example.com", "password": "nope"}).status_code == 429


def test_session_expires(tmp_path):
    lib = Library(tmp_path / "lib")
    a = Accounts(lib.db, ADMIN_EMAIL, ADMIN_PASSWORD, session_days=-1)
    _, token = a.login_user(ADMIN_EMAIL, ADMIN_PASSWORD, "1.2.3.4")
    assert a.session_user(token) is None


def test_cross_origin_cookie_post_refused(login_client):
    evil = {"origin": "http://evil.example"}
    assert login_client.post("/genre", json={"genre": "lofi"}, headers=evil).status_code == 403
    assert login_client.post("/auth/logout", headers=evil).status_code == 403
    assert login_client.post("/users/invite", json={"email": "x@example.com"}, headers=evil).status_code == 403
    assert login_client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                             headers=evil).status_code == 403
    assert login_client.get("/auth/me").status_code == 200  # still signed in
    assert login_client.post("/repeat", json={"mode": "off"}, headers={"origin": "http://localhost:8000"}).status_code == 200


def test_change_own_password(login_client):
    other = anon(login_client)
    other.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert login_client.post("/auth/password", json={"current": "wrong-pass", "new": "new-pass-123"}).status_code == 400
    assert login_client.post("/auth/password", json={"current": ADMIN_PASSWORD, "new": "short"}).status_code == 422
    assert login_client.post("/auth/password", json={"current": ADMIN_PASSWORD, "new": "new-pass-123"}).json() == {"ok": True}
    assert login_client.get("/auth/me").status_code == 200  # this session stays
    assert other.get("/auth/me").status_code == 401  # others are signed out
    c = anon(login_client)
    assert c.post("/auth/login", json={"email": ADMIN_EMAIL, "password": "new-pass-123"}).status_code == 200


# --- the env admin --------------------------------------------------------------

def test_admin_is_upserted_from_env(tmp_path):
    lib = Library(tmp_path / "lib")
    a = Accounts(lib.db, ADMIN_EMAIL, ADMIN_PASSWORD)
    _, token = a.login_user(ADMIN_EMAIL, ADMIN_PASSWORD, "ip")
    again = Accounts(lib.db, ADMIN_EMAIL.upper(), ADMIN_PASSWORD)  # restart, same env
    assert again.admin_id == a.admin_id and again.session_user(token) is not None  # nothing changed
    assert [u["email"] for u in again.users()] == [ADMIN_EMAIL]

    changed = Accounts(lib.db, ADMIN_EMAIL, "a-new-env-password")  # the env password is the source of truth
    assert changed.admin_id == a.admin_id
    assert changed.session_user(token) is None  # old sessions end with the old password
    with pytest.raises(acct.AccountError):
        changed.login_user(ADMIN_EMAIL, ADMIN_PASSWORD, "ip2")
    assert changed.login_user(ADMIN_EMAIL, "a-new-env-password", "ip2")[0]["role"] == "admin"


def test_env_admin_email_of_a_member_becomes_admin(tmp_path):
    lib = Library(tmp_path / "lib")
    a = Accounts(lib.db, ADMIN_EMAIL, ADMIN_PASSWORD)
    _, token = a.invite("boss@example.com")
    member, _ = a.use_token(token, "member-pass-1")
    assert member["role"] == "member"
    b = Accounts(lib.db, "boss@example.com", "boss-pass-12")
    assert b.admin_id == member["id"] and b.get(member["id"])["role"] == "admin"


def test_login_mode_needs_a_password_and_a_valid_email(tmp_path):
    lib = Library(tmp_path / "lib")
    with pytest.raises(ValueError):
        Accounts(lib.db, ADMIN_EMAIL, None)
    with pytest.raises(ValueError):
        Accounts(lib.db, "not-an-email", "whatever-123")


def test_create_app_reads_the_admin_from_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", ADMIN_PASSWORD)
    from app.main import create_app
    from tests.conftest import fake_player

    lib = Library(tmp_path / "lib")
    with TestClient(create_app(fake_player(tmp_path, lib), lib)) as c:
        assert c.get("/state").status_code == 401
        assert c.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).status_code == 200


# --- local data moves to the admin ------------------------------------------------

def local_data(lib: Library, spec, tmp_path):
    """Everything a local-mode user can own."""
    entry, _ = lib.like(spec, "procedural")
    pl = lib.create_playlist("Old set")
    lib.add_to_playlist(pl["id"], entry["id"])
    lib.record_history("t1", spec, "procedural")
    lib.set_user_setting("app", {"prompt_count": 3})
    lib.set_user_setting("ai", {"backend": "openai"})
    credentials.Credentials(lib.db).update({"openai_api_key": "sk-local"}, "local")
    key, token = ApiKeys(lib.db).create("bot", ["read"])
    return entry, pl, token


def owners(lib: Library) -> dict:
    with lib.db.connect() as db:
        return {t: sorted({r[0] for r in db.execute(f"SELECT user_id FROM {t} WHERE user_id <> ''")}) for t in OWNED}


def test_adopt_local_moves_everything_to_the_admin(tmp_path, spec):
    lib = Library(tmp_path / "lib")
    entry, pl, token = local_data(lib, spec, tmp_path)
    a = Accounts(lib.db, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert all(v == [a.admin_id] for v in owners(lib).values()), owners(lib)
    mine = lib.for_user(a.admin_id)
    assert [e["id"] for e in mine.entries()] == [entry["id"]]
    assert mine.playlist(pl["id"])["song_ids"] == [entry["id"]]
    assert [h["id"] for h in mine.history()] == ["t1"]
    assert mine.user_setting("app") == {"prompt_count": 3} and mine.user_setting("ai") == {"backend": "openai"}
    assert credentials.Credentials(lib.db).get("openai_api_key", a.admin_id) == "sk-local"
    assert ApiKeys(lib.db).verify(token)["user_id"] == a.admin_id
    assert a.key_owner(ApiKeys(lib.db).verify(token)["user_id"])["id"] == a.admin_id
    assert lib.get_setting("accounts_owner") == a.admin_id
    Accounts(lib.db, ADMIN_EMAIL, ADMIN_PASSWORD)  # idempotent
    assert all(v == [a.admin_id] for v in owners(lib).values())


def test_adopt_local_on_a_partly_migrated_database(tmp_path, spec):
    lib = Library(tmp_path / "lib")
    a = Accounts(lib.db, ADMIN_EMAIL, ADMIN_PASSWORD)
    admin = lib.for_user(a.admin_id)
    admin.like(spec, "procedural")
    admin.set_user_setting("app", {"prompt_count": 5})
    # login mode was switched off for a while: local data again, partly the same as the admin's
    entry, pl, _ = local_data(lib, spec, tmp_path)
    other = spec.model_copy(update={"seed": 4321})
    lib.like(other, "procedural")
    Accounts(lib.db, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert all(v == [a.admin_id] for v in owners(lib).values()), owners(lib)
    assert {e["id"] for e in admin.entries()} == {entry["id"], other.audio_hash()}
    assert admin.playlist(pl["id"])["song_ids"] == [entry["id"]]  # points at the admin's copy
    assert admin.user_setting("app") == {"prompt_count": 5}  # the admin's own row wins
    assert admin.user_setting("ai") == {"backend": "openai"}


def test_existing_local_app_becomes_the_admins_in_login_mode(tmp_path, spec):
    lib = Library(tmp_path / "lib")
    entry, pl, token = local_data(lib, spec, tmp_path)
    from app.main import create_app
    from tests.conftest import fake_player

    accounts = Accounts(lib.db, ADMIN_EMAIL, ADMIN_PASSWORD)
    with TestClient(create_app(fake_player(tmp_path, lib), lib, accounts=accounts)) as c:
        c.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert [e["id"] for e in c.get("/library").json()["library"]] == [entry["id"]]
        assert [p["id"] for p in c.get("/playlists").json()["playlists"]] == [pl["id"]]
        assert c.get("/settings").json()["app"]["prompt_count"] == 3
        assert c.get("/settings").json()["ai"]["keys"]["openai"]["set"] is True
        assert c.get("/library", headers={"Authorization": f"Bearer {token}"}).status_code == 200


# --- mail -----------------------------------------------------------------------

class FakeSMTP:
    sent: list = []
    fail = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.steps = host, port, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.steps.append("starttls")

    def login(self, user, password):
        self.steps.append(("login", user))

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise smtplib.SMTPServerDisconnected("gone")
        FakeSMTP.sent.append((self.host, self.port, self.steps, msg))


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.sent, FakeSMTP.fail = [], False
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(config, "SMTP_HOST", "mail.example.com")
    monkeypatch.setattr(config, "SMTP_PORT", 2525)
    monkeypatch.setattr(config, "SMTP_FROM", "motif@example.com")
    monkeypatch.setattr(config, "SMTP_USER", "mailer")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "mail-pass")
    monkeypatch.setattr(config, "SMTP_TLS", "starttls")
    return FakeSMTP


def test_mailer_configured(monkeypatch):
    assert mailer.configured() is False and mailer.send("a@b.co", "s", "t", "<p>t</p>") is False
    monkeypatch.setattr(config, "SMTP_HOST", "mail.example.com")
    monkeypatch.setattr(config, "SMTP_FROM", None)
    monkeypatch.setattr(config, "SMTP_USER", None)
    assert mailer.configured() is False  # needs a sender
    monkeypatch.setattr(config, "SMTP_USER", "mailer@example.com")
    assert mailer.configured() is True


def test_mailer_sends_with_starttls_and_login(smtp):
    assert mailer.send_invite("new@example.com", "http://x/#/welcome?token=abc") is True
    host, port, steps, msg = smtp.sent[0]
    assert (host, port) == ("mail.example.com", 2525) and steps == ["starttls", ("login", "mailer")]
    assert msg["To"] == "new@example.com" and msg["From"] == "motif@example.com"
    assert "token=abc" in msg.get_body(("plain",)).get_content()
    assert "token=abc" in msg.get_body(("html",)).get_content()


def test_mailer_failure_returns_false(smtp, caplog):
    smtp.fail = True
    assert mailer.send_reset("a@example.com", "http://x/#/welcome?token=secret-token") is False
    assert "secret-token" not in caplog.text  # logs carry no links


def test_invite_by_mail_hides_the_link(login_client, smtp):
    r = login_client.post("/users/invite", json={"email": "new@example.com"}).json()
    assert r["sent"] is True and r["link"] is None
    body = smtp.sent[-1][3].get_body(("plain",)).get_content()
    token = body.split("token=")[1].split()[0]
    assert anon(login_client).get(f"/auth/token/{token}").json() == {"email": "new@example.com", "kind": "invite"}
    assert login_client.get("/users").json()["smtp"] is True


def test_failed_mail_returns_the_link(login_client, smtp, monkeypatch):
    smtp.fail = True
    monkeypatch.setattr(config, "PUBLIC_URL", "https://motif.example.com")
    r = login_client.post("/users/invite", json={"email": "new@example.com"}).json()
    assert r["sent"] is False and r["link"].startswith("https://motif.example.com/#/welcome?token=")
    member = TestClient(login_client.app)
    assert member.post("/auth/token", json={"token": r["link"].split("token=")[1],
                                            "password": "member-pass-1"}).status_code == 200


def test_link_uses_the_request_base_without_public_url(login_client):
    r = login_client.post("/users/invite", json={"email": "new@example.com"}).json()
    assert r["sent"] is False and r["link"].startswith("http://testserver/#/welcome?token=")
    assert login_client.get("/users").json()["smtp"] is False


def test_tokens_are_stored_hashed(login_client):
    link = login_client.post("/users/invite", json={"email": "new@example.com"}).json()["link"]
    token = link.split("token=")[1]
    _, session = login_client.accounts.login_user(ADMIN_EMAIL, ADMIN_PASSWORD, "ip")
    with login_client.library.db.connect() as db:
        dump = json.dumps([list(r.values()) for t in ("tokens", "sessions") for r in db.execute(f"SELECT * FROM {t}")])
    assert token not in dump and session not in dump


def test_expired_invite_is_refused(login_client):
    link = login_client.post("/users/invite", json={"email": "new@example.com"}).json()["link"]
    token = link.split("token=")[1]
    with login_client.library.db.connect() as db:
        db.execute("UPDATE tokens SET expires = ?", (time.time() - 1,))
    assert anon(login_client).get(f"/auth/token/{token}").status_code == 404
    assert anon(login_client).post("/auth/token", json={"token": token, "password": "long-enough"}).status_code == 404
