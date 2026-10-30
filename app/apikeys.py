"""API keys for tools and the overlay (Streamer.bot, scripts, OBS).

Two kinds of key, each a random token shown once on creation (only its SHA-256 is
stored; the token has 256 bits of entropy, so a fast hash is fine):

- API keys (`mk_…`) for tools and bots, with read / control / request scopes.
- Overlay keys (`mo_…`) for overlay pages: they play the audio and show what's
  playing (the `speaker` scope) and nothing else. Only overlay keys open an overlay.

Settings, admin and library edits are never reachable with a key. Every key
belongs to the user who made it and acts as that user (their player and library).
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
import uuid
from collections import deque
from typing import Optional

from app.db import Database

SCOPES = ("read", "control", "request", "speaker")
API_SCOPES = ("read", "control", "request")
PREFIXES = {"api": "mk_", "overlay": "mo_"}
LAST_USED_EVERY = 60.0  # seconds between last_used writes per key
RATE_LIMITS = {"control": (60, 60.0), "skip": (60, 60.0), "request": (6, 60.0)}  # scope -> (requests, per seconds)

# (method, path regex) -> scope a key needs. Anything not listed is refused for keys.
_RULES: list[tuple[str, re.Pattern, str]] = [
    (m, re.compile(p), s) for m, p, s in (
        ("GET", r"/(state|now|queue|history|library|playlists|genres)", "read"),
        ("GET", r"/playlists/[^/]+", "read"),
        ("GET", r"/events", "read"),
        ("GET", r"/audio/[^/]+\.mp3", "read"),
        ("POST", r"/skip", "skip"),
        ("POST", r"/(repeat|genre|random|play|previous|queue/jump|like|control|speaker/assign)", "control"),
        ("POST", r"/playlists/[^/]+/(play|shuffle)", "control"),
        ("POST", r"/(prompt|playlists/[^/]+/generate)", "request"),
        ("POST", r"/(speaker/claim|playback)", "speaker"),
    )
]


def required_scope(method: str, path: str) -> Optional[str]:
    for m, pattern, scope in _RULES:
        if m == method and pattern.fullmatch(path):
            return scope
    return None


def has_scope(scopes: list[str], needed: str) -> bool:
    # A speaker (the overlay) has to read the state and audio it plays, and advance at song end
    # (the /skip handler only lets a speaker-only key do the guarded `from_id` hand-over).
    if needed == "skip":
        return "control" in scopes or "speaker" in scopes
    return needed in scopes or (needed == "read" and "speaker" in scopes)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ApiKeys:
    def __init__(self, db: Database):
        self.db = db
        with self.db.connect() as conn:  # keys made before key kinds existed: speaker keys are overlays
            conn.execute("UPDATE api_keys SET kind = 'overlay' WHERE kind = 'api' AND scopes LIKE '%speaker%'")
        self._used: dict[str, float] = {}
        self.revoked: set[str] = set()  # ids revoked while this server runs: open streams close at once
        self._hits: dict[tuple[str, str], deque] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _public(row) -> dict:
        return {"id": row["id"], "name": row["name"], "scopes": row["scopes"].split(","),
                "kind": row["kind"], "prefix": row["prefix"], "created": row["created"],
                "last_used": row["last_used"]}

    def create(self, name: str, scopes: list[str], kind: str = "api", user_id: str = "local") -> tuple[dict, str]:
        name = " ".join(str(name).split())[:60]
        if not name:
            raise ValueError("name can't be empty")
        if kind == "overlay":
            scopes = ["speaker"]  # plays the audio, reads what's playing, advances at song end
        elif kind == "api":
            if not all(s in API_SCOPES for s in scopes) or not scopes:
                raise ValueError(f"choose at least one scope of {', '.join(API_SCOPES)}")
            scopes = sorted(set(scopes), key=SCOPES.index)
        else:
            raise ValueError("kind must be api or overlay")
        token = PREFIXES[kind] + secrets.token_urlsafe(32)
        row = {"id": uuid.uuid4().hex[:10], "name": name, "hash": _hash(token), "scopes": ",".join(scopes),
               "prefix": token[:7], "created": time.time(), "last_used": None, "kind": kind}
        with self.db.connect() as db:
            db.execute("INSERT INTO api_keys (id, name, hash, scopes, prefix, created, last_used, kind, user_id) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (*row.values(), user_id))
        return self._public(row), token

    def list(self, user_id: str = "local") -> list[dict]:
        with self.db.connect() as db:
            rows = db.execute("SELECT * FROM api_keys WHERE user_id = ? ORDER BY created", (user_id,)).fetchall()
        return [self._public(r) for r in rows]

    def revoke(self, key_id: str, user_id: str = "local") -> bool:
        with self.db.connect() as db:
            gone = db.execute("DELETE FROM api_keys WHERE id = ? AND user_id = ?", (key_id, user_id)).rowcount > 0
        if gone:
            self.revoked.add(key_id)
        return gone

    def active(self, key_id: str) -> bool:
        """Still valid? (Long-lived streams re-check, e.g. after a revoke from another process.)"""
        if key_id in self.revoked:
            return False
        with self.db.connect() as db:
            return db.execute("SELECT 1 FROM api_keys WHERE id = ?", (key_id,)).fetchone() is not None

    def verify(self, token: str) -> Optional[dict]:
        if not token.startswith(tuple(PREFIXES.values())) or len(token) > 100:
            return None
        with self.db.connect() as db:
            row = db.execute("SELECT * FROM api_keys WHERE hash = ?", (_hash(token),)).fetchone()
            if row is None:
                return None
            now = time.time()
            if now - self._used.get(row["id"], 0) > LAST_USED_EVERY:
                self._used[row["id"]] = now
                db.execute("UPDATE api_keys SET last_used = ? WHERE id = ?", (now, row["id"]))
        return {**self._public(row), "user_id": row["user_id"]}

    def allow(self, key_id: str, scope: str) -> bool:
        """Sliding-window rate limit per key and scope (only control and request are limited)."""
        if scope not in RATE_LIMITS:
            return True
        limit, window = RATE_LIMITS[scope]
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault((key_id, scope), deque())
            while hits and now - hits[0] > window:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True
