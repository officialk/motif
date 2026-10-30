"""Users, login sessions and invite / password-reset tokens.

Two modes:

- Login mode (an admin email is configured): every browser request needs a session.
  The admin is created or updated from the environment on every start (the env
  password is the source of truth). Others join through an invite link.
- Local mode (no admin email): no login; everything runs as the built-in user `local`.

Passwords are hashed with scrypt and a per-user salt. Session and invite/reset
tokens are random 256-bit strings; only their SHA-256 is stored.

Data that belongs to a user carries a `user_id` column. Tables that have one are
listed in OWNED_TABLES: on every start in login mode rows still owned by `local`
move to the admin (`adopt_local`), and deleting a user deletes their rows there
(`delete_user`). Extra per-user cleanup (in-memory state) goes in `on_user_deleted`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
import uuid
from collections import deque
from typing import Callable, Optional

from fastapi import HTTPException, Request

from app import config
from app.db import OWNED, Conn, Database

log = logging.getLogger(__name__)

LOCAL_ID = "local"
FEATURES = ("download", "ai", "api", "overlay", "backup")  # what the admin can turn off per member
ALL_FEATURES = {f: True for f in FEATURES}
LOCAL_USER = {"id": LOCAL_ID, "email": "", "role": "admin", "features": dict(ALL_FEATURES)}
COOKIE = "motif_session"
INVITE_TTL = 48 * 3600.0
RESET_TTL = 3600.0
PASSWORD_MIN, PASSWORD_MAX = 8, 200
EMAIL_MAX = 254
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# Failed logins allowed per window: (attempts, seconds) per email and per client address.
LOGIN_LIMITS = {"email": (5, 60.0), "ip": (20, 60.0)}

# Tables with a user_id column. See the module docstring.
OWNED_TABLES: list[str] = list(OWNED)
on_user_deleted: list[Callable[[str], None]] = []
# Tables whose rows are keyed per user by these columns: rows `local` has that the admin already has
# (e.g. after switching login mode off and on again) can't simply change owner.
_OWNED_KEYS = {"songs": "id", "settings": "key", "secrets": "name"}

_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


class AccountError(ValueError):
    """A request that can't be done (bad input or state); `status` is the HTTP code to answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def normalize_email(email: str) -> str:
    return str(email).strip().lower()


def valid_email(email: str) -> str:
    """Normalized email, or ValueError."""
    email = normalize_email(email)
    if len(email) > EMAIL_MAX or not EMAIL.fullmatch(email):
        raise ValueError("enter a valid email address")
    return email


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${dk.hex()}"


def check_password(password: str, stored: Optional[str]) -> bool:
    try:
        _, n, r, p, salt, dk = (stored or "").split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(dk) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), dk)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))  # unknown emails cost the same time as wrong passwords


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _features(row) -> dict:
    """Admin: everything. Members: stored switches (JSON, missing = on)."""
    if row.get("role") == "admin":  # invite rows have no role: they carry the member's features
        return dict(ALL_FEATURES)
    try:
        saved = json.loads(row.get("features") or "{}")
    except (ValueError, TypeError):
        saved = {}
    return {f: bool(saved.get(f, True)) for f in FEATURES}


def _user(row) -> dict:
    return {"id": row["id"], "email": row["email"], "role": row["role"], "features": _features(row)}


def has_feature(user: dict, name: str) -> bool:
    """May this user (dict from the request) use a feature (download / ai / api / overlay)?"""
    return user.get("role") == "admin" or bool(user.get("features", ALL_FEATURES).get(name, True))


class Accounts:
    """Account store. `admin_email` None = local mode."""

    def __init__(self, db: Database, admin_email: Optional[str] = None, admin_password: Optional[str] = None,
                 session_days: float = config.SESSION_DAYS):
        self.db = db
        self.admin_email = normalize_email(admin_email) if admin_email else None
        self.session_seconds = session_days * 86400
        self.admin_id: str = LOCAL_ID
        self._fails: dict[str, deque] = {}
        self.on_deleted: list[Callable[[str], None]] = []  # this instance's cleanup (the app's players)
        self._lock = threading.Lock()
        if self.admin_email is None:
            log.warning("MOTIF_ADMIN_EMAIL is not set: running in local mode without login")
            return
        if not admin_password:
            raise ValueError("MOTIF_ADMIN_PASSWORD is required when MOTIF_ADMIN_EMAIL is set")
        valid_email(self.admin_email)
        self.admin_id = self._ensure_admin(admin_password)
        self.adopt_local()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            conn.execute("DELETE FROM tokens WHERE expires < ?", (time.time() - 30 * 86400,))

    @classmethod
    def from_config(cls, db: Database) -> "Accounts":
        return cls(db, config.ADMIN_EMAIL, config.ADMIN_PASSWORD)

    @property
    def login(self) -> bool:
        return self.admin_email is not None

    def require_login_mode(self) -> None:
        if not self.login:
            raise AccountError("accounts are off: set MOTIF_ADMIN_EMAIL and MOTIF_ADMIN_PASSWORD")

    # --- admin + local data -------------------------------------------------

    def _ensure_admin(self, password: str) -> str:
        """Create or update the env admin; re-hash only when the env password changed."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (self.admin_email,)).fetchone()
            if row is None:
                user_id = uuid.uuid4().hex[:10]
                conn.execute("INSERT INTO users (id, email, role, password, created, last_login) "
                             "VALUES (?, ?, 'admin', ?, ?, NULL)",
                             (user_id, self.admin_email, hash_password(password), time.time()))
                log.info("created the admin account")
                return user_id
            if row["role"] != "admin":
                conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (row["id"],))
            if not check_password(password, row["password"]):
                conn.execute("UPDATE users SET password = ? WHERE id = ?", (hash_password(password), row["id"]))
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
                log.info("updated the admin password from the environment")
            return row["id"]

    def adopt_local(self) -> None:
        """Give rows owned by the local-mode user to the admin (idempotent; runs on every start).

        Also records the first owner in settings (`accounts_owner`) so the switch is visible.
        """
        with self.db.connect() as conn:
            # Same song in both libraries: the local playlists' entries point at the admin's copy.
            conn.execute("UPDATE playlist_songs SET user_id = ? WHERE user_id = ? AND song_id IN "
                         "(SELECT id FROM songs WHERE user_id = ?)", (self.admin_id, LOCAL_ID, self.admin_id))
            for table, key in _OWNED_KEYS.items():  # otherwise the admin's own row wins
                conn.execute(f"DELETE FROM {table} WHERE user_id = ? AND {key} IN "
                             f"(SELECT {key} FROM {table} WHERE user_id = ?)", (LOCAL_ID, self.admin_id))
            for table in OWNED_TABLES:
                moved = conn.execute(f"UPDATE {table} SET user_id = ? WHERE user_id = ?",
                                     (self.admin_id, LOCAL_ID)).rowcount
                if moved:
                    log.info("moved %d %s rows from local mode to the admin", moved, table)
            conn.execute("INSERT INTO settings (user_id, key, value) VALUES ('', 'accounts_owner', ?) "
                         "ON CONFLICT(user_id, key) DO NOTHING", (f'"{self.admin_id}"',))

    def key_owner(self, user_id: Optional[str]) -> Optional[dict]:
        """User an API/overlay key acts as: its owner (None if that user is gone)."""
        if not self.login:
            return dict(LOCAL_USER)
        return self.get(self.admin_id if user_id in (None, LOCAL_ID) else user_id)

    def is_primary(self, user_id: str) -> bool:
        """The env admin (or the local-mode user): the only one who may use server-side tools (claude CLI)."""
        return user_id == self.admin_id

    # --- users -------------------------------------------------------------

    def get(self, user_id: str) -> Optional[dict]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _user(row) if row else None

    def users(self) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created").fetchall()
        return [{**_user(r), "created": r["created"], "last_login": r["last_login"]} for r in rows]

    def set_features(self, user_id: str, changes: dict, by: str) -> dict:
        """Turn member features on/off (partial); the admin's own row can't change."""
        if user_id == by or user_id == self.admin_id:
            raise AccountError("the admin always has every feature")
        unknown = set(changes) - set(FEATURES)
        if unknown:
            raise AccountError(f"unknown feature: {', '.join(sorted(unknown))}")
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise AccountError("unknown user", 404)
            if row["role"] == "admin":
                raise AccountError("the admin always has every feature")
            features = {**_features(row), **{k: bool(v) for k, v in changes.items()}}
            conn.execute("UPDATE users SET features = ? WHERE id = ?", (json.dumps(features), user_id))
        return {**_user({**row, "features": json.dumps(features)}), "created": row["created"],
                "last_login": row["last_login"]}

    def delete_user(self, user_id: str, by: str) -> None:
        if user_id == by:
            raise AccountError("you can't remove yourself")
        if user_id == self.admin_id:
            raise AccountError("the admin from MOTIF_ADMIN_EMAIL can't be removed")
        with self.db.connect() as conn:
            if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
                raise AccountError("unknown user", 404)
            for table in OWNED_TABLES:
                conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM tokens WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        for hook in (*on_user_deleted, *self.on_deleted):
            try:
                hook(user_id)
            except Exception:  # cleanup of in-memory state must not undo the delete
                log.exception("user-deleted hook failed")

    def change_password(self, user_id: str, current: str, new: str, keep_session: Optional[str]) -> None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT password FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None or not check_password(current, row["password"]):
                raise AccountError("current password is wrong")
            conn.execute("UPDATE users SET password = ? WHERE id = ?", (hash_password(new), user_id))
            # sign out everywhere else
            conn.execute("DELETE FROM sessions WHERE user_id = ? AND hash <> ?",
                         (user_id, _hash(keep_session) if keep_session else ""))

    def set_password(self, user_id: str, password: str, by: str) -> None:
        """The admin sets a member's password: their sessions and reset links end."""
        if user_id == by:
            raise AccountError("change your own password in Settings")
        with self.db.connect() as conn:
            row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise AccountError("unknown user", 404)
            if row["role"] == "admin":
                raise AccountError("the admin's password comes from MOTIF_ADMIN_PASSWORD")
            conn.execute("UPDATE users SET password = ? WHERE id = ?", (hash_password(password), user_id))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM tokens WHERE kind = 'reset' AND user_id = ?", (user_id,))

    # --- login + sessions --------------------------------------------------

    def _limited(self, keys: list[tuple[str, str]], record: bool) -> bool:
        """Sliding-window failed-login counter; True when any key is over its limit."""
        now = time.monotonic()
        over = False
        with self._lock:
            for kind, value in keys:
                limit, window = LOGIN_LIMITS[kind]
                hits = self._fails.setdefault(f"{kind}:{value}", deque())
                while hits and now - hits[0] > window:
                    hits.popleft()
                if record:
                    hits.append(now)
                over = over or len(hits) >= limit
                if not hits:
                    del self._fails[f"{kind}:{value}"]
        return over

    def login_user(self, email: str, password: str, ip: str) -> tuple[dict, str]:
        """(user, session token); AccountError 401 on bad credentials, 429 when rate limited."""
        email = normalize_email(email)
        keys = [("email", email), ("ip", ip)]
        if self._limited(keys, record=False):
            raise AccountError("too many attempts, try again in a minute", 429)
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not check_password(password, row["password"] if row else _DUMMY_HASH) or row is None:
            self._limited(keys, record=True)
            raise AccountError("wrong email or password", 401)
        with self._lock:
            self._fails.pop(f"email:{email}", None)
        return _user(row), self.new_session(row["id"])

    def new_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.db.connect() as conn:
            conn.execute("INSERT INTO sessions (hash, user_id, created, expires) VALUES (?, ?, ?, ?)",
                         (_hash(token), user_id, now, now + self.session_seconds))
            conn.execute("UPDATE users SET last_login = ? WHERE id = ?", (now, user_id))
        return token

    def session_user(self, token: str) -> Optional[dict]:
        if not token or len(token) > 100:
            return None
        with self.db.connect() as conn:
            row = conn.execute("SELECT u.id, u.email, u.role, u.features, s.expires FROM sessions s "
                               "JOIN users u ON u.id = s.user_id WHERE s.hash = ?", (_hash(token),)).fetchone()
            if row is None:
                return None
            if row["expires"] < time.time():
                conn.execute("DELETE FROM sessions WHERE hash = ?", (_hash(token),))
                return None
        return _user(row)

    def end_session(self, token: str) -> None:
        with self.db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE hash = ?", (_hash(token),))

    # --- invite + reset tokens ---------------------------------------------

    @staticmethod
    def _invite(row) -> dict:
        return {"id": row["id"], "email": row["email"], "created": row["created"], "expires": row["expires"],
                "features": _features(row)}

    def invites(self) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM tokens WHERE kind = 'invite' ORDER BY created").fetchall()
        return [self._invite(r) for r in rows]

    def invite(self, email: str, features: Optional[dict] = None) -> tuple[dict, str]:
        """(invite, token). `features` is what the member gets (missing ones on).
        Re-inviting a pending email replaces its old token."""
        email = valid_email(email)
        unknown = set(features or {}) - set(FEATURES)
        if unknown:
            raise AccountError(f"unknown feature: {', '.join(sorted(unknown))}")
        chosen = {**ALL_FEATURES, **{k: bool(v) for k, v in (features or {}).items()}}
        token = secrets.token_urlsafe(32)
        now = time.time()
        row = {"id": uuid.uuid4().hex[:10], "hash": _hash(token), "kind": "invite", "email": email,
               "user_id": None, "created": now, "expires": now + INVITE_TTL, "features": json.dumps(chosen)}
        with self.db.connect() as conn:
            if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                raise AccountError("this email already has an account", 409)
            conn.execute("DELETE FROM tokens WHERE kind = 'invite' AND email = ?", (email,))
            conn.execute("INSERT INTO tokens (id, hash, kind, email, user_id, created, expires, features) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(row.values()))
        return self._invite(row), token

    def resend_invite(self, invite_id: str) -> tuple[dict, str]:
        """New token (and expiry) for a pending invite."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM tokens WHERE id = ? AND kind = 'invite'", (invite_id,)).fetchone()
            if row is None:
                raise AccountError("unknown invite", 404)
            conn.execute("UPDATE tokens SET hash = ?, created = ?, expires = ? WHERE id = ?",
                         (_hash(token), now, now + INVITE_TTL, invite_id))
        return self._invite({**row, "created": now, "expires": now + INVITE_TTL}), token

    def revoke_invite(self, invite_id: str) -> None:
        with self.db.connect() as conn:
            if not conn.execute("DELETE FROM tokens WHERE id = ? AND kind = 'invite'", (invite_id,)).rowcount:
                raise AccountError("unknown invite", 404)

    def reset_token(self, user_id: str) -> tuple[str, str]:
        """(email, token) for a password reset link; older reset links of the user stop working."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.db.connect() as conn:
            row = conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise AccountError("unknown user", 404)
            conn.execute("DELETE FROM tokens WHERE kind = 'reset' AND user_id = ?", (user_id,))
            conn.execute("INSERT INTO tokens (id, hash, kind, email, user_id, created, expires) "
                         "VALUES (?, ?, 'reset', ?, ?, ?, ?)",
                         (uuid.uuid4().hex[:10], _hash(token), row["email"], user_id, now, now + RESET_TTL))
        return row["email"], token

    def _token_row(self, conn: Conn, token: str):
        if not token or len(token) > 100:
            return None
        row = conn.execute("SELECT * FROM tokens WHERE hash = ?", (_hash(token),)).fetchone()
        return row if row is not None and row["expires"] >= time.time() else None

    def peek_token(self, token: str) -> Optional[dict]:
        with self.db.connect() as conn:
            row = self._token_row(conn, token)
        return {"email": row["email"], "kind": row["kind"]} if row else None

    def use_token(self, token: str, password: str) -> tuple[dict, str]:
        """Set the password from an invite (creates the member) or reset link; returns (user, session token)."""
        with self.db.connect() as conn:
            row = self._token_row(conn, token)
            if row is None or not conn.execute("DELETE FROM tokens WHERE id = ?", (row["id"],)).rowcount:
                raise AccountError("this link is invalid or has expired", 404)
            if row["kind"] == "invite":
                if conn.execute("SELECT 1 FROM users WHERE email = ?", (row["email"],)).fetchone():
                    raise AccountError("this email already has an account", 409)
                user_id = uuid.uuid4().hex[:10]
                conn.execute("INSERT INTO users (id, email, role, password, created, last_login, features) "
                             "VALUES (?, ?, 'member', ?, ?, NULL, ?)",
                             (user_id, row["email"], hash_password(password), time.time(),
                              json.dumps(_features(row))))
            else:
                user_id = row["user_id"]
                if not conn.execute("UPDATE users SET password = ? WHERE id = ?",
                                    (hash_password(password), user_id)).rowcount:
                    raise AccountError("this link is invalid or has expired", 404)
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))  # sign out everywhere
            user = _user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
        return user, self.new_session(user_id)


# --- request helpers (FastAPI) -------------------------------------------------

def current_user(request: Request) -> dict:
    """The signed-in user (dict id/email/role) set by the access middleware; 401 if none."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "not signed in")
    return user


def require_admin(request: Request) -> dict:
    """Dependency for admin-only endpoints: 403 for members."""
    user = current_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "only the admin can do that")
    return user
