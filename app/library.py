"""Saved songs, playlists, play history and settings in one database.

SQLite by default (library/motif.db); PostgreSQL when MOTIF_DATABASE_URL is set
(see app/db.py — the SQL here runs unchanged on both).

Songs are stored as their seed + full SongSpec JSON (a couple of KB), never as
audio: rendering is deterministic, so audio is re-rendered (or fetched from S3
media) on demand. The spec is kept, not just the seed, because AI briefs and
future generator changes could otherwise not reproduce a song exactly.

A saved song is either liked (shows in Liked songs) or only kept because a
playlist uses it; unliking such a song keeps it for its playlists.

Older JSON files (index.json, playlists.json, history.json) are imported once.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from pydantic import ValidationError

from app import config, db as dbmod
from app.db import Conn, Database
from app.schema import SongSpec

log = logging.getLogger(__name__)

HISTORY_MAX = 200
NAME_MAX = 60
SONG_ID = re.compile(r"[0-9a-f]{16}")

TABLES = dbmod.REQUIRED_TABLES
ENTRY_FIELDS = ("id", "name", "genre", "tempo", "key", "scale", "bars", "music_seconds", "duration",
                "source", "seed", "liked_at", "liked")
LIKED_ID = "liked"  # virtual playlist id for Liked songs
HISTORY_FIELDS = ("id", "hash", "name", "genre", "tempo", "key", "scale", "music_seconds", "source", "played_at")


class LibraryError(ValueError):
    pass


def clean_name(name: str) -> str:
    name = " ".join(str(name).split())[:NAME_MAX]
    if not name:
        raise LibraryError("name can't be empty")
    return name


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\s.-]", "", name, flags=re.UNICODE).strip()
    return re.sub(r"\s+", " ", name)[:80] or "song"


def summary(spec: SongSpec) -> dict:
    return {
        "id": spec.audio_hash(), "name": spec.name, "genre": spec.genre, "tempo": spec.tempo,
        "key": spec.key, "scale": spec.scale, "bars": spec.bars,
        "music_seconds": spec.music_seconds, "duration": spec.duration_seconds, "seed": spec.seed,
    }


def _parse_spec(text: str | None) -> Optional[SongSpec]:
    if not text:
        return None
    try:
        return SongSpec.model_validate_json(text)
    except ValidationError:
        return None  # saved by an older version with a different spec format


class Library:
    def __init__(self, root: Path | None = None, db_path: Path | None = None, database: Database | None = None):
        """`database` wins; otherwise MOTIF_DATABASE_URL (for the default library) or SQLite at db_path."""
        self.root = Path(root or config.LIBRARY_DIR)
        self.root.mkdir(parents=True, exist_ok=True)
        # SQLite file: used as the database, or as the source of a one-time import into Postgres.
        self.db_path = Path(db_path or (self.root / "motif.db" if root else config.DB_PATH))
        self.lock = threading.RLock()
        legacy = self.db_path.with_name("music.db")  # the app was called "music" before it became Motif
        if self.db_path.name == "motif.db" and not self.db_path.exists() and legacy.exists():
            legacy.rename(self.db_path)
        if database is None:
            url = config.DATABASE_URL if root is None and db_path is None else None
            database = dbmod.from_url(url, self.db_path)
        self.db = database
        self.db.init_schema()
        if self.db.dialect != "sqlite":
            self._import_local_sqlite()
        self._import_json()

    @contextmanager
    def _db(self) -> Iterator[Conn]:
        with self.db.connect() as conn:
            yield conn

    def _import_local_sqlite(self) -> None:
        """First start on Postgres: bring over the existing local SQLite library once."""
        if not self.db_path.exists() or not self.db.is_empty():
            return
        local = dbmod.SQLiteDatabase(self.db_path)
        local.init_schema()
        if local.is_empty():
            return
        self.db.restore_from(self.db_path)
        self.set_setting("imported_from_sqlite", {"path": str(self.db_path), "at": time.time()})
        log.info("imported the local SQLite library %s into PostgreSQL", self.db_path)

    # --- saved songs ------------------------------------------------------

    def entries(self) -> list[dict]:
        """Liked songs, newest first."""
        with self._db() as db:
            rows = db.execute(f"SELECT {', '.join(ENTRY_FIELDS)} FROM songs WHERE liked = 1 "
                              "ORDER BY liked_at DESC").fetchall()
        return [dict(r) for r in rows]

    def get(self, song_id: str) -> Optional[dict]:
        with self._db() as db:
            row = db.execute(f"SELECT {', '.join(ENTRY_FIELDS)} FROM songs WHERE id = ?", (song_id,)).fetchone()
        return dict(row) if row else None

    def save(self, spec: SongSpec, source: str, liked: bool = False) -> tuple[dict, bool]:
        """Store a song (seed + spec). Returns (entry, already_there); liking an unliked song marks it."""
        entry = {**summary(spec), "source": source, "liked_at": time.time(), "liked": int(liked)}
        with self.lock:
            existing = self.get(entry["id"])
            if existing:
                if liked and not existing["liked"]:
                    with self._db() as db:
                        db.execute("UPDATE songs SET liked = 1, liked_at = ? WHERE id = ?",
                                   (entry["liked_at"], entry["id"]))
                    return self.get(entry["id"]), False
                return existing, True
            with self._db() as db:
                db.execute(
                    f"INSERT INTO songs ({', '.join(ENTRY_FIELDS)}, spec) "
                    f"VALUES ({', '.join('?' * (len(ENTRY_FIELDS) + 1))})",
                    (*(entry[f] for f in ENTRY_FIELDS), spec.model_dump_json()),
                )
        return entry, False

    def like(self, spec: SongSpec, source: str) -> tuple[dict, bool]:
        """Save and like a song. Returns (entry, already_liked)."""
        return self.save(spec, source, liked=True)

    def unlike(self, song_id: str) -> None:
        """Remove from Liked songs; the song is kept only while a playlist uses it."""
        with self.lock, self._db() as db:
            if not db.execute("SELECT 1 FROM songs WHERE id = ?", (song_id,)).fetchone():
                raise LibraryError("unknown song")
            if db.execute("SELECT 1 FROM playlist_songs WHERE song_id = ?", (song_id,)).fetchone():
                db.execute("UPDATE songs SET liked = 0 WHERE id = ?", (song_id,))
            else:
                db.execute("DELETE FROM songs WHERE id = ?", (song_id,))

    def spec(self, song_id: str) -> Optional[SongSpec]:
        if not SONG_ID.fullmatch(song_id):
            return None
        with self._db() as db:
            row = db.execute("SELECT spec FROM songs WHERE id = ?", (song_id,)).fetchone()
        return _parse_spec(row["spec"]) if row else None

    def rename_song(self, song_id: str, name: str) -> dict:
        """Rename a saved song (display name only; the audio is unchanged)."""
        name = clean_name(name)
        with self.lock, self._db() as db:
            if db.execute("UPDATE songs SET name = ? WHERE id = ?", (name, song_id)).rowcount == 0:
                raise LibraryError("unknown song")
        return self.get(song_id)

    def delete_song(self, song_hash: str) -> dict:
        """Forget a song completely: its seed + spec (and so every playlist entry) and its history.

        Returns what was removed: {"saved": bool, "history": rows}.
        """
        with self.lock, self._db() as db:
            saved = db.execute("DELETE FROM songs WHERE id = ?", (song_hash,)).rowcount > 0
            history = db.execute("DELETE FROM history WHERE hash = ?", (song_hash,)).rowcount
        return {"saved": saved, "history": history}

    # --- playlists --------------------------------------------------------

    def _playlist_songs(self, db: Conn, pl_id: str) -> list[dict]:
        rows = db.execute(
            f"SELECT {', '.join('s.' + f for f in ENTRY_FIELDS)} FROM playlist_songs ps "
            "JOIN songs s ON s.id = ps.song_id WHERE ps.playlist_id = ? ORDER BY ps.pos",
            (pl_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def _expand(self, db: Conn, row: dict, with_songs: bool) -> dict:
        songs = self._playlist_songs(db, row["id"])
        out = dict(row)
        out.update(count=len(songs), seconds=sum(s["music_seconds"] or 0 for s in songs),
                   genres=sorted({s["genre"] for s in songs}), song_ids=[s["id"] for s in songs])
        if with_songs:
            out["songs"] = songs
        return out

    def playlists(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM playlists ORDER BY created").fetchall()
            return [self._expand(db, r, with_songs=False) for r in rows]

    def playlist(self, pl_id: str) -> Optional[dict]:
        if pl_id == LIKED_ID:  # Liked songs plays like a playlist (newest first); it can't be edited as one
            songs = self.entries()
            return {"id": LIKED_ID, "name": "Liked songs", "source": None, "prompt": None, "songs": songs,
                    "song_ids": [s["id"] for s in songs], "count": len(songs),
                    "seconds": sum(s["music_seconds"] or 0 for s in songs),
                    "genres": sorted({s["genre"] for s in songs})}
        with self._db() as db:
            row = db.execute("SELECT * FROM playlists WHERE id = ?", (pl_id,)).fetchone()
            return self._expand(db, row, with_songs=True) if row else None

    def create_playlist(self, name: str, source: Optional[str] = None, prompt: Optional[str] = None) -> dict:
        """`source` marks how it was made ("ai" for a playlist the AI composed); `prompt` is what it was
        made from, so more songs can be generated in the same vibe later."""
        name = clean_name(name)
        pl_id, now = uuid.uuid4().hex[:10], time.time()
        with self.lock, self._db() as db:
            db.execute("INSERT INTO playlists (id, name, created, updated, source, prompt) VALUES (?, ?, ?, ?, ?, ?)",
                       (pl_id, name, now, now, source, prompt))
        return self.playlist(pl_id)

    def _touch(self, db: Conn, pl_id: str) -> None:
        if db.execute("UPDATE playlists SET updated = ? WHERE id = ?", (time.time(), pl_id)).rowcount == 0:
            raise LibraryError("unknown playlist")

    def rename_playlist(self, pl_id: str, name: str) -> dict:
        name = clean_name(name)
        with self.lock, self._db() as db:
            self._touch(db, pl_id)
            db.execute("UPDATE playlists SET name = ? WHERE id = ?", (name, pl_id))
        return self.playlist(pl_id)

    def delete_playlist(self, pl_id: str) -> None:
        with self.lock, self._db() as db:
            song_ids = [r[0] for r in db.execute("SELECT song_id FROM playlist_songs WHERE playlist_id = ?", (pl_id,))]
            if db.execute("DELETE FROM playlists WHERE id = ?", (pl_id,)).rowcount == 0:
                raise LibraryError("unknown playlist")
            for sid in song_ids:
                self._drop_orphan(db, sid)

    def add_to_playlist(self, pl_id: str, song_id: str) -> dict:
        """Append a saved song (no duplicates)."""
        with self.lock, self._db() as db:
            self._touch(db, pl_id)
            if not db.execute("SELECT 1 FROM songs WHERE id = ?", (song_id,)).fetchone():
                raise LibraryError("song is not saved")
            pos = db.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM playlist_songs WHERE playlist_id = ?",
                             (pl_id,)).fetchone()[0]
            db.execute("INSERT INTO playlist_songs (playlist_id, song_id, pos) VALUES (?, ?, ?) "
                       "ON CONFLICT DO NOTHING",
                       (pl_id, song_id, pos))
        return self.playlist(pl_id)

    def remove_from_playlist(self, pl_id: str, song_id: str) -> dict:
        with self.lock, self._db() as db:
            self._touch(db, pl_id)
            db.execute("DELETE FROM playlist_songs WHERE playlist_id = ? AND song_id = ?", (pl_id, song_id))
            self._drop_orphan(db, song_id)
        return self.playlist(pl_id)

    @staticmethod
    def _drop_orphan(db: Conn, song_id: str) -> None:
        """Forget a song that is neither liked nor in any playlist."""
        db.execute("DELETE FROM songs WHERE id = ? AND liked = 0 AND NOT EXISTS "
                   "(SELECT 1 FROM playlist_songs WHERE song_id = ?)", (song_id, song_id))

    def reorder_playlist(self, pl_id: str, song_ids: list[str]) -> dict:
        with self.lock, self._db() as db:
            self._touch(db, pl_id)
            current = [r[0] for r in db.execute("SELECT song_id FROM playlist_songs WHERE playlist_id = ?",
                                                (pl_id,))]
            if sorted(current) != sorted(song_ids) or len(set(song_ids)) != len(song_ids):
                raise LibraryError("new order must contain exactly the playlist's songs")
            db.executemany("UPDATE playlist_songs SET pos = ? WHERE playlist_id = ? AND song_id = ?",
                           [(i, pl_id, s) for i, s in enumerate(song_ids)])
        return self.playlist(pl_id)

    # --- history ----------------------------------------------------------

    def _insert_history(self, db: Conn, h: dict, spec_json: str | None) -> None:
        db.execute(
            "INSERT INTO history (id, hash, name, genre, tempo, key, scale, music_seconds, source, played_at, spec) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (h.get("id"), h.get("hash"), h.get("name"), h.get("genre"), h.get("tempo"), h.get("key"), h.get("scale"),
             h.get("music_seconds"), h.get("source"), h.get("played_at") or time.time(), spec_json),
        )

    def record_history(self, track_id: str, spec: SongSpec, source: str) -> None:
        h = {**summary(spec), "id": track_id, "hash": spec.audio_hash(), "source": source, "played_at": time.time()}
        with self.lock, self._db() as db:
            last = db.execute("SELECT hash FROM history ORDER BY n DESC LIMIT 1").fetchone()
            if last and last["hash"] == h["hash"]:
                return  # the same song again (re-cued on start, repeat one): one history entry is enough
            self._insert_history(db, h, spec.model_dump_json())
            db.execute("DELETE FROM history WHERE n NOT IN (SELECT n FROM history ORDER BY n DESC LIMIT ?)",
                       (HISTORY_MAX,))

    def history(self, limit: int = 50) -> list[dict]:
        with self._db() as db:
            rows = db.execute(f"SELECT {', '.join(HISTORY_FIELDS)} FROM history ORDER BY n DESC LIMIT ?",
                              (limit,)).fetchall()
        return [dict(r) for r in rows]

    def history_spec(self, track_id: str) -> Optional[tuple[SongSpec, str]]:
        with self._db() as db:
            row = db.execute("SELECT spec, source FROM history WHERE id = ? ORDER BY n DESC LIMIT 1",
                             (track_id,)).fetchone()
        spec = _parse_spec(row["spec"]) if row else None
        return (spec, row["source"] or "procedural") if spec else None

    # --- settings ---------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._db() as db:
            row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.lock, self._db() as db:
            db.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))

    # --- one-time import of the old JSON files ----------------------------

    def _import_json(self) -> None:
        def load(name: str):
            path = self.root / name
            if not path.exists():
                return None, path
            try:
                return json.loads(path.read_text()), path
            except ValueError:
                return None, path

        songs, songs_path = load("index.json")
        for e in songs or []:
            spec_file = self.root / "songs" / str(e.get("id")) / "spec.json"
            spec = _parse_spec(spec_file.read_text()) if spec_file.exists() else None
            if spec is not None:
                entry, _ = self.like(spec, e.get("source", "procedural"))
                if e.get("name") and e["name"] != spec.name:
                    self.rename_song(entry["id"], e["name"])
        pls, pls_path = load("playlists.json")
        for pl in pls or []:
            new = self.create_playlist(pl.get("name") or "Playlist")
            for sid in pl.get("songs", []):
                try:
                    self.add_to_playlist(new["id"], sid)
                except LibraryError:
                    pass
        hist, hist_path = load("history.json")
        with self.lock, self._db() as db:
            for h in reversed(hist or []):
                self._insert_history(db, h, json.dumps(h["spec"]) if h.get("spec") else None)
        for data, path in ((songs, songs_path), (pls, pls_path), (hist, hist_path)):
            if data is not None:
                path.rename(path.with_suffix(".imported.json"))
