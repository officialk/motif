"""Optional S3-compatible storage (AWS S3, MinIO, Cloudflare R2, ...).

Layout inside the bucket, under ``S3_PREFIX`` (default ``music/``)::

    <prefix>media/<hash>.mp3 | .wav        rendered audio of saved/exported songs
    <prefix>backups/music-YYYYmmdd-HHMMSS.db   SQLite snapshots (UTC timestamps)

With no ``S3_BUCKET`` configured everything stays local and the media store is a
no-op. Credentials come from boto3's standard chain and are never handled here.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import config

log = logging.getLogger(__name__)

REQUIRED_TABLES = ("songs", "playlists", "playlist_songs", "history", "settings")
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

    def backup(self, db_path: Path) -> dict:
        """Snapshot the live database with the sqlite backup API and upload it."""
        now = datetime.now(timezone.utc)
        key = f"{self.backup_prefix}music-{now:%Y%m%d-%H%M%S}.db"
        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="music-backup-")
        os.close(fd)
        try:
            src = sqlite3.connect(str(db_path))
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
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

    def restore(self, key: str, db_path: Path) -> None:
        """Replace the live database content with a verified backup."""
        if not key.startswith(self.backup_prefix) or ".." in key or key == self.backup_prefix:
            raise StorageError("key is not a backup of this app")
        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="music-restore-")
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
                src = sqlite3.connect(tmp)
                dst = sqlite3.connect(str(db_path))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                    src.close()
            except sqlite3.Error as exc:
                raise StorageError(f"restore failed: {exc}") from exc
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


class BackupScheduler(threading.Thread):
    """Daemon thread that backs up the database every ``interval_hours``."""

    def __init__(self, backup_store: BackupStore, db_path: Path, interval_hours: float) -> None:
        super().__init__(name="s3-backup", daemon=True)
        self.backup_store = backup_store
        self.db_path = Path(db_path)
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


def s3_client():
    import boto3  # lazy: only needed when S3 is configured

    return boto3.client("s3", endpoint_url=config.S3_ENDPOINT_URL, region_name=config.S3_REGION)


def from_config(client=None) -> tuple[MediaStore, BackupStore | None]:
    if not config.S3_BUCKET:
        return MediaStore(), None
    client = client or s3_client()
    media: MediaStore = (
        S3MediaStore(client, config.S3_BUCKET, config.S3_PREFIX) if config.S3_MEDIA else MediaStore()
    )
    return media, BackupStore(client, config.S3_BUCKET, config.S3_PREFIX)
