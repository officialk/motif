"""Per-user runtime: each user's library view, AI config, backups and Player.

Every user has their own Player (queue, speaker, live clients), created on first
use and cued like a fresh start (Settings -> App -> on start). All players share
one engine: audio is cached once by hash, renders queue on one global limit
(MOTIF_RENDER_WORKERS) and a cache prune never drops what another player needs.

AI and S3 backups are configured per user in the app (nothing from the
environment). A user's AI calls only use that user's keys; the claude CLI backend
(the server's own login) is for the env admin / local user only. A member whose
`ai` feature is off is procedural only; with `backup` off their schedule stops.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from app import accounts as acct, storage
from app.credentials import Credentials, UserCredentials
from app.engine.base import Engine
from app.generator import ai
from app.library import Library
from app.player import Player

log = logging.getLogger("motif.users")


class Users:
    def __init__(self, lib: Library, creds: Credentials, accts: acct.Accounts,
                 cue: Callable[[Player, Library], None] = lambda p, lib: None):
        self.lib_base = lib
        self.creds = creds
        self.accts = accts
        self.engine: Optional[Engine] = None  # attached on start (building it needs the soundfont)
        self.cue = cue
        self._players: dict[str, Player] = {}
        self._schedulers: dict[str, storage.BackupScheduler] = {}
        self._lock = threading.RLock()
        accts.on_deleted.append(self.drop)

    def attach(self, engine: Engine) -> None:
        """The one engine every player renders with."""
        self.engine = engine
        engine.in_use = self.hashes_in_use

    # --- who --------------------------------------------------------------

    def user(self, uid: str) -> Optional[dict]:
        """Fresh user record (features may have changed), or None if the user is gone."""
        return self.accts.get(uid) if self.accts.login else dict(acct.LOCAL_USER)

    def lib(self, uid: str) -> Library:
        return self.lib_base.for_user(uid)

    def keys(self, uid: str) -> UserCredentials:
        return self.creds.for_user(uid)

    # --- AI -----------------------------------------------------------------

    def ai_settings(self, uid: str) -> ai.AISettings:
        """The user's saved AI settings (AI off until they set it). Ignores the `ai` feature."""
        saved = self.lib(uid).user_setting("ai")
        if not saved:
            return ai.AISettings()
        try:
            s = ai.AISettings(**{k: v for k, v in saved.items() if k in ai.AISettings.__dataclass_fields__})
        except (TypeError, ValueError):
            log.warning("ignoring invalid saved AI settings")
            return ai.AISettings()
        if s.backend == "claude-cli" and not self.accts.is_primary(uid):
            return ai.AISettings()  # the CLI runs as the server's own login
        return s

    def ai(self, uid: str, user: Optional[dict] = None) -> tuple[ai.AISettings, UserCredentials]:
        """(settings, keys) to make songs with for this user: AI off when their `ai` feature is off."""
        user = user if user is not None else self.user(uid)
        if user is None or not acct.has_feature(user, "ai"):
            return ai.AISettings(), self.keys(uid)
        return self.ai_settings(uid), self.keys(uid)

    def ratio(self, uid: str) -> float:
        s, _ = self.ai(uid)
        return s.ratio if s.backend != "off" else 0.0

    def make_spec(self, uid: str):
        def maker(genre, mood, use_ai):
            s, keys = self.ai(uid)
            return ai.make_spec(genre, mood, use_ai, s, keys)
        return maker

    # --- players ------------------------------------------------------------

    def player(self, uid: str) -> Player:
        with self._lock:
            p = self._players.get(uid)
            if p is None:
                lib = self.lib(uid)
                p = Player(self.engine, self.make_spec(uid), claude_ratio=self.ratio(uid),
                           on_history=lambda t: lib.record_history(t.id, t.spec, t.source), library=lib)
                self._players[uid] = p
                self.cue(p, lib)
            return p

    def adopt(self, uid: str, player: Player) -> None:
        """Use a ready-made player (tests) for a user; it plays from that user's library."""
        lib = self.lib(uid)
        if player.library is not lib:
            player.library = lib
            player.on_history = lambda t: lib.record_history(t.id, t.spec, t.source)
        with self._lock:
            self._players[uid] = player

    def existing(self, uid: str) -> Optional[Player]:
        with self._lock:
            return self._players.get(uid)

    def all(self) -> list[Player]:
        with self._lock:
            return list(self._players.values())

    def hashes_in_use(self) -> set[str]:
        out: set[str] = set()
        for p in self.all():
            out |= p.hashes()
        return out

    def access_changed(self, uid: str) -> None:
        """Features changed: AI share, backups and open streams follow at once."""
        p = self.existing(uid)
        if p is not None:
            p.claude_ratio = self.ratio(uid)
            p.access_changed()
        self.schedule(uid)

    def drop(self, uid: str) -> None:
        """The user was removed: end their streams, stop their player and schedule."""
        with self._lock:
            p = self._players.pop(uid, None)
            sched = self._schedulers.pop(uid, None)
        if sched is not None:
            sched.stop()
        if p is not None:
            p.close()

    # --- backups --------------------------------------------------------------

    def backup_settings(self, uid: str) -> storage.BackupSettings:
        return storage.BackupSettings.load(self.lib(uid).user_setting("backup"))

    def backups(self, uid: str) -> Optional[storage.BackupStore]:
        """The user's backup store, or None when they haven't set a bucket."""
        return storage.from_settings(self.backup_settings(uid), self.keys(uid))

    def schedule(self, uid: str) -> None:
        """(Re)start or stop the user's scheduled backups from their settings and features."""
        with self._lock:
            old = self._schedulers.pop(uid, None)
            if old is not None:
                old.stop()
            user = self.user(uid)
            settings = self.backup_settings(uid)
            if user is None or not acct.has_feature(user, "backup") or not settings.enabled \
                    or settings.backup_interval_hours <= 0:
                return
            try:
                store = self.backups(uid)
            except Exception as e:  # e.g. keys missing or boto3 can't build a client
                log.warning("scheduled backups for %s not started: %s", uid, e)
                return
            sched = storage.BackupScheduler(store, self.lib_base.db, uid, settings.backup_interval_hours)
            self._schedulers[uid] = sched
            sched.start()

    def schedule_all(self) -> None:
        for uid in set(self.lib_base.users_with_setting("backup")):
            self.schedule(uid)

    def shutdown(self) -> None:
        with self._lock:
            players, self._players = list(self._players.values()), {}
            scheds, self._schedulers = list(self._schedulers.values()), {}
        for s in scheds:
            s.stop()
        for p in players:
            p.shutdown()
