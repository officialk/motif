"""Database layer: SQLite (default) or PostgreSQL (when MOTIF_DATABASE_URL is set).

All SQL in the app is written once with `?` placeholders; the Postgres backend
translates them. Rows come back as `Row` (a dict that also supports index access).

Backups are SQLite files. A user's backup (`snapshot_user` / `restore_user`) holds
only that user's rows and restores into either database. Whole-database copies
(`snapshot_to` / `restore_from`) move a library between SQLite and Postgres.
"""

from __future__ import annotations

import logging
import queue
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# Order matters: parents before children (inserts), reversed for deletes.
TABLES = ("users", "songs", "playlists", "playlist_songs", "history", "settings", "secrets", "api_keys",
          "sessions", "tokens")
REQUIRED_TABLES = ("songs", "playlists", "playlist_songs", "history", "settings")

# Rows owned by a user carry `user_id` ('local' = the local-mode user, see app/accounts.py).
# A song id is its spec hash, so the same song can be in several users' libraries: songs are
# keyed by (user_id, id) and playlist entries point at the song of the playlist's owner.
_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS songs (
        id TEXT NOT NULL, name TEXT NOT NULL, genre TEXT NOT NULL, tempo {float}, key TEXT, scale TEXT,
        bars INTEGER, music_seconds {float}, duration {float}, source TEXT, seed BIGINT, spec TEXT NOT NULL,
        liked_at {float} NOT NULL, liked INTEGER NOT NULL DEFAULT 1, user_id TEXT NOT NULL DEFAULT 'local',
        PRIMARY KEY (user_id, id))""",
    """CREATE TABLE IF NOT EXISTS playlists (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, created {float} NOT NULL, updated {float} NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS playlist_songs (
        playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
        song_id TEXT NOT NULL,
        pos INTEGER NOT NULL,
        user_id TEXT NOT NULL DEFAULT 'local',
        PRIMARY KEY (playlist_id, song_id),
        FOREIGN KEY (user_id, song_id) REFERENCES songs(user_id, id) ON DELETE CASCADE ON UPDATE CASCADE)""",
    """CREATE TABLE IF NOT EXISTS history (
        n {autoid}, id TEXT NOT NULL, hash TEXT, name TEXT, genre TEXT, tempo {float},
        key TEXT, scale TEXT, music_seconds {float}, source TEXT, played_at {float} NOT NULL, spec TEXT)""",
    # settings/secrets: user_id '' = server-wide rows (e.g. accounts_owner), else that user's own.
    """CREATE TABLE IF NOT EXISTS settings (
        user_id TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (user_id, key))""",
    """CREATE TABLE IF NOT EXISTS secrets (
        user_id TEXT NOT NULL DEFAULT '', name TEXT NOT NULL, value {blob} NOT NULL, PRIMARY KEY (user_id, name))""",
    """CREATE TABLE IF NOT EXISTS api_keys (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, hash TEXT NOT NULL UNIQUE, scopes TEXT NOT NULL,
        prefix TEXT NOT NULL, created {float} NOT NULL, last_used {float})""",
    # Accounts (app/accounts.py). Passwords are scrypt strings; sessions and tokens only by SHA-256.
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, role TEXT NOT NULL, password TEXT,
        created {float} NOT NULL, last_login {float}, features TEXT)""",
    """CREATE TABLE IF NOT EXISTS sessions (
        hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created {float} NOT NULL, expires {float} NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS tokens (
        id TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, email TEXT NOT NULL,
        user_id TEXT REFERENCES users(id) ON DELETE CASCADE, created {float} NOT NULL, expires {float} NOT NULL,
        features TEXT)""",
)
_TYPES = {
    "sqlite": {"float": "REAL", "autoid": "INTEGER PRIMARY KEY AUTOINCREMENT", "blob": "BLOB"},
    "postgres": {"float": "DOUBLE PRECISION", "autoid": "BIGSERIAL PRIMARY KEY", "blob": "BYTEA"},
}
# Columns added after the first release: (table, column, definition).
_ADDED_COLUMNS = (("songs", "liked", "INTEGER NOT NULL DEFAULT 1"), ("history", "hash", "TEXT"),
                  ("playlists", "source", "TEXT"), ("playlists", "prompt", "TEXT"),
                  ("api_keys", "kind", "TEXT NOT NULL DEFAULT 'api'"), ("users", "features", "TEXT"),
                  ("playlists", "user_id", "TEXT NOT NULL DEFAULT 'local'"),
                  ("history", "user_id", "TEXT NOT NULL DEFAULT 'local'"),
                  ("api_keys", "user_id", "TEXT NOT NULL DEFAULT 'local'"), ("tokens", "features", "TEXT"))
# songs, playlist_songs, settings and secrets got user_id as part of their keys: older databases are
# rebuilt once (`_rekey`). Per-user settings rows (and S3 settings, which used to be server-wide) move to
# the local user; other settings rows stay server-wide.
USER_SETTINGS = ("app", "ai", "backup")
OWNED = ("songs", "playlists", "playlist_songs", "history", "settings", "secrets", "api_keys")
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS history_user ON history (user_id, n)",
    "CREATE INDEX IF NOT EXISTS playlists_user ON playlists (user_id)",
    "CREATE INDEX IF NOT EXISTS playlist_songs_song ON playlist_songs (user_id, song_id)",
    "CREATE INDEX IF NOT EXISTS api_keys_user ON api_keys (user_id)",
)


def schema(dialect: str) -> list[str]:
    return [s.format(**_TYPES[dialect]) for s in _SCHEMA]


def _table_sql(dialect: str, table: str, new_name: str) -> str:
    """The CREATE statement of one table under another name (for rebuilds)."""
    stmt = next(s for s in schema(dialect) if s.startswith(f"CREATE TABLE IF NOT EXISTS {table} ("))
    return stmt.replace(f"CREATE TABLE IF NOT EXISTS {table} (", f"CREATE TABLE {new_name} (", 1)


def _sqlite_file(dest: Path) -> sqlite3.Connection:
    """A new SQLite file with the current schema (backups)."""
    out = sqlite3.connect(dest)
    for stmt in schema("sqlite"):
        out.execute(stmt)
    for table, column, definition in _ADDED_COLUMNS:
        if column not in {r[1] for r in out.execute(f"PRAGMA table_info({table})")}:
            out.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return out


class DatabaseError(RuntimeError):
    pass


class Row(dict):
    """dict row that also allows row[0] like sqlite3.Row."""

    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return super().__getitem__(k)


class Cursor:
    def __init__(self, cur):
        self._cur = cur

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def _wrap(self, row) -> Optional[Row]:
        if row is None:
            return None
        cols = [d[0] for d in self._cur.description]
        return Row(zip(cols, row))

    def fetchone(self) -> Optional[Row]:
        return self._wrap(self._cur.fetchone())

    def fetchall(self) -> list[Row]:
        return [self._wrap(r) for r in self._cur.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class Conn:
    """Thin wrapper so callers use the same `?` SQL on both backends."""

    def __init__(self, raw, dialect: str):
        self.raw = raw
        self.dialect = dialect

    def _sql(self, sql: str) -> str:
        # psycopg treats % as a placeholder marker, so literal % (LIKE patterns) must be doubled.
        return sql.replace("%", "%%").replace("?", "%s") if self.dialect == "postgres" else sql

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Cursor:
        cur = self.raw.cursor()
        cur.execute(self._sql(sql), tuple(params))
        return Cursor(cur)

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        cur = self.raw.cursor()
        cur.executemany(self._sql(sql), [tuple(p) for p in seq])


class Database:
    dialect = "base"

    @contextmanager
    def connect(self) -> Iterator[Conn]:  # pragma: no cover - interface
        raise NotImplementedError
        yield

    def describe(self) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def init_schema(self) -> None:
        with self.connect() as db:
            for stmt in schema(self.dialect):
                db.execute(stmt)
            for table, column, definition in _ADDED_COLUMNS:
                if column not in self.columns(db, table):
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            rekey = {t for t in ("songs", "playlist_songs", "settings", "secrets")
                     if "user_id" not in self.columns(db, t)}
        if rekey:
            if "playlist_songs" in rekey:
                rekey.add("songs")  # rebuilt together (playlist entries point at songs by owner + id)
            self._rekey(rekey)
            log.info("%s are now kept per user", ", ".join(sorted(rekey)))
        with self.connect() as db:
            for stmt in _INDEXES:
                db.execute(stmt)

    def _rekey(self, tables: set[str]) -> None:  # pragma: no cover - interface
        """Add user_id to the keys of `tables` (songs + playlist_songs, settings, secrets), keeping every row."""
        raise NotImplementedError

    def columns(self, db: Conn, table: str) -> set[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def is_empty(self) -> bool:
        with self.connect() as db:
            return not any(db.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
                           for t in ("songs", "playlists", "history"))

    # --- backups: always a SQLite file --------------------------------------

    def snapshot_to(self, dest: Path) -> None:
        """Write a consistent SQLite copy of every table to `dest`."""
        out = _sqlite_file(dest)
        try:
            with self.connect() as db:
                for table in TABLES:
                    rows = db.execute(f"SELECT * FROM {table}").fetchall()
                    if rows:
                        cols = list(rows[0].keys())
                        out.executemany(
                            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                            [tuple(r.values()) for r in rows],
                        )
            out.commit()
        finally:
            out.close()

    def restore_from(self, src: Path) -> None:
        """Replace all tables with the contents of a SQLite snapshot (one transaction).

        Tables the snapshot doesn't have (e.g. secrets in older backups) are left alone.
        """
        source = sqlite3.connect(src)
        source.row_factory = sqlite3.Row
        try:
            have = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            tables = [t for t in TABLES if t in have]
            with self.connect() as db:
                for table in reversed(tables):
                    db.execute(f"DELETE FROM {table}")
                for table in tables:
                    rows = source.execute(f"SELECT * FROM {table}").fetchall()
                    if not rows:
                        continue
                    target_cols = self.columns(db, table)
                    cols = [c for c in rows[0].keys() if c in target_cols]
                    db.executemany(
                        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [tuple(r[c] for c in cols) for r in rows],
                    )
                self._after_restore(db)
        finally:
            source.close()

    def _after_restore(self, db: Conn) -> None:
        pass

    # --- one user's data: per-user backups ------------------------------------

    def snapshot_user(self, dest: Path, user_id: str) -> None:
        """Write one user's rows (library, playlists, history, settings, encrypted keys, API keys) to a
        SQLite file with the full schema. Accounts, sessions and other users are never included."""
        out = _sqlite_file(dest)
        try:
            with self.connect() as db:
                for table in OWNED:
                    rows = db.execute(f"SELECT * FROM {table} WHERE user_id = ?", (user_id,)).fetchall()
                    if rows:
                        cols = list(rows[0].keys())
                        out.executemany(
                            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                            [tuple(r.values()) for r in rows],
                        )
            out.commit()
        finally:
            out.close()

    def restore_user(self, src: Path, user_id: str, keep_secrets: Sequence[str] = ()) -> dict:
        """Replace one user's rows with those in a snapshot (one transaction). Returns counts.

        Works with per-user snapshots and with whole-database backups of older versions (no user_id
        columns: every row is taken). Other users' rows are never touched. The user's backup settings
        and the secrets named in `keep_secrets` (their S3 keys) stay as they are, so the user keeps
        access to the bucket they restored from. Playlist ids already taken by someone else get new ids;
        API keys whose token is already in use elsewhere are skipped.
        """
        source = sqlite3.connect(src)
        source.row_factory = sqlite3.Row
        try:
            have = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

            def rows(table: str, order: str = "") -> list[dict]:
                if table not in have:
                    return []
                return [dict(r) for r in source.execute(f"SELECT * FROM {table} {order}")]

            counts = {}
            with self.connect() as db:
                for table in ("playlists", "songs", "history", "api_keys"):  # playlist_songs cascade
                    db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
                db.execute("DELETE FROM settings WHERE user_id = ? AND key <> 'backup'", (user_id,))
                keep = list(keep_secrets) or [""]
                db.execute(f"DELETE FROM secrets WHERE user_id = ? AND name NOT IN ({', '.join('?' * len(keep))})",
                           (user_id, *keep))

                def insert(table: str, row: dict, conflict: bool = False) -> bool:
                    target = self.columns(db, table)
                    row = {k: v for k, v in {**row, "user_id": user_id}.items() if k in target}
                    sql = (f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})"
                           + (" ON CONFLICT DO NOTHING" if conflict else ""))
                    return db.execute(sql, tuple(row.values())).rowcount > 0

                counts["songs"] = sum(insert("songs", r, conflict=True) for r in rows("songs"))
                ids: dict[str, str] = {}
                for r in rows("playlists"):
                    new = r["id"]
                    if db.execute("SELECT 1 FROM playlists WHERE id = ?", (new,)).fetchone():
                        new = uuid.uuid4().hex[:10]
                    ids[r["id"]] = new
                    insert("playlists", {**r, "id": new})
                counts["playlists"] = len(ids)
                for r in rows("playlist_songs"):
                    if r["playlist_id"] in ids and db.execute(
                            "SELECT 1 FROM songs WHERE user_id = ? AND id = ?", (user_id, r["song_id"])).fetchone():
                        insert("playlist_songs", {**r, "playlist_id": ids[r["playlist_id"]]}, conflict=True)
                history = rows("history", "ORDER BY n")
                for r in history:
                    r.pop("n", None)  # new ids in this database, same order
                    insert("history", r)
                counts["history"] = len(history)
                for r in rows("settings"):
                    if r["key"] in ("app", "ai"):  # older whole-database backups also hold server rows
                        insert("settings", r, conflict=True)
                for r in rows("secrets"):
                    if r["name"] not in keep_secrets:
                        insert("secrets", r, conflict=True)
                counts["api_keys"] = sum(
                    insert("api_keys", r, conflict=True) for r in rows("api_keys")
                    if not db.execute("SELECT 1 FROM api_keys WHERE hash = ?", (r["hash"],)).fetchone())
        finally:
            source.close()
        return counts


class SQLiteDatabase(Database):
    dialect = "sqlite"

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[Conn]:
        """One short-lived connection per operation; works across threads and with restores."""
        raw = sqlite3.connect(self.path, timeout=10)
        raw.execute("PRAGMA foreign_keys = ON")
        try:
            with raw:
                yield Conn(raw, self.dialect)
        finally:
            raw.close()

    def columns(self, db: Conn, table: str) -> set[str]:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}

    def describe(self) -> dict:
        return {"kind": "sqlite", "target": str(self.path)}

    def _rekey(self, tables: set[str]) -> None:
        # SQLite can't change a primary key in place: build new tables, copy, swap (one transaction,
        # foreign keys off meanwhile as the SQLite docs prescribe, checked before commit).
        raw = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute("BEGIN IMMEDIATE")
            try:
                db = Conn(raw, self.dialect)
                if "songs" in tables:
                    old = self.columns(db, "songs")
                    raw.execute(_table_sql("sqlite", "songs", "songs_new"))
                    cols = ", ".join(c for c in self.columns(db, "songs_new") if c in old)
                    raw.execute(f"INSERT INTO songs_new ({cols}) SELECT {cols} FROM songs")
                    owner = "ps.user_id" if "user_id" in self.columns(db, "playlist_songs") else "p.user_id"
                    raw.execute(_table_sql("sqlite", "playlist_songs", "playlist_songs_new"))
                    raw.execute("INSERT INTO playlist_songs_new (playlist_id, song_id, pos, user_id) "
                                f"SELECT ps.playlist_id, ps.song_id, ps.pos, COALESCE({owner}, 'local') "
                                "FROM playlist_songs ps LEFT JOIN playlists p ON p.id = ps.playlist_id")
                    raw.execute("DROP TABLE playlist_songs")
                    raw.execute("DROP TABLE songs")
                    raw.execute("ALTER TABLE songs_new RENAME TO songs")
                    raw.execute("ALTER TABLE playlist_songs_new RENAME TO playlist_songs")
                if "settings" in tables:
                    raw.execute(_table_sql("sqlite", "settings", "settings_new"))
                    raw.execute("INSERT INTO settings_new (user_id, key, value) SELECT "
                                f"CASE WHEN key IN ('app', 'ai', 'storage') THEN 'local' ELSE '' END, "
                                "CASE WHEN key = 'storage' THEN 'backup' ELSE key END, value FROM settings")
                    raw.execute("DROP TABLE settings")
                    raw.execute("ALTER TABLE settings_new RENAME TO settings")
                if "secrets" in tables:
                    raw.execute(_table_sql("sqlite", "secrets", "secrets_new"))
                    raw.execute("INSERT INTO secrets_new (user_id, name, value) SELECT 'local', name, value FROM secrets")
                    raw.execute("DROP TABLE secrets")
                    raw.execute("ALTER TABLE secrets_new RENAME TO secrets")
                if raw.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise DatabaseError("per-user migration left broken playlist entries")
                raw.execute("COMMIT")
            except BaseException:
                raw.execute("ROLLBACK")
                raise
        finally:
            raw.close()

class PostgresDatabase(Database):
    dialect = "postgres"
    POOL_SIZE = 4

    def __init__(self, url: str, connect_timeout: float = 10):
        try:
            import psycopg  # noqa: F401 - imported lazily: only needed for Postgres
        except ImportError as e:
            raise DatabaseError("MOTIF_DATABASE_URL is set but psycopg is not installed") from e
        if urlsplit(url).scheme not in ("postgres", "postgresql"):
            raise DatabaseError("MOTIF_DATABASE_URL must start with postgresql://")
        self.url = url
        self.connect_timeout = connect_timeout
        self._pool: "queue.LifoQueue" = queue.LifoQueue(self.POOL_SIZE)
        self._lock = threading.Lock()

    def _new(self):
        import psycopg

        try:
            # autocommit + transaction() below: every `with db.connect()` is exactly one BEGIN..COMMIT
            return psycopg.connect(self.url, connect_timeout=int(self.connect_timeout), autocommit=True)
        except psycopg.Error as e:
            # psycopg messages can echo connection details; keep only the class name + host.
            raise DatabaseError(f"cannot connect to PostgreSQL at {self.describe()['target']}: "
                                f"{type(e).__name__}") from None

    @contextmanager
    def connect(self) -> Iterator[Conn]:
        try:
            raw = self._pool.get_nowait()
            if raw.closed or raw.broken:
                raw = self._new()
        except queue.Empty:
            raw = self._new()
        try:
            with raw.transaction():  # commits on success, rolls back on any exception
                yield Conn(raw, self.dialect)
        finally:
            if not raw.closed and not raw.broken:
                try:
                    self._pool.put_nowait(raw)
                except queue.Full:
                    raw.close()

    def columns(self, db: Conn, table: str) -> set[str]:
        rows = db.execute("SELECT column_name FROM information_schema.columns "
                          "WHERE table_schema = current_schema() AND table_name = ?", (table,))
        return {r["column_name"] for r in rows}

    def _rekey(self, tables: set[str]) -> None:
        with self.connect() as db:  # one transaction: DDL is transactional in PostgreSQL
            if "songs" in tables:
                db.execute("ALTER TABLE songs ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT 'local'")
                db.execute("ALTER TABLE playlist_songs ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT 'local'")
                db.execute("UPDATE playlist_songs ps SET user_id = p.user_id FROM playlists p "
                           "WHERE p.id = ps.playlist_id")
                db.execute("ALTER TABLE playlist_songs DROP CONSTRAINT IF EXISTS playlist_songs_song_id_fkey")
                db.execute("ALTER TABLE songs DROP CONSTRAINT IF EXISTS songs_pkey")
                db.execute("ALTER TABLE songs ADD PRIMARY KEY (user_id, id)")
                db.execute("ALTER TABLE playlist_songs ADD CONSTRAINT playlist_songs_user_id_song_id_fkey "
                           "FOREIGN KEY (user_id, song_id) REFERENCES songs(user_id, id) "
                           "ON DELETE CASCADE ON UPDATE CASCADE")
            if "settings" in tables:
                db.execute("ALTER TABLE settings ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT ''")
                db.execute("UPDATE settings SET user_id = 'local' WHERE key IN ('app', 'ai', 'storage')")
                db.execute("UPDATE settings SET key = 'backup' WHERE key = 'storage'")
                db.execute("ALTER TABLE settings DROP CONSTRAINT IF EXISTS settings_pkey")
                db.execute("ALTER TABLE settings ADD PRIMARY KEY (user_id, key)")
            if "secrets" in tables:
                db.execute("ALTER TABLE secrets ADD COLUMN IF NOT EXISTS user_id TEXT NOT NULL DEFAULT 'local'")
                db.execute("ALTER TABLE secrets ALTER COLUMN user_id SET DEFAULT ''")
                db.execute("ALTER TABLE secrets DROP CONSTRAINT IF EXISTS secrets_pkey")
                db.execute("ALTER TABLE secrets ADD PRIMARY KEY (user_id, name)")

    def describe(self) -> dict:
        """Where the data lives, without user or password."""
        u = urlsplit(self.url)
        host = u.hostname or "localhost"
        port = f":{u.port}" if u.port else ""
        return {"kind": "postgres", "target": f"{host}{port}{u.path or ''}"}

    def _after_restore(self, db: Conn) -> None:
        # Keep the history id sequence ahead of the restored rows.
        db.execute("SELECT setval(pg_get_serial_sequence('history', 'n'), "
                   "COALESCE((SELECT MAX(n) FROM history), 0) + 1, false)")

    def close(self) -> None:
        while True:
            try:
                self._pool.get_nowait().close()
            except queue.Empty:
                break


def redact(url: str) -> str:
    return re.sub(r"//[^@/]*@", "//***@", url)


def from_url(url: Optional[str], sqlite_path: Path) -> Database:
    """Postgres when a URL is configured (it must be reachable), else SQLite at sqlite_path."""
    if not url:
        return SQLiteDatabase(sqlite_path)
    db = PostgresDatabase(url)
    with db.connect() as c:  # fail fast: never fall back silently and split the data
        c.execute("SELECT 1")
    log.info("using PostgreSQL at %s", db.describe()["target"])
    return db
