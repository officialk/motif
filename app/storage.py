"""Optional S3-compatible storage (AWS S3, MinIO, Cloudflare R2, ...).

Layout inside the bucket, under ``S3_PREFIX`` (default ``motif/``)::

    <prefix>media/<hash>.mp3 | .wav        rendered audio of saved/exported songs
    <prefix>backups/motif-YYYYmmdd-HHMMSS.db   SQLite snapshots (UTC timestamps)

With no bucket configured everything stays local and the media store is a
no-op. Settings come from the Settings page (stored in the DB) with the S3_*
environment variables as defaults. Credentials come from the credentials store
(Settings) when both keys are saved there, otherwise from boto3's standard chain;
they are never logged.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config
from .db import REQUIRED_TABLES  # shared with the database layer

log = logging.getLogger(__name__)


_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}


class StorageError(RuntimeError):
    """A storage operation failed with a message safe to show to the user."""


def _is_missing(exc: Exception) -> bool:
    err = getattr(exc, "response", None) or {}
    return str(err.get("Error", {}).get("Code", "")) in _MISSING_CODES


def _content_type(name: str) -> str:
    return "audio/wav" if name.lower().endswith(".wav") else "audio/mpeg"


class MediaStore:
    """Local no-op media store."""

    remote: bool = False

    def fetch(self, name: str, dest: Path) -> bool:
        return False

    def put(self, src: Path, name: str) -> None:
        return None

    def exists(self, name: str) -> bool:
        return False

    def delete(self, name: str) -> None:
        pass


class S3MediaStore(MediaStore):
    remote = True

    def __init__(self, client, bucket: str, prefix: str) -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    def key(self, name: str) -> str:
        return f"{self.prefix}media/{name}"

    def fetch(self, name: str, dest: Path) -> bool:
        dest = Path(dest)
        tmp_name = None
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=dest.name + ".", suffix=".part")
            os.close(fd)
            self.client.download_file(self.bucket, self.key(name), tmp_name)
            os.replace(tmp_name, dest)
            tmp_name = None
            return True
        except Exception as exc:  # never break playback over storage trouble
            if not _is_missing(exc):
                log.warning("S3 fetch of %s failed: %s", name, exc)
            return False
        finally:
            if tmp_name:
                Path(tmp_name).unlink(missing_ok=True)

    def put(self, src: Path, name: str) -> None:
        self.client.upload_file(
            str(src), self.bucket, self.key(name), ExtraArgs={"ContentType": _content_type(name)}
        )

    def delete(self, name: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self.key(name))
        except Exception as exc:  # deleting a copy must never break the app
            log.warning("S3 delete of %s failed: %s", name, exc)

    def exists(self, name: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.key(name))
            return True
        except Exception as exc:
            if not _is_missing(exc):
                log.warning("S3 head of %s failed: %s", name, exc)
            return False


class BackupStore:
    def __init__(self, client, bucket: str, prefix: str) -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    @property
    def backup_prefix(self) -> str:
        return f"{self.prefix}backups/"

    def backup(self, db: "Database | Path") -> dict:
        """Snapshot the live database (always as a SQLite file) and upload it."""
        now = datetime.now(timezone.utc)
        key = f"{self.backup_prefix}motif-{now:%Y%m%d-%H%M%S}.db"
        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="motif-backup-")
        os.close(fd)
        Path(tmp).unlink()  # the snapshot creates it
        try:
            _database(db).snapshot_to(Path(tmp))
            size = os.path.getsize(tmp)
            self.client.upload_file(tmp, self.bucket, key)
        except Exception as exc:
            raise StorageError(f"backup failed: {exc}") from exc
        finally:
            Path(tmp).unlink(missing_ok=True)
        return {"key": key, "size": size, "created": now.timestamp()}

    def list(self) -> list[dict]:
        """Backups, newest first."""
        out: list[dict] = []
        token = None
        try:
            while True:
                kwargs = {"Bucket": self.bucket, "Prefix": self.backup_prefix}
                if token:
                    kwargs["ContinuationToken"] = token
                resp = self.client.list_objects_v2(**kwargs)
                for obj in resp.get("Contents", []):
                    if not obj["Key"].endswith(".db"):
                        continue
                    modified = obj.get("LastModified")
                    out.append(
                        {
                            "key": obj["Key"],
                            "size": int(obj.get("Size", 0)),
                            "created": modified.timestamp() if modified else 0.0,
                        }
                    )
                if not resp.get("IsTruncated"):
                    break
                token = resp.get("NextContinuationToken")
        except Exception as exc:
            raise StorageError(f"could not list backups: {exc}") from exc
        out.sort(key=lambda b: (b["created"], b["key"]), reverse=True)
        return out

    def restore(self, key: str, db: "Database | Path") -> None:
        """Replace the live database content with a verified backup."""
        if not key.startswith(self.backup_prefix) or ".." in key or key == self.backup_prefix:
            raise StorageError("key is not a backup of this app")
        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="motif-restore-")
        os.close(fd)
        try:
            try:
                self.client.download_file(self.bucket, key, tmp)
            except Exception as exc:
                if _is_missing(exc):
                    raise StorageError("backup not found") from exc
                raise StorageError(f"could not download backup: {exc}") from exc
            self._verify(tmp)
            try:
                _database(db).restore_from(Path(tmp))
            except Exception as exc:  # sqlite3 / psycopg errors
                raise StorageError(f"restore failed: {type(exc).__name__}: {exc}") from exc
        finally:
            Path(tmp).unlink(missing_ok=True)

    @staticmethod
    def _verify(path: str) -> None:
        try:
            conn = sqlite3.connect(path)
            try:
                result = conn.execute("PRAGMA integrity_check").fetchone()
                if not result or result[0] != "ok":
                    raise StorageError("backup failed the integrity check")
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise StorageError(f"backup is not a valid SQLite database: {exc}") from exc
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            raise StorageError(f"backup is missing tables: {', '.join(missing)}")


def _database(db: "Database | Path") -> "Database":
    from app.db import Database, SQLiteDatabase

    return db if isinstance(db, Database) else SQLiteDatabase(Path(db))


class BackupScheduler(threading.Thread):
    """Daemon thread that backs up the database every ``interval_hours``."""

    def __init__(self, backup_store: BackupStore, db_path: "Database | Path", interval_hours: float) -> None:
        super().__init__(name="s3-backup", daemon=True)
        self.backup_store = backup_store
        self.db_path = db_path  # a Database, or a SQLite file path
        self.interval = max(float(interval_hours), 0.0) * 3600
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                info = self.backup_store.backup(self.db_path)
                log.info("scheduled backup uploaded: %s", info["key"])
            except Exception as exc:
                log.warning("scheduled backup failed: %s", exc)

    def stop(self) -> None:
        self._stop_event.set()


@dataclass
class StorageSettings:
    bucket: str = ""
    prefix: str = "motif/"
    endpoint: Optional[str] = None
    region: Optional[str] = None
    media: bool = True
    backup_interval_hours: float = 0.0

    def __post_init__(self) -> None:
        self.bucket = (self.bucket or "").strip()
        if self.bucket and (not 3 <= len(self.bucket) <= 63 or any(c.isspace() or c == "/" for c in self.bucket)):
            raise ValueError("bucket name must be 3-63 characters without spaces or slashes")
        prefix = (self.prefix or "").strip().lstrip("/")
        self.prefix = prefix if not prefix or prefix.endswith("/") else prefix + "/"
        self.endpoint = (self.endpoint or "").strip() or None
        if self.endpoint and not self.endpoint.startswith(("http://", "https://")):
            raise ValueError("endpoint must start with http:// or https://")
        self.region = (self.region or "").strip() or None
        self.media = bool(self.media)
        self.backup_interval_hours = float(self.backup_interval_hours or 0)
        if not 0 <= self.backup_interval_hours <= 24 * 30:
            raise ValueError("backup interval must be between 0 and 720 hours")

    @property
    def enabled(self) -> bool:
        return bool(self.bucket)

    @classmethod
    def from_config(cls) -> "StorageSettings":
        return cls(bucket=config.S3_BUCKET, prefix=config.S3_PREFIX, endpoint=config.S3_ENDPOINT_URL,
                   region=config.S3_REGION, media=config.S3_MEDIA,
                   backup_interval_hours=config.S3_BACKUP_INTERVAL_HOURS)

    def fields(self) -> dict:
        return asdict(self)


def s3_client(settings: Optional[StorageSettings] = None, creds=None):
    import boto3  # lazy: only needed when S3 is configured

    s = settings or StorageSettings.from_config()
    kwargs = {"endpoint_url": s.endpoint, "region_name": s.region}
    if creds is not None:
        key_id, secret = creds.get("aws_access_key_id"), creds.get("aws_secret_access_key")
        if key_id and secret:
            kwargs.update(aws_access_key_id=key_id, aws_secret_access_key=secret)
    return boto3.client("s3", **kwargs)


def from_settings(settings: StorageSettings, creds=None, client=None) -> tuple[MediaStore, BackupStore | None]:
    if not settings.enabled:
        return MediaStore(), None
    client = client or s3_client(settings, creds)
    media: MediaStore = S3MediaStore(client, settings.bucket, settings.prefix) if settings.media else MediaStore()
    return media, BackupStore(client, settings.bucket, settings.prefix)


def test_connection(settings: StorageSettings, creds=None, client=None) -> dict:
    """Can we reach the bucket with these settings? {ok, seconds, error?}"""
    t0 = time.monotonic()
    if not settings.enabled:
        return {"ok": False, "seconds": 0.0, "error": "no bucket configured"}
    try:
        client = client or s3_client(settings, creds)
        client.list_objects_v2(Bucket=settings.bucket, Prefix=settings.prefix, MaxKeys=1)
    except Exception as e:  # noqa: BLE001 - report any failure to the user
        code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code")
        msg = f"{type(e).__name__}: {code}" if code else type(e).__name__
        return {"ok": False, "seconds": round(time.monotonic() - t0, 2), "error": msg}
    return {"ok": True, "seconds": round(time.monotonic() - t0, 2)}


test_connection.__test__ = False  # not a pytest test


def from_config(client=None) -> tuple[MediaStore, BackupStore | None]:
    s = StorageSettings.from_config()
    s.media = config.S3_MEDIA
    return from_settings(s, None, client)
