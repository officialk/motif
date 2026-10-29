"""API keys and S3 credentials entered in Settings, encrypted in the database (SQLite or Postgres).

Each value is sealed with AES-256-GCM (random 96-bit nonce, the credential's
name as associated data, so ciphertexts can't be swapped between names) and
stored in the `secrets` table. The master key is never stored in the database:

1. `MOTIF_SECRET_KEY` (base64, 32 bytes) if set — best for Docker/servers;
2. otherwise `<library>/secret.key` (next to the SQLite file), created once with mode 600.

So a copy of the database or an S3 backup only contains ciphertext. Restoring
a backup on a machine without the same key leaves those secrets unreadable
(they show as "not set" and can be entered again).

Values are write-only from the browser's point of view: the API only reports
whether a key is set and where it comes from. A saved value wins; otherwise
the usual environment variable is used.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional, Union

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app import config
from app.db import Database, SQLiteDatabase

log = logging.getLogger(__name__)

# setting name -> environment variable used as a fallback
NAMES: dict[str, str] = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "openai_api_key": "OPENAI_API_KEY",
    "aws_access_key_id": "AWS_ACCESS_KEY_ID",
    "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
}
MAX_LEN = 512
KEY_ENV = "MOTIF_SECRET_KEY"
NONCE_BYTES = 12


class CredentialsError(ValueError):
    pass


def load_master_key(key_file: Path) -> bytes:
    """32-byte key from MOTIF_SECRET_KEY, else from key_file (created on first use, mode 600)."""
    env = os.environ.get(KEY_ENV)
    if env:
        try:
            key = base64.b64decode(env, validate=True)
        except (binascii.Error, ValueError) as e:
            raise CredentialsError(f"{KEY_ENV} must be base64") from e
        if len(key) != 32:
            raise CredentialsError(f"{KEY_ENV} must decode to 32 bytes")
        return key
    key_file = Path(key_file)
    if not key_file.exists():
        key_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # another process just created it
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(base64.b64encode(AESGCM.generate_key(bit_length=256)))
            log.info("created master key %s (keep it: backups need it to restore saved keys)", key_file)
    key = base64.b64decode(key_file.read_bytes().strip())
    if len(key) != 32:
        raise CredentialsError(f"{key_file} does not hold a 32-byte key")
    return key


class Credentials:
    def __init__(self, database: Union[Database, Path], key_file: Path | None = None):
        if not isinstance(database, Database):
            database = SQLiteDatabase(Path(database))
            database.init_schema()
        self.db = database
        # The key lives next to the SQLite file, or in the library folder when the data is in Postgres.
        here = database.path.parent if isinstance(database, SQLiteDatabase) else config.LIBRARY_DIR
        self.key_file = Path(key_file or here / "secret.key")
        self._legacy_file = here / "credentials.json"
        self._aead = AESGCM(load_master_key(self.key_file))
        self._lock = threading.Lock()
        self._import_plaintext_file()

    def _seal(self, name: str, value: str) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, value.encode(), name.encode())

    def _open(self, name: str, blob: bytes) -> Optional[str]:
        try:
            return self._aead.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], name.encode()).decode()
        except (InvalidTag, ValueError):
            log.warning("saved %s can't be decrypted with this master key (restored from elsewhere?)", name)
            return None

    def _saved(self, name: str) -> Optional[str]:
        with self.db.connect() as db:
            row = db.execute("SELECT value FROM secrets WHERE name = ?", (name,)).fetchone()
        return self._open(name, bytes(row[0])) if row else None

    def get(self, name: str) -> Optional[str]:
        return self._saved(name) or os.environ.get(NAMES[name]) or None

    def status(self, name: str) -> dict:
        if self._saved(name):
            return {"set": True, "source": "settings"}
        if os.environ.get(NAMES[name]):
            return {"set": True, "source": "env"}
        return {"set": False, "source": None}

    def update(self, values: dict[str, Optional[str]]) -> None:
        """name -> value: a non-empty string saves it, "" removes the saved value, None leaves it."""
        changes = {k: v for k, v in values.items() if v is not None}
        for name, value in changes.items():
            if name not in NAMES:
                raise CredentialsError(f"unknown credential {name!r}")
            if len(value) > MAX_LEN or any(c in value for c in "\r\n\t"):
                raise CredentialsError(f"{name} looks malformed")
        if not changes:
            return
        with self._lock, self.db.connect() as db:
            for name, value in changes.items():
                value = value.strip()
                if value:
                    db.execute("INSERT INTO secrets (name, value) VALUES (?, ?) "
                               "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                               (name, self._seal(name, value)))
                else:
                    db.execute("DELETE FROM secrets WHERE name = ?", (name,))

    def _import_plaintext_file(self) -> None:
        """One-time move of the older plain-text credentials.json into the encrypted table."""
        legacy = self._legacy_file
        if not legacy.exists():
            return
        try:
            data = json.loads(legacy.read_text())
        except (OSError, ValueError):
            data = {}
        self.update({k: v for k, v in data.items() if k in NAMES and isinstance(v, str) and v})
        legacy.unlink()
        log.info("moved saved credentials into the encrypted database table")


_current: Optional[Credentials] = None
_current_lock = threading.Lock()


def current() -> Credentials:
    """The credentials store in use (the app points it at its database)."""
    global _current
    with _current_lock:
        if _current is None:
            _current = Credentials(config.DB_PATH)  # the app replaces this with its own database
        return _current


def use(store: Credentials) -> None:
    global _current
    with _current_lock:
        _current = store
