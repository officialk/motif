"""Storage tests against an in-memory fake S3 client (no network)."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app import config, storage


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


def make_db(path: Path, marker: str = "one") -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS songs (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE IF NOT EXISTS playlists (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE IF NOT EXISTS playlist_songs (playlist_id TEXT, song_id TEXT);
        CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, song_id TEXT);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    conn.execute("DELETE FROM songs")
    conn.execute("INSERT INTO songs VALUES ('a', ?)", (marker,))
    conn.commit()
    conn.close()
    return path


def song_names(path: Path) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT name FROM songs")]
    finally:
        conn.close()


@pytest.fixture()
def s3() -> FakeS3:
    return FakeS3()


def test_local_media_store_is_noop(tmp_path):
    m = storage.MediaStore()
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    assert m.remote is False
    m.put(f, "a.mp3")
    assert m.exists("a.mp3") is False
    assert m.fetch("a.mp3", tmp_path / "b.mp3") is False


def test_media_put_fetch_exists_missing(s3, tmp_path):
    m = storage.S3MediaStore(s3, "bkt", "music/")
    src = tmp_path / "x.mp3"
    src.write_bytes(b"audio-bytes")
    assert m.remote is True
    assert m.exists("x.mp3") is False
    m.put(src, "x.mp3")
    assert "music/media/x.mp3" in s3.objects
    assert s3.extra["music/media/x.mp3"]["ContentType"] == "audio/mpeg"
    wav = tmp_path / "x.wav"
    wav.write_bytes(b"w")
    m.put(wav, "x.wav")
    assert s3.extra["music/media/x.wav"]["ContentType"] == "audio/wav"
    assert m.exists("x.mp3") is True

    dest = tmp_path / "out" / "got.mp3"
    assert m.fetch("x.mp3", dest) is True
    assert dest.read_bytes() == b"audio-bytes"
    assert [p.name for p in dest.parent.iterdir()] == ["got.mp3"]  # no temp left

    missing = tmp_path / "nope.mp3"
    assert m.fetch("nope.mp3", missing) is False
    assert not missing.exists()
    assert list(tmp_path.glob("nope.mp3*")) == []


def test_media_fetch_other_error_returns_false(tmp_path):
    class Boom(FakeS3):
        def download_file(self, *a, **k):
            raise ClientError({"Error": {"Code": "500", "Message": "boom"}}, "GetObject")

    m = storage.S3MediaStore(Boom(), "bkt", "p/")
    assert m.fetch("a.mp3", tmp_path / "a.mp3") is False
    assert list(tmp_path.iterdir()) == []


def test_backup_snapshot_while_db_open(s3, tmp_path):
    db = make_db(tmp_path / "live.db")
    holder = sqlite3.connect(db)
    holder.execute("INSERT INTO songs VALUES ('b', 'two')")
    holder.commit()
    holder.execute("SELECT * FROM songs").fetchall()  # keep connection active
    store = storage.BackupStore(s3, "bkt", "music/")
    info = store.backup(db)
    holder.close()
    assert info["key"].startswith("music/backups/music-") and info["key"].endswith(".db")
    assert info["size"] > 0 and isinstance(info["created"], float)

    snap = tmp_path / "snap.db"
    snap.write_bytes(s3.objects[info["key"]]["body"])
    conn = sqlite3.connect(snap)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(storage.REQUIRED_TABLES) <= names
    assert conn.execute("SELECT count(*) FROM songs").fetchone()[0] == 2
    conn.close()


def test_list_newest_first_with_pagination(tmp_path):
    s3 = FakeS3(page_size=2)
    store = storage.BackupStore(s3, "bkt", "music/")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(5):
        s3.objects[f"music/backups/music-2026010{i + 1}-000000.db"] = {
            "body": b"x" * (i + 1),
            "modified": base + timedelta(days=i),
        }
    s3.objects["music/media/x.mp3"] = {"body": b"m", "modified": base}
    items = store.list()
    assert [i["key"][-9:-3] for i in items] == ["000000"] * 5
    assert [i["key"] for i in items] == sorted((i["key"] for i in items), reverse=True)
    assert [i["size"] for i in items] == [5, 4, 3, 2, 1]


def test_restore_replaces_content_visible_to_open_connection(s3, tmp_path):
    db = make_db(tmp_path / "live.db", "original")
    store = storage.BackupStore(s3, "bkt", "music/")
    info = store.backup(db)

    reader = sqlite3.connect(db)
    w = sqlite3.connect(db)
    w.execute("UPDATE songs SET name='changed'")
    w.commit()
    w.close()
    assert reader.execute("SELECT name FROM songs").fetchone()[0] == "changed"

    store.restore(info["key"], db)
    assert reader.execute("SELECT name FROM songs").fetchone()[0] == "original"
    reader.close()
    assert song_names(db) == ["original"]


def test_restore_rejections_leave_live_db_untouched(s3, tmp_path):
    db = make_db(tmp_path / "live.db", "keep")
    store = storage.BackupStore(s3, "bkt", "music/")

    s3.objects["music/media/x.db"] = {"body": b"x", "modified": datetime.now(timezone.utc)}
    with pytest.raises(storage.StorageError):
        store.restore("music/media/x.db", db)
    with pytest.raises(storage.StorageError):
        store.restore("other/backups/music-1.db", db)
    with pytest.raises(storage.StorageError):
        store.restore("music/backups/../media/x.db", db)

    s3.objects["music/backups/music-20260101-000000.db"] = {
        "body": b"this is not sqlite" * 50,
        "modified": datetime.now(timezone.utc),
    }
    with pytest.raises(storage.StorageError, match="SQLite"):
        store.restore("music/backups/music-20260101-000000.db", db)

    bad = tmp_path / "bad.db"
    c = sqlite3.connect(bad)
    c.execute("CREATE TABLE songs (id TEXT)")
    c.commit()
    c.close()
    s3.objects["music/backups/music-20260102-000000.db"] = {
        "body": bad.read_bytes(),
        "modified": datetime.now(timezone.utc),
    }
    with pytest.raises(storage.StorageError, match="missing tables"):
        store.restore("music/backups/music-20260102-000000.db", db)

    with pytest.raises(storage.StorageError, match="not found"):
        store.restore("music/backups/music-nope.db", db)

    assert song_names(db) == ["keep"]


def test_scheduler_runs_and_stops(s3, tmp_path):
    db = make_db(tmp_path / "live.db")
    store = storage.BackupStore(s3, "bkt", "music/")
    sched = storage.BackupScheduler(store, db, 0.00001)  # ~36 ms
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

        def backup(self, db_path):
            Failing.calls += 1
            raise storage.StorageError("nope")

    sched = storage.BackupScheduler(Failing(), tmp_path / "x.db", 0.00001)
    sched.start()
    deadline = time.time() + 5
    while time.time() < deadline and Failing.calls < 2:
        time.sleep(0.02)
    sched.stop()
    sched.join(timeout=5)
    assert Failing.calls >= 2 and not sched.is_alive()


def test_from_config_disabled_and_enabled(monkeypatch, s3):
    monkeypatch.setattr(config, "S3_BUCKET", "")
    media, backups = storage.from_config(s3)
    assert type(media) is storage.MediaStore and backups is None

    monkeypatch.setattr(config, "S3_BUCKET", "bkt")
    monkeypatch.setattr(config, "S3_PREFIX", "pre/")
    monkeypatch.setattr(config, "S3_MEDIA", True)
    media, backups = storage.from_config(s3)
    assert isinstance(media, storage.S3MediaStore) and media.remote
    assert media.key("a.mp3") == "pre/media/a.mp3"
    assert isinstance(backups, storage.BackupStore) and backups.prefix == "pre/"

    monkeypatch.setattr(config, "S3_MEDIA", False)
    media, backups = storage.from_config(s3)
    assert type(media) is storage.MediaStore and backups is not None
