import base64
import json
import os
import sqlite3
import stat

import httpx
import pytest

from app import credentials, storage


def creds(client) -> credentials.Credentials:
    return client.app.state.users.creds


def test_keys_are_write_only_and_kept_out_of_the_db(client):
    secret = "sk-test-0123456789abcdef"
    s = client.put("/settings/ai", json={"backend": "openai", "openai_api_key": secret}).json()
    assert s["ai"]["keys"]["openai"] == {"set": True, "source": "settings"}
    assert secret not in json.dumps(s) and secret not in client.get("/settings").text
    store = creds(client)
    assert store.key_file.parent == client.library.root  # next to the test database, never the real one
    assert stat.S_IMODE(os.stat(store.key_file).st_mode) == 0o600
    with sqlite3.connect(store.db.path) as db:
        user, blob = db.execute("SELECT user_id, value FROM secrets WHERE name = 'openai_api_key'").fetchone()
    assert user == "local" and secret.encode() not in blob and len(blob) > len(secret)  # stored encrypted
    assert not (client.library.root / "credentials.json").exists()
    # omitted = unchanged, "" = cleared
    client.put("/settings/ai", json={"ratio": 0.5})
    assert store.get("openai_api_key", "local") == secret
    s = client.put("/settings/ai", json={"openai_api_key": ""}).json()
    assert s["ai"]["keys"]["openai"] == {"set": False, "source": None}


def test_env_keys_are_never_used(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    monkeypatch.setenv("AI_BACKEND", "claude-api")
    s = client.get("/settings").json()["ai"]
    assert s["backend"] == "off" and s["keys"]["anthropic"] == {"set": False, "source": None}
    client.put("/settings/ai", json={"anthropic_api_key": "from-settings"})
    assert creds(client).get("anthropic_api_key", "local") == "from-settings"
    client.put("/settings/ai", json={"anthropic_api_key": ""})
    assert creds(client).get("anthropic_api_key", "local") is None


def test_malformed_key_rejected(client):
    assert client.put("/settings/ai", json={"openai_api_key": "abc\ndef"}).status_code == 400
    assert client.put("/settings/ai", json={"backend": "nope", "openai_api_key": "good"}).status_code == 400
    assert creds(client).status("openai_api_key", "local")["source"] != "settings"  # nothing saved on error


def test_models_are_per_backend(client):
    users = client.app.state.users
    s = client.put("/settings/ai", json={"backend": "openai", "openai_model": "llama3.1",
                                         "model": "claude-sonnet-5-5"}).json()["ai"]
    assert (s["openai_model"], s["model"]) == ("llama3.1", "claude-sonnet-5-5")
    assert users.ai_settings("local").effective_model == "llama3.1"
    s = client.put("/settings/ai", json={"backend": "claude-api"}).json()["ai"]
    assert users.ai_settings("local").effective_model == "claude-sonnet-5-5" and s["openai_model"] == "llama3.1"
    assert set(s["defaults"]) == {"model", "openai_model", "cli_model", "base_url"}


def test_openai_call_uses_saved_key(client, monkeypatch):
    client.put("/settings/ai", json={"backend": "openai", "openai_api_key": "sk-saved",
                                     "base_url": "http://localhost:11434/v1", "openai_model": "llama3.1"})
    seen = {}

    def fake_post(url, headers, json, timeout):
        seen.update(url=url, headers=headers, model=json["model"])
        body = {"choices": [{"message": {"content": '{"genre": "lofi", "seed": 3, "name": "Saved Key"}'}}]}
        return httpx.Response(200, json=body)

    monkeypatch.setattr(httpx, "post", fake_post)
    r = client.post("/settings/ai/test").json()
    assert r["ok"] and r["source"] == "openai"
    assert seen["headers"]["Authorization"] == "Bearer sk-saved" and seen["model"] == "llama3.1"
    assert seen["url"] == "http://localhost:11434/v1/chat/completions"


class FakeS3:
    def __init__(self):
        self.calls = []

    def list_objects_v2(self, **kw):
        self.calls.append(kw)
        return {"KeyCount": 0}


def test_backup_settings_apply_live(client, monkeypatch):
    fake = FakeS3()
    made = []

    def fake_client(settings=None, creds=None):
        made.append((settings.endpoint, creds.get("aws_access_key_id")))
        return fake

    monkeypatch.setattr(storage, "s3_client", fake_client)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-is-never-used")
    s = client.put("/settings/backup", json={
        "bucket": "my-motif", "prefix": "radio", "endpoint": "http://minio:9000",
        "aws_access_key_id": "AKIATEST", "aws_secret_access_key": "shh-secret",
    }).json()["backup"]
    assert s["enabled"] and s["bucket"] == "my-motif" and s["prefix"] == "radio/"
    assert s["keys"]["aws_secret_access_key"] == {"set": True, "source": "settings"}
    assert "shh-secret" not in client.get("/settings").text
    assert "storage" not in client.get("/settings").json()  # no server-wide storage card any more
    assert isinstance(client.app.state.users.backups("local"), storage.BackupStore)
    assert made[-1] == ("http://minio:9000", "AKIATEST")
    assert client.get("/backups").json() == {"enabled": True, "backups": []}

    r = client.post("/settings/backup/test").json()
    assert r["ok"] and fake.calls[-1] == {"Bucket": "my-motif", "Prefix": "radio/", "MaxKeys": 1}

    s = client.put("/settings/backup", json={"bucket": ""}).json()["backup"]
    assert s["enabled"] is False and client.app.state.users.backups("local") is None
    assert client.get("/backups").json() == {"enabled": False, "backups": []}
    assert client.post("/settings/backup/test").json()["ok"] is False


def test_backup_without_keys_reports_it(client):
    client.put("/settings/backup", json={"bucket": "my-motif"})
    r = client.post("/settings/backup/test").json()
    assert r["ok"] is False and "access key" in r["error"]


@pytest.mark.parametrize("body", [
    {"bucket": "a b"}, {"bucket": "x"}, {"endpoint": "minio:9000", "bucket": "okay-bucket"},
    {"backup_interval_hours": -1}, {"aws_secret_access_key": "bad\nkey"},
])
def test_backup_settings_validation(client, body):
    assert client.put("/settings/backup", json=body).status_code == 400


def test_encryption_at_rest(tmp_path, monkeypatch):
    monkeypatch.delenv("MOTIF_SECRET_KEY", raising=False)
    db = tmp_path / "x.db"
    store = credentials.Credentials(db)
    store.update({"openai_api_key": "sk-abc", "aws_secret_access_key": "aws-shh"}, "u1")
    raw = db.read_bytes()
    assert b"sk-abc" not in raw and b"aws-shh" not in raw
    assert credentials.Credentials(db).get("openai_api_key", "u1") == "sk-abc"  # same key file: readable
    assert store.get("openai_api_key", "u2") is None  # another user's keys are theirs only
    # ciphertexts are bound to their name: swapping them makes both unreadable
    with sqlite3.connect(db) as c:
        a, b = (c.execute("SELECT value FROM secrets WHERE name = ?", (n,)).fetchone()[0]
                for n in ("openai_api_key", "aws_secret_access_key"))
        c.execute("UPDATE secrets SET value = ? WHERE name = 'openai_api_key'", (b,))
    assert store.get("openai_api_key", "u1") is None


def test_backup_without_the_master_key_is_unreadable(tmp_path, monkeypatch):
    monkeypatch.delenv("MOTIF_SECRET_KEY", raising=False)
    db = tmp_path / "a" / "motif.db"
    db.parent.mkdir()
    credentials.Credentials(db).update({"openai_api_key": "sk-abc"}, "u1")
    copy = tmp_path / "b" / "motif.db"  # e.g. a restored S3 backup on another machine
    copy.parent.mkdir()
    copy.write_bytes(db.read_bytes())
    other = credentials.Credentials(copy)  # new machine -> new key file
    assert other.get("openai_api_key", "u1") is None
    assert other.status("openai_api_key", "u1") == {"set": False, "source": None}


def test_master_key_from_env(tmp_path, monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("MOTIF_SECRET_KEY", key)
    (tmp_path / "env").mkdir()
    store = credentials.Credentials(tmp_path / "env" / "e.db")
    store.update({"anthropic_api_key": "ak"}, "u1")
    assert not store.key_file.exists()  # no key file when the env var is used
    assert credentials.Credentials(tmp_path / "env" / "e.db").get("anthropic_api_key", "u1") == "ak"
    monkeypatch.setenv("MOTIF_SECRET_KEY", "not-base64!")
    with pytest.raises(credentials.CredentialsError):
        credentials.Credentials(tmp_path / "f.db")


def test_plaintext_file_is_migrated(tmp_path, monkeypatch):
    monkeypatch.delenv("MOTIF_SECRET_KEY", raising=False)
    (tmp_path / "credentials.json").write_text(json.dumps({"openai_api_key": "sk-old"}))
    store = credentials.Credentials(tmp_path / "motif.db")
    assert store.get("openai_api_key", "local") == "sk-old" and not (tmp_path / "credentials.json").exists()
    assert b"sk-old" not in (tmp_path / "motif.db").read_bytes()
