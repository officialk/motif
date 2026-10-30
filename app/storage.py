"""Per-user backups to S3-compatible storage (AWS S3, MinIO, Cloudflare R2, ...).

Each user sets their own bucket, prefix, endpoint, region, keys and schedule in
Settings -> Backups (keys encrypted, see app/credentials.py). A backup holds only
that user's data, as a SQLite file::

    <prefix>backups/motif-YYYYmmdd-HHMMSS.db   (UTC timestamps)

and a restore replaces only that user's rows. Song audio is never stored in S3:
renders are deterministic and cached locally by hash.
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

from .credentials import S3_NAMES
from .db import REQUIRED_TABLES  # shared with the database layer

log = logging.getLogger(__name__)


_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}


class StorageError(RuntimeError):
    """A storage operation failed with a message safe to show to the user."""


def _is_missing(exc: Exception) -> bool:
    err = getattr(exc, "response", None) or {}
    return str(err.get("Error", {}).get("Code", "")) in _MISSING_CODES


class BackupStore:
    def __init__(self, client, bucket: str, prefix: str) -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    @property
    def backup_prefix(self) -> str:
        return f"{self.prefix}backups/"

    def backup(self, db: "Database | Path", user_id: str) -> dict:
        """Snapshot one user's data (as a SQLite file) and upload it."""
        now = datetime.now(timezone.utc)
        key = f"{self.backup_prefix}motif-{now:%Y%m%d-%H%M%S}.db"
        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="motif-backup-")
        os.close(fd)
        Path(tmp).unlink()  # the snapshot creates it
        try:
            _database(db).snapshot_user(Path(tmp), user_id)
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

    def restore(self, key: str, db: "Database | Path", user_id: str) -> dict:
        """Replace one user's data with a verified backup (theirs, or an older whole-database one)."""
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
                return _database(db).restore_user(Path(tmp), user_id, keep_secrets=S3_NAMES)
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
    """Daemon thread that backs up one user's data every ``interval_hours``."""

    def __init__(self, backup_store: BackupStore, db: "Database | Path", user_id: str, interval_hours: float) -> None:
        super().__init__(name=f"s3-backup-{user_id}", daemon=True)
        self.backup_store = backup_store
        self.db = db
        self.user_id = user_id
        self.interval = max(float(interval_hours), 0.0) * 3600
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            try:
                info = self.backup_store.backup(self.db, self.user_id)
                log.info("scheduled backup uploaded: %s", info["key"])
            except Exception as exc:
                log.warning("scheduled backup failed: %s", exc)

    def stop(self) -> None:
        self._stop_event.set()


@dataclass
class BackupSettings:
    bucket: str = ""
    prefix: str = "motif/"
    endpoint: Optional[str] = None
    region: Optional[str] = None
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
        self.backup_interval_hours = float(self.backup_interval_hours or 0)
        if not 0 <= self.backup_interval_hours <= 24 * 30:
            raise ValueError("backup interval must be between 0 and 720 hours")

    @property
    def enabled(self) -> bool:
        return bool(self.bucket)

    @classmethod
    def load(cls, saved: Optional[dict]) -> "BackupSettings":
        """From a saved settings row (unknown fields, e.g. the old `media` switch, are ignored)."""
        names = set(cls.__dataclass_fields__)
        try:
            return cls(**{k: v for k, v in (saved or {}).items() if k in names})
        except (TypeError, ValueError):
            log.warning("ignoring invalid saved backup settings")
            return cls()

    def fields(self) -> dict:
        return asdict(self)


def s3_client(settings: BackupSettings, creds=None):
    """boto3 client with the user's saved keys only (never the server's environment or profiles)."""
    import boto3  # lazy: only needed when S3 is configured
    from botocore.config import Config

    key_id = creds.get("aws_access_key_id") if creds is not None else None
    secret = creds.get("aws_secret_access_key") if creds is not None else None
    if not (key_id and secret):
        raise StorageError("enter an access key ID and secret access key")
    return boto3.client("s3", endpoint_url=settings.endpoint, region_name=settings.region,
                        aws_access_key_id=key_id, aws_secret_access_key=secret,
                        config=Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 2}))


def from_settings(settings: BackupSettings, creds=None, client=None) -> Optional[BackupStore]:
    if not settings.enabled:
        return None
    return BackupStore(client or s3_client(settings, creds), settings.bucket, settings.prefix)


def test_connection(settings: BackupSettings, creds=None, client=None) -> dict:
    """Can we reach the bucket with these settings? {ok, seconds, error?}"""
    t0 = time.monotonic()
    if not settings.enabled:
        return {"ok": False, "seconds": 0.0, "error": "no bucket configured"}
    try:
        client = client or s3_client(settings, creds)
        client.list_objects_v2(Bucket=settings.bucket, Prefix=settings.prefix, MaxKeys=1)
    except StorageError as e:
        return {"ok": False, "seconds": round(time.monotonic() - t0, 2), "error": str(e)}
    except Exception as e:  # noqa: BLE001 - report any failure to the user
        code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code")
        msg = f"{type(e).__name__}: {code}" if code else type(e).__name__
        return {"ok": False, "seconds": round(time.monotonic() - t0, 2), "error": msg}
    return {"ok": True, "seconds": round(time.monotonic() - t0, 2)}


test_connection.__test__ = False  # not a pytest test
