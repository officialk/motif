"""Paths and environment settings.

Secrets (API keys, AWS credentials) are only ever read from the environment by
the client libraries that need them; nothing here stores or logs them.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("MUSIC_DATA_DIR", ROOT))
CACHE_DIR = Path(os.environ.get("MUSIC_CACHE_DIR", DATA_DIR / "cache"))
LIBRARY_DIR = Path(os.environ.get("MUSIC_LIBRARY_DIR", DATA_DIR / "library"))
DB_PATH = Path(os.environ.get("MUSIC_DB", LIBRARY_DIR / "music.db"))
SOUNDFONT_DIR = Path(os.environ.get("MUSIC_SOUNDFONT_DIR", ROOT / "soundfonts"))
WEB_DIR = ROOT / "web"

HOST = os.environ.get("MUSIC_HOST", "127.0.0.1")
PORT = int(os.environ.get("MUSIC_PORT", "8000"))
# Host names the browser may use to reach the app. Requests with any other Host header
# (DNS rebinding) or a foreign Origin on POST/PUT/PATCH/DELETE (cross-site requests) are refused.
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("MUSIC_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if h.strip()]

SAMPLE_RATE = 44100
CACHE_MAX_SONGS = int(os.environ.get("MUSIC_CACHE_MAX", "12"))  # ~50 MB each (WAV + MP3)


def _backend_default() -> str:
    # CLAUDE_BACKEND=cli|api is still accepted for older setups.
    legacy = {"cli": "claude-cli", "api": "claude-api"}.get(os.environ.get("CLAUDE_BACKEND", "").strip().lower())
    return os.environ.get("AI_BACKEND", legacy or "off").strip().lower()


# AI brief generation. Backends: off | claude-cli | claude-api | openai (any OpenAI-compatible server).
AI_BACKEND = _backend_default()
AI_RATIO = float(os.environ.get("AI_RATIO", os.environ.get("CLAUDE_RATIO", "0.25")))
AI_TIMEOUT = float(os.environ.get("AI_TIMEOUT", os.environ.get("CLAUDE_TIMEOUT", "20")))
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")
CLAUDE_CLI = os.environ.get("CLAUDE_CLI", "claude")
CLAUDE_CLI_MODEL = os.environ.get("CLAUDE_CLI_MODEL", "haiku")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_JSON_MODE = os.environ.get("OPENAI_JSON_MODE", "1") != "0"

# S3-compatible storage (AWS S3, MinIO, Cloudflare R2, ...). Empty bucket = local only.
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_PREFIX = os.environ.get("S3_PREFIX", "music/")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL") or None
S3_REGION = os.environ.get("S3_REGION") or None
S3_MEDIA = os.environ.get("S3_MEDIA", "1") != "0"  # store saved/exported songs' audio in S3
S3_BACKUP_INTERVAL_HOURS = float(os.environ.get("S3_BACKUP_INTERVAL_HOURS", "0"))  # 0 = manual only


def find_soundfont() -> Path:
    env = os.environ.get("MUSIC_SOUNDFONT")
    if env:
        return Path(env)
    fonts = sorted(SOUNDFONT_DIR.glob("*.sf2"))
    if not fonts:
        raise FileNotFoundError(f"no .sf2 soundfont found in {SOUNDFONT_DIR}")
    return fonts[0]
