"""Storage tests against an in-memory fake S3 client (no network)."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app import storage
from app.library import Library


def _missing(op: str) -> ClientError:
    return ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, op)


class FakeS3:
    """Implements only the boto3 client methods storage.py uses."""

    def __init__(self, page_size: int = 1000) -> None:
        self.objects: dict[str, dict] = {}
        self.page_size = page_size
        self.extra: dict[str, dict] = {}

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.objects[key] = {
            "body": Path(filename).read_bytes(),
            "modified": datetime.now(timezone.utc) + timedelta(seconds=len(self.objects)),
        }
        self.extra[key] = ExtraArgs or {}

    def download_file(self, bucket, key, filename):
        if key not in self.objects:
            raise _missing("HeadObject")
        Path(filename).write_bytes(self.objects[key]["body"])

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = {"body": Body, "modified": datetime.now(timezone.utc)}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": self.objects[Key]["body"]}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _missing("HeadObject")
        return {"ContentLength": len(self.objects[Key]["body"])}

    def list_objects_v2(self, Bucket, Prefix="", ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken) if ContinuationToken else 0
        page = keys[start : start + self.page_size]
        resp = {
            "Contents": [
                {"Key": k, "Size": len(self.objects[k]["body"]), "LastModified": self.objects[k]["modified"]}
                for k in page
            ],
            "IsTruncated": start + self.page_size < len(keys),
        }
        if resp["IsTruncated"]:
            resp["NextContinuationToken"] = str(start + self.page_size)
        return resp


def make_lib(path: Path, spec, marker: str = "one", user: str = "u1") -> Library:
    """A library with one liked song named `marker` for `user` and one for somebody else."""
    lib = Library(path)
    entry, _ = lib.for_user(user).like(spec, "procedural")
    lib.for_user(user).rename_song(entry["id"], marker)
    lib.for_user("other").like(spec, "procedural")
    return lib


def song_names(lib: Library, user: str = "u1") -> list[str]:
    return [e["name"] for e in lib.for_user(user).entries()]


@pytest.fixture()
def s3() -> FakeS3:
    return FakeS3()


def test_backup_snapshot_while_db_open_holds_only_that_user(s3, tmp_path, spec):
    lib = make_lib(tmp_path / "lib", spec)
    holder = sqlite3.connect(lib.db_path)
    holder.execute("SELECT * FROM songs").fetchall()  # keep a connection active
    store = storage.BackupStore(s3, "bkt", "motif/")
    info = store.backup(lib.db, "u1")
    holder.close()
    assert info["key"].startswith("motif/backups/motif-") and info["key"].endswith(".db")
    assert info["size"] > 0 and isinstance(info["created"], float)

    snap = tmp_path / "snap.db"
    snap.write_bytes(s3.objects[info["key"]]["body"])
    conn = sqlite3.connect(snap)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(storage.REQUIRED_TABLES) <= names
    assert conn.execute("SELECT user_id, name FROM songs").fetchall() == [("u1", "one")]
    assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 0  # never accounts
    conn.close()


def test_list_newest_first_with_pagination(tmp_path):
    s3 = FakeS3(page_size=2)
    store = storage.BackupStore(s3, "bkt", "motif/")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        s3.objects[f"motif/backups/motif-2026010{i + 1}-000000.db"] = {
            "body": b"x" * (i + 1),
            "modified": base + timedelta(days=i),
        }
    s3.objects["motif/media/x.mp3"] = {"body": b"m", "modified": base}
    items = store.list()
    assert [i["key"][-9:-3] for i in items] == ["000000"] * 5
    assert [i["key"] for i in items] == sorted((i["key"] for i in items), reverse=True)
    assert [i["size"] for i in items] == [5, 4, 3, 2, 1]


def test_restore_replaces_content_visible_to_open_connection(s3, tmp_path, spec):
    lib = make_lib(tmp_path / "lib", spec, "original")
    store = storage.BackupStore(s3, "bkt", "motif/")
    info = store.backup(lib.db, "u1")

    reader = sqlite3.connect(lib.db_path)
    lib.for_user("u1").rename_song(spec.audio_hash(), "changed")
    lib.for_user("other").rename_song(spec.audio_hash(), "theirs")
    q = "SELECT name FROM songs WHERE user_id = 'u1'"
    assert reader.execute(q).fetchone()[0] == "changed"

    assert store.restore(info["key"], lib.db, "u1")["songs"] == 1
    assert reader.execute(q).fetchone()[0] == "original"
    reader.close()
    assert song_names(lib) == ["original"] and song_names(lib, "other") == ["theirs"]  # only u1's rows


def test_restore_rejections_leave_live_db_untouched(s3, tmp_path, spec):
    lib = make_lib(tmp_path / "lib", spec, "keep")
    db = lib.db
    store = storage.BackupStore(s3, "bkt", "motif/")

    s3.objects["motif/media/x.db"] = {"body": b"x", "modified": datetime.now(timezone.utc)}
    with pytest.raises(storage.StorageError):
        store.restore("motif/media/x.db", db, "u1")
    with pytest.raises(storage.StorageError):
        store.restore("other/backups/motif-1.db", db, "u1")
    with pytest.raises(storage.StorageError):
        store.restore("motif/backups/../media/x.db", db, "u1")

    s3.objects["motif/backups/motif-20260101-000000.db"] = {
        "body": b"this is not sqlite" * 50,
        "modified": datetime.now(timezone.utc),
    }
    with pytest.raises(storage.StorageError, match="SQLite"):
        store.restore("motif/backups/motif-20260101-000000.db", db, "u1")

    bad = tmp_path / "bad.db"
    c = sqlite3.connect(bad)
    c.execute("CREATE TABLE songs (id TEXT)")
    c.commit()
    c.close()
    s3.objects["motif/backups/motif-20260102-000000.db"] = {
        "body": bad.read_bytes(),
        "modified": datetime.now(timezone.utc),
    }
    with pytest.raises(storage.StorageError, match="missing tables"):
        store.restore("motif/backups/motif-20260102-000000.db", db, "u1")

    with pytest.raises(storage.StorageError, match="not found"):
        store.restore("motif/backups/motif-nope.db", db, "u1")

    assert song_names(lib) == ["keep"]


def test_scheduler_runs_and_stops(s3, tmp_path, spec):
    lib = make_lib(tmp_path / "lib", spec)
    store = storage.BackupStore(s3, "bkt", "motif/")
    sched = storage.BackupScheduler(store, lib.db, "u1", 0.00001)  # ~36 ms
    sched.start()
    deadline = time.time() + 5
    while time.time() < deadline and not any("/backups/" in k for k in s3.objects):
        time.sleep(0.02)
    sched.stop()
    sched.join(timeout=5)
    assert not sched.is_alive()
    assert sched.daemon
    assert any("/backups/" in k for k in s3.objects)


def test_scheduler_survives_failures(tmp_path):
    class Failing:
        calls = 0

        def backup(self, db, user_id):
            Failing.calls += 1
            raise storage.StorageError("nope")

    sched = storage.BackupScheduler(Failing(), tmp_path / "x.db", "u1", 0.00001)
    sched.start()
    deadline = time.time() + 5
    while time.time() < deadline and Failing.calls < 2:
        time.sleep(0.02)
    sched.stop()
    sched.join(timeout=5)
    assert Failing.calls >= 2 and not sched.is_alive()


def test_from_settings_disabled_and_enabled(s3):
    assert storage.from_settings(storage.BackupSettings(bucket=""), client=s3) is None
    store = storage.from_settings(storage.BackupSettings(bucket="bkt", prefix="pre"), client=s3)
    assert isinstance(store, storage.BackupStore) and store.prefix == "pre/" and store.client is s3
    assert store.backup_prefix == "pre/backups/"


def test_s3_client_needs_the_users_own_keys(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "env-is-never-used")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "env-is-never-used")
    with pytest.raises(storage.StorageError, match="access key"):
        storage.s3_client(storage.BackupSettings(bucket="bkt"))


def test_saved_settings_ignore_old_fields():
    s = storage.BackupSettings.load({"bucket": "bkt", "media": True, "prefix": "x"})
    assert s.enabled and s.prefix == "x/"
    assert storage.BackupSettings.load({"bucket": "a b"}).enabled is False  # invalid -> defaults
