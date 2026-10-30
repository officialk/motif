"""Database layer: SQLite (default) or PostgreSQL (when MOTIF_DATABASE_URL is set).

All SQL in the app is written once with `?` placeholders; the Postgres backend
translates them. Rows come back as `Row` (a dict that also supports index access).

Backups are always SQLite files: SQLite snapshots itself with the backup API,
Postgres exports its tables into a fresh SQLite file. Either kind restores into
either database, so moving between SQLite and Postgres is a backup + restore.
"""

from __future__ import annotations

import logging
import queue
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# Order matters: parents before children (inserts), reversed for deletes.
TABLES = ("songs", "playlists", "playlist_songs", "history", "settings", "secrets", "api_keys")
REQUIRED_TABLES = ("songs", "playlists", "playlist_songs", "history", "settings")

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS songs (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, genre TEXT NOT NULL, tempo {float}, key TEXT, scale TEXT,
        bars INTEGER, music_seconds {float}, duration {float}, source TEXT, seed BIGINT, spec TEXT NOT NULL,
        liked_at {float} NOT NULL, liked INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS playlists (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, created {float} NOT NULL, updated {float} NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS playlist_songs (
        playlist_id TEXT NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
        song_id TEXT NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
        pos INTEGER NOT NULL,
        PRIMARY KEY (playlist_id, song_id))""",
    """CREATE TABLE IF NOT EXISTS history (
        n {autoid}, id TEXT NOT NULL, hash TEXT, name TEXT, genre TEXT, tempo {float},
        key TEXT, scale TEXT, music_seconds {float}, source TEXT, played_at {float} NOT NULL, spec TEXT)""",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, value {blob} NOT NULL)",
    """CREATE TABLE IF NOT EXISTS api_keys (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, hash TEXT NOT NULL UNIQUE, scopes TEXT NOT NULL,
        prefix TEXT NOT NULL, created {float} NOT NULL, last_used {float})""",
)
_TYPES = {
    "sqlite": {"float": "REAL", "autoid": "INTEGER PRIMARY KEY AUTOINCREMENT", "blob": "BLOB"},
    "postgres": {"float": "DOUBLE PRECISION", "autoid": "BIGSERIAL PRIMARY KEY", "blob": "BYTEA"},
}
# Columns added after the first release: (table, column, definition).
_ADDED_COLUMNS = (("songs", "liked", "INTEGER NOT NULL DEFAULT 1"), ("history", "hash", "TEXT"))


def schema(dialect: str) -> list[str]:
    return [s.format(**_TYPES[dialect]) for s in _SCHEMA]


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
        return sql.replace("?", "%s") if self.dialect == "postgres" else sql

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

    def columns(self, db: Conn, table: str) -> set[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def is_empty(self) -> bool:
        with self.connect() as db:
            return not any(db.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
                           for t in ("songs", "playlists", "history"))

    # --- backups: always a SQLite file --------------------------------------

    def snapshot_to(self, dest: Path) -> None:
        """Write a consistent SQLite copy of every table to `dest`."""
        out = sqlite3.connect(dest)
        try:
            for stmt in schema("sqlite"):
                out.execute(stmt)
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

    def snapshot_to(self, dest: Path) -> None:
        src = sqlite3.connect(self.path)
        out = sqlite3.connect(dest)
        try:
            src.backup(out)  # consistent even while other connections write
        finally:
            out.close()
            src.close()

    def restore_from(self, src: Path) -> None:
        source = sqlite3.connect(src)
        target = sqlite3.connect(self.path)
        try:
            source.backup(target)  # replaces the whole file's content in place
        finally:
            target.close()
            source.close()
        self.init_schema()  # an older backup may predate newer tables/columns


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
