"""API keys for tools and the overlay (Streamer.bot, scripts, OBS).

A key is a random `mk_…` token shown once on creation; only its SHA-256 is
stored (the token has 256 bits of entropy, so a fast hash is fine). Each key has
scopes that decide which requests it may make; settings, admin and library edits
are never reachable with a key.
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
PREFIX = "mk_"
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
        ("POST", r"/(repeat|genre|random|play|like|control|speaker/assign)", "control"),
        ("POST", r"/playlists/[^/]+/play", "control"),
        ("POST", r"/prompt", "request"),
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
        self._used: dict[str, float] = {}
        self.revoked: set[str] = set()  # ids revoked while this server runs: open streams close at once
        self._hits: dict[tuple[str, str], deque] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _public(row) -> dict:
        return {"id": row["id"], "name": row["name"], "scopes": row["scopes"].split(","),
                "prefix": row["prefix"], "created": row["created"], "last_used": row["last_used"]}

    def create(self, name: str, scopes: list[str]) -> tuple[dict, str]:
        name = " ".join(str(name).split())[:60]
        scopes = sorted(set(scopes), key=SCOPES.index) if all(s in SCOPES for s in scopes) else None
        if not name:
            raise ValueError("name can't be empty")
        if not scopes:
            raise ValueError(f"choose at least one scope of {', '.join(SCOPES)}")
        token = PREFIX + secrets.token_urlsafe(32)
        row = {"id": uuid.uuid4().hex[:10], "name": name, "hash": _hash(token), "scopes": ",".join(scopes),
               "prefix": token[:7], "created": time.time(), "last_used": None}
        with self.db.connect() as db:
            db.execute("INSERT INTO api_keys (id, name, hash, scopes, prefix, created, last_used) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?)", tuple(row.values()))
        return self._public(row), token

    def list(self) -> list[dict]:
        with self.db.connect() as db:
            rows = db.execute("SELECT * FROM api_keys ORDER BY created").fetchall()
        return [self._public(r) for r in rows]

    def revoke(self, key_id: str) -> bool:
        with self.db.connect() as db:
            gone = db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,)).rowcount > 0
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
        if not token.startswith(PREFIX) or len(token) > 100:
            return None
        with self.db.connect() as db:
            row = db.execute("SELECT * FROM api_keys WHERE hash = ?", (_hash(token),)).fetchone()
            if row is None:
                return None
            now = time.time()
            if now - self._used.get(row["id"], 0) > LAST_USED_EVERY:
                self._used[row["id"]] = now
                db.execute("UPDATE api_keys SET last_used = ? WHERE id = ?", (now, row["id"]))
        return self._public(row)

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
