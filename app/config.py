"""Paths and environment settings.

Nothing here is a secret except the admin and SMTP passwords; AI and S3 keys are
entered per user in the app and stored encrypted (app/credentials.py).
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("MOTIF_DATA_DIR", ROOT))
CACHE_DIR = Path(os.environ.get("MOTIF_CACHE_DIR", DATA_DIR / "cache"))
LIBRARY_DIR = Path(os.environ.get("MOTIF_LIBRARY_DIR", DATA_DIR / "library"))
DB_PATH = Path(os.environ.get("MOTIF_DB", LIBRARY_DIR / "motif.db"))
# PostgreSQL instead of SQLite, e.g. postgresql://motif:secret@db.example:5432/motif. Unset = SQLite at DB_PATH.
DATABASE_URL = os.environ.get("MOTIF_DATABASE_URL", "").strip() or None
SOUNDFONT_DIR = Path(os.environ.get("MOTIF_SOUNDFONT_DIR", ROOT / "soundfonts"))
WEB_DIR = ROOT / "web"

HOST = os.environ.get("MOTIF_HOST", "127.0.0.1")
PORT = int(os.environ.get("MOTIF_PORT", "8000"))
# Host names the browser may use to reach the app. Requests with any other Host header
# (DNS rebinding) or a foreign Origin on POST/PUT/PATCH/DELETE (cross-site requests) are refused.
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("MOTIF_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h.strip()]

# Accounts: with an admin email set the app needs a login (the admin is created/updated from these
# on every start); unset = local mode, one built-in user and no login.
ADMIN_EMAIL = os.environ.get("MOTIF_ADMIN_EMAIL", "").strip().lower() or None
ADMIN_PASSWORD = os.environ.get("MOTIF_ADMIN_PASSWORD", "") or None
PUBLIC_URL = os.environ.get("MOTIF_PUBLIC_URL", "").strip().rstrip("/") or None  # base for links in emails
SESSION_DAYS = float(os.environ.get("MOTIF_SESSION_DAYS") or "30")
RENDER_WORKERS = int(os.environ.get("MOTIF_RENDER_WORKERS") or "2")  # concurrent renders across all players

# Outgoing mail for invites and password resets. No host = links are shown to the admin instead.
SMTP_HOST = os.environ.get("MOTIF_SMTP_HOST", "").strip() or None
SMTP_PORT = int(os.environ.get("MOTIF_SMTP_PORT") or "587")
SMTP_USER = os.environ.get("MOTIF_SMTP_USER", "") or None
SMTP_PASSWORD = os.environ.get("MOTIF_SMTP_PASSWORD", "") or None
SMTP_FROM = os.environ.get("MOTIF_SMTP_FROM", "").strip() or None
SMTP_TLS = (os.environ.get("MOTIF_SMTP_TLS") or "starttls").strip().lower()  # starttls | ssl | none

SAMPLE_RATE = 44100
CACHE_MAX_SONGS = int(os.environ.get("MOTIF_CACHE_MAX", "12"))  # ~50 MB each (WAV + MP3)


# AI briefs and S3 backups have no environment settings: every user sets their own in the app
# (Settings -> AI, Settings -> Backups), with keys encrypted in the database (app/credentials.py).


def find_soundfont() -> Path:
    env = os.environ.get("MOTIF_SOUNDFONT")
    if env:
        return Path(env)
    fonts = sorted(SOUNDFONT_DIR.glob("*.sf2"))
    if not fonts:
        raise FileNotFoundError(f"no .sf2 soundfont found in {SOUNDFONT_DIR}")
    return fonts[0]
