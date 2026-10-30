"""Engine interface. An engine turns a SongSpec into a WAV (+ MP3) of the whole song.

Audio lookup order: local cache -> render. The cache is shared by every user
(files are named by spec hash). Playback only needs the MP3; the WAV is rendered
when someone downloads it. At most `render_workers` renders run at once across
all players (MOTIF_RENDER_WORKERS); others wait for a slot.
"""

from __future__ import annotations

import subprocess
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from app import config
from app.schema import SongSpec


class Engine(ABC):
    name = "base"

    def __init__(self, cache_dir: Path | None = None, render_workers: int | None = None):
        self.cache_dir = Path(cache_dir or config.CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._slots = threading.BoundedSemaphore(max(1, render_workers or config.RENDER_WORKERS))
        # Hashes any player still needs (set by the app when several players share this engine).
        self.in_use: Callable[[], set[str]] = set

    def wav_path(self, spec: SongSpec) -> Path:
        return self.cache_dir / f"{self.name}-{spec.audio_hash()}.wav"

    def mp3_path(self, spec: SongSpec) -> Path:
        return self.wav_path(spec).with_suffix(".mp3")

    def is_cached(self, spec: SongSpec) -> bool:
        return self.wav_path(spec).exists() and self.mp3_path(spec).exists()

    def _lock(self, spec: SongSpec) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(spec.audio_hash(), threading.Lock())

    def render(self, spec: SongSpec) -> Path:
        """Render (or take from cache) WAV + MP3. Returns the WAV path; the MP3 sits next to it."""
        with self._lock(spec):  # two threads asking for the same song render it once
            if self.is_cached(spec):
                for p in (self.wav_path(spec), self.mp3_path(spec)):
                    p.touch()
                return self.wav_path(spec)
            with self._slots:
                wav = self.render_wav(spec)
                encode_mp3(wav, self.mp3_path(spec))
            return wav

    def ensure_mp3(self, spec: SongSpec) -> Path:
        """MP3 for playback: cache, else a full render."""
        mp3 = self.mp3_path(spec)
        with self._lock(spec):
            if mp3.exists():
                mp3.touch()
                return mp3
        self.render(spec)
        return mp3

    @abstractmethod
    def render_wav(self, spec: SongSpec) -> Path:
        """Render exactly spec.duration_seconds (music + ring-out) to self.wav_path(spec)."""

    def forget(self, spec_hash: str) -> None:
        """Remove a song's cached audio."""
        for ext in ("mp3", "wav"):
            (self.cache_dir / f"{self.name}-{spec_hash}.{ext}").unlink(missing_ok=True)

    def prune(self, keep: set[str], max_songs: int = config.CACHE_MAX_SONGS) -> None:
        """Drop least-recently-used renders beyond max_songs, never touching hashes in `keep`."""
        try:
            keep = keep | self.in_use()
        except Exception:  # never let a prune fail over this
            pass
        mp3s = sorted(self.cache_dir.glob(f"{self.name}-*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)
        for mp3 in mp3s[max_songs:]:
            h = mp3.stem.split("-", 1)[1]
            if h in keep or ".tmp" in mp3.name:
                continue
            mp3.unlink(missing_ok=True)
            mp3.with_suffix(".wav").unlink(missing_ok=True)


def encode_mp3(wav: Path, mp3: Path) -> Path:
    tmp = mp3.with_suffix(".tmp.mp3")
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(wav), "-c:a", "libmp3lame", "-qscale:a", "2", str(tmp)],
        check=True, capture_output=True, timeout=120,
    )
    tmp.replace(mp3)
    return mp3
