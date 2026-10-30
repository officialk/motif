"""Server-side radio state: now playing, prefetched next, history, play mode.

Modes: one genre (a "radio"), shuffle (random radio each song), or a playlist
(prompts create one; songs are re-rendered from their saved spec, in order or
shuffled). Repeat: off | all (playlists wrap) | one (the same song again).

The browser does the actual playback; when a song's last bar ends it starts
the prefetched one and tells the server (POST /skip with the id it was playing) and the server
promotes the prefetched track and starts preparing the one after.
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.engine.base import Engine
from app.library import Library
from app.schema import GENRES, SongSpec

log = logging.getLogger("motif.player")

# (genre, mood, prefer_claude) -> (spec, source)
# (genre or None = let the AI choose, mood, use_ai) -> (spec, source)
SpecMaker = Callable[[Optional[str], Optional[str], bool], tuple[SongSpec, str]]
REPEAT_MODES = ("off", "all", "one")


@dataclass
class Track:
    genre: Optional[str]
    mood: Optional[str] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: str = "generating"  # generating | rendering | ready | error
    spec: Optional[SongSpec] = None
    source: str = "procedural"
    error: Optional[str] = None
    created: float = field(default_factory=time.time)
    library_id: Optional[str] = None  # set when the audio comes from the library
    display_name: Optional[str] = None  # library name (songs can be renamed)
    playlist_pos: Optional[int] = None  # position in the playing playlist's order
    leaves_playlist: bool = False  # first radio song after a playlist ended (repeat off)
    ready_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict:
        s = self.spec
        d = {
            "id": self.id, "status": self.status, "genre": self.genre, "mood": self.mood,
            "source": self.source, "error": self.error, "created": self.created,
            "library_id": self.library_id, "playlist_pos": self.playlist_pos,
        }
        if s is not None:
            d.update(
                name=self.display_name or s.name, seed=s.seed, tempo=s.tempo, key=s.key, scale=s.scale, bars=s.bars,
                music_seconds=s.music_seconds, tail_seconds=s.tail_seconds, duration=s.duration_seconds,
                sections=s.section_times(), hash=s.audio_hash(),
                instruments=s.instruments.model_dump(), keys_style=s.keys_style,
            )
        if self.status == "ready":
            d.update(mp3=f"/audio/{self.id}.mp3")
        return d


@dataclass
class Mode:
    genre: Optional[str] = None
    shuffle: bool = False
    mood: Optional[str] = None
    repeat: str = "off"
    playlist_id: Optional[str] = None
    playlist_name: Optional[str] = None
    playlist_shuffle: bool = False
    order: list[str] = field(default_factory=list)  # song ids in play order

    def leave_playlist(self) -> None:
        self.playlist_id, self.playlist_name, self.playlist_shuffle, self.order = None, None, False, []

    def public(self, now: "Track | None") -> dict:
        d = {"genre": self.genre, "shuffle": self.shuffle, "mood": self.mood, "repeat": self.repeat,
             "playlist": None}
        if self.playlist_id:
            d["playlist"] = {
                "id": self.playlist_id, "name": self.playlist_name, "shuffle": self.playlist_shuffle,
                "count": len(self.order), "position": now.playlist_pos if now else None,
            }
        return d


class Player:
    def __init__(
        self,
        engine: Engine,
        spec_maker: SpecMaker,
        claude_ratio: float = 0.0,
        on_history: Callable[[Track], None] | None = None,
        workers: int = 2,
        library: Library | None = None,
    ):
        self.engine = engine
        self.library = library
        self.spec_maker = spec_maker
        self.claude_ratio = claude_ratio
        self.on_history = on_history
        self.lock = threading.RLock()
        # Live sync: every change bumps `version` and wakes the SSE streams waiting on `changed`.
        self.changed = threading.Condition(self.lock)
        self.version = 0
        self.speaker: Optional[dict] = None  # {client, kind, since}: the one client that plays audio
        self.playback: Optional[dict] = None  # last report from the speaker
        self.command: Optional[dict] = None  # last remote command for the speaker
        self.clients: dict[str, dict] = {}  # connected pages/overlays: client -> {kind, name, audio, since, streams}
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="render")
        self.tracks: OrderedDict[str, Track] = OrderedDict()
        self.now_playing: Optional[Track] = None
        self.up_next: Optional[Track] = None
        self.history: deque[Track] = deque(maxlen=100)
        self.mode = Mode()
        self._rng = random.Random()

    # --- commands -------------------------------------------------------

    def play_genre(self, genre: str, mood: str | None = None) -> None:
        if genre not in GENRES:
            raise ValueError(f"unknown genre {genre!r}")
        with self.lock:
            self.mode.leave_playlist()
            self.mode.genre, self.mode.shuffle, self.mode.mood = genre, False, mood
            self._replace_now(self._new_track(genre, mood, prefer_claude=bool(mood)))

    def play_random(self, mood: str | None = None) -> None:
        with self.lock:
            self.mode.leave_playlist()
            self.mode.shuffle, self.mode.mood = True, mood
            genre = self._pick_shuffle_genre()
            self.mode.genre = genre
            self._replace_now(self._new_track(genre, mood, prefer_claude=bool(mood)))

    def play_spec(self, spec: SongSpec, source: str) -> Track:
        """Play a known spec (e.g. from history); the radio then continues in its genre."""
        with self.lock:
            track = Track(genre=spec.genre, spec=spec, source=source, status="rendering")
            self._register(track)
            self.pool.submit(self._render_job, track)
            self.mode.leave_playlist()
            self.mode.genre, self.mode.shuffle = spec.genre, False
            self._replace_now(track)
            return track

    def play_saved(self, song_id: str) -> Track:
        """Play one saved song from the library; the radio then continues in its genre."""
        with self.lock:
            track = self._library_track(song_id)
            if track is None:
                raise LookupError("unknown or unplayable song")
            self.mode.leave_playlist()
            self.mode.genre, self.mode.shuffle = track.genre, False
            self._replace_now(track)
            return track

    def play_playlist(self, pl_id: str, start: str | None = None, shuffle: bool = False) -> Track:
        if self.library is None:
            raise LookupError("no library")
        pl = self.library.playlist(pl_id)
        if pl is None:
            raise LookupError("unknown playlist")
        order = list(pl["song_ids"])
        if not order:
            raise ValueError("playlist is empty")
        if shuffle:
            self._rng.shuffle(order)
        if start is not None:
            if start not in order:
                raise LookupError("song is not in this playlist")
            if shuffle:  # start with the chosen song, shuffle the rest
                order.remove(start)
                order.insert(0, start)
        with self.lock:
            self.mode.playlist_id, self.mode.playlist_name = pl["id"], pl["name"]
            self.mode.playlist_shuffle, self.mode.order = shuffle, order
            self.mode.shuffle, self.mode.mood = False, None
            track = self._playlist_track(order.index(start) if start else 0)
            if track is None:
                self.mode.leave_playlist()
                raise ValueError("no playable songs in this playlist")
            self.mode.genre = track.genre
            self._replace_now(track)
            return track

    def set_repeat(self, mode: str) -> None:
        if mode not in REPEAT_MODES:
            raise ValueError(f"repeat must be one of {', '.join(REPEAT_MODES)}")
        with self.lock:
            self.mode.repeat = mode
            # Re-plan the prefetched song so the change applies to what plays next.
            if self.now_playing is not None:
                self.up_next = self._make_next()
            self._changed()

    def skip(self, from_id: str | None = None) -> bool:
        """Advance to the prefetched track. With from_id, only if that is still playing (no double skips)."""
        with self.lock:
            if from_id and (self.now_playing is None or self.now_playing.id != from_id):
                return False
            if self.now_playing is None and self.mode.genre is None:
                return False
            nxt = self.up_next
            if nxt is None or nxt.status == "error":
                nxt = self._make_next()
            self._replace_now(nxt)
            return True

    def forget(self, spec_hash: str) -> bool:
        """Drop a deleted song from this session's history. True if it is playing or queued."""
        with self.lock:
            kept = [t for t in self.history if not (t.spec and t.spec.audio_hash() == spec_hash)]
            self.history.clear()
            self.history.extend(kept)
            self._changed()
            return any(t and t.spec and t.spec.audio_hash() == spec_hash for t in (self.now_playing, self.up_next))

    # --- state ------------------------------------------------------------

    def get(self, track_id: str) -> Track | None:
        with self.lock:
            return self.tracks.get(track_id)

    def state(self) -> dict:
        with self.lock:
            now = self.now_playing
            return {
                "now": now.public() if now else None,
                "up_next": self.up_next.public() if self.up_next else None,
                "history": [t.public() for t in reversed(self.history)],
                "mode": self.mode.public(now),
            }

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    # --- live sync (SSE) ----------------------------------------------------

    def _changed(self) -> None:
        """Call with the lock held after any change clients should see."""
        self.version += 1
        self.changed.notify_all()

    def wait_change(self, version: int, timeout: float) -> int:
        """Block until the version differs from `version` (or timeout); returns the current version."""
        with self.changed:
            if self.version == version:
                self.changed.wait(timeout)
            return self.version

    def live(self) -> dict:
        """Everything a client needs to render and play, as one versioned snapshot."""
        with self.lock:
            clients = [{k: v for k, v in c.items() if k != "streams"} | {"client": cid}
                       for cid, c in self.clients.items()]
            return {"version": self.version, "state": self.state(), "speaker": self.speaker,
                    "playback": self.playback, "command": self.command, "clients": clients,
                    "server_time": time.time()}

    def connect_client(self, client: str, kind: str, name: Optional[str], audio: bool) -> None:
        """A page or overlay opened its event stream (a client may briefly hold two while reconnecting)."""
        with self.lock:
            c = self.clients.get(client)
            if c is None:
                self.clients[client] = {"kind": kind, "name": name, "audio": audio, "since": time.time(), "streams": 1}
            else:
                c["streams"] += 1
            self._changed()

    def disconnect_client(self, client: str) -> None:
        with self.lock:
            c = self.clients.get(client)
            if c is None:
                return
            c["streams"] -= 1
            if c["streams"] <= 0:
                del self.clients[client]
                if self.speaker and self.speaker["client"] == client:
                    self.speaker, self.playback = None, None  # a gone client can't play audio
                self._changed()

    def assign_speaker(self, client: str) -> dict:
        """Move the audio to a connected client that can play (e.g. from the site's "Play on" menu)."""
        with self.lock:
            c = self.clients.get(client)
            if c is None:
                raise LookupError("that player is not connected")
            if not c["audio"]:
                raise ValueError("that overlay is display-only (audio=0)")
            if not self.speaker or self.speaker["client"] != client:
                self.speaker = {"client": client, "kind": c["kind"], "since": time.time()}
                self.playback = None
                self._changed()
            return self.speaker

    def claim_speaker(self, client: str, kind: str, force: bool = False) -> Optional[dict]:
        """Overlays always take over; a site tab only when nobody plays (or with force: "Play here")."""
        with self.lock:
            current = self.speaker
            if current is None or current["client"] == client or kind == "overlay" or force:
                if current is None or current["client"] != client:
                    self.speaker = {"client": client, "kind": kind, "since": time.time()}
                    self.playback = None
                    self._changed()
            return self.speaker

    def report_playback(self, client: str, track_id: Optional[str], state: str, position: float,
                        volume: float) -> bool:
        with self.lock:
            if not self.speaker or self.speaker["client"] != client:
                return False
            self.playback = {"client": client, "track_id": track_id, "state": state,
                             "position": max(0.0, float(position)), "at": time.time(),
                             "volume": min(max(float(volume), 0.0), 1.0)}
            self._changed()
            return True

    def send_command(self, action: str, t: Optional[float] = None, volume: Optional[float] = None) -> dict:
        with self.lock:
            if self.speaker is None:
                raise LookupError("nothing is playing audio right now")
            seq = (self.command or {}).get("seq", 0) + 1
            self.command = {"seq": seq, "action": action, "at": time.time()}
            if t is not None:
                self.command["t"] = max(0.0, float(t))
            if volume is not None:
                self.command["volume"] = min(max(float(volume), 0.0), 1.0)
            self._changed()
            return self.command

    # --- internals --------------------------------------------------------

    def _replace_now(self, track: Track) -> None:
        old = self.now_playing
        if old is not None and old.status == "ready":
            self.history.append(old)
            if self.on_history:
                try:
                    self.on_history(old)
                except Exception:
                    log.exception("history hook failed")
        self.now_playing = track
        if track.leaves_playlist:
            self.mode.leave_playlist()
        # Prefetch: as soon as a song is chosen, start on the next one.
        self.up_next = self._make_next()
        self._prune()
        self._changed()

    def _make_next(self) -> Track:
        now = self.now_playing
        if self.mode.repeat == "one" and now is not None and now.spec is not None:
            return self._again(now)
        if self.mode.playlist_id:
            pos = now.playlist_pos if now and now.playlist_pos is not None else -1
            if pos + 1 < len(self.mode.order) or self.mode.repeat == "all":
                track = self._playlist_track(pos + 1)
                if track is not None:
                    return track
            # End of the playlist (repeat off): the radio carries on; the mode switches when it starts.
            track = self._new_track(self._next_genre(), None)
            track.leaves_playlist = True
            return track
        return self._new_track(self._next_genre(), self.mode.mood)

    def _again(self, track: Track) -> Track:
        """The same song once more (repeat one); audio comes from the cache."""
        if track.library_id:
            again = self._library_track(track.library_id)
            if again is not None:
                again.playlist_pos = track.playlist_pos
                return again
        again = Track(genre=track.genre, mood=track.mood, spec=track.spec, source=track.source,
                      status="rendering", library_id=track.library_id, display_name=track.display_name,
                      playlist_pos=track.playlist_pos)
        self._register(again)
        self.pool.submit(self._render_job, again)
        return again

    def _playlist_track(self, pos: int) -> Track | None:
        """Track for order[pos], wrapping around and skipping songs that were removed."""
        order = self.mode.order
        for i in range(len(order)):
            p = (pos + i) % len(order)
            track = self._library_track(order[p])
            if track is not None:
                track.playlist_pos = p
                return track
        return None

    def _library_track(self, song_id: str) -> Track | None:
        """A saved song: rebuilt from its stored spec (audio from cache, S3 media or a fresh render)."""
        if self.library is None:
            return None
        spec, entry = self.library.spec(song_id), self.library.get(song_id)
        if spec is None or entry is None:
            return None
        track = Track(genre=spec.genre, spec=spec, source=entry.get("source", "procedural"), status="rendering",
                      library_id=song_id, display_name=entry.get("name"))
        self._register(track)
        self.pool.submit(self._render_job, track)
        return track

    def _next_genre(self) -> str:
        if self.mode.shuffle or self.mode.genre is None:
            return self._pick_shuffle_genre()
        return self.mode.genre

    def _pick_shuffle_genre(self) -> str:
        current = self.now_playing.genre if self.now_playing else None
        choices = [g for g in GENRES if g != current]
        return self._rng.choice(choices)

    def _new_track(self, genre: str | None, mood: str | None, prefer_claude: bool = False) -> Track:
        track = Track(genre=genre, mood=mood)
        self._register(track)
        use_claude = prefer_claude or (self.claude_ratio > 0 and self._rng.random() < self.claude_ratio)
        self.pool.submit(self._job, track, use_claude)
        return track

    def _register(self, track: Track) -> None:
        self.tracks[track.id] = track
        while len(self.tracks) > 300:
            self.tracks.popitem(last=False)

    def _job(self, track: Track, use_claude: bool) -> None:
        try:
            spec, source = self.spec_maker(track.genre, track.mood, use_claude)
            with self.lock:
                track.spec, track.source, track.status = spec, source, "rendering"
                track.genre = spec.genre
                self._changed()
        except Exception as e:  # spec_maker already falls back; this is a last resort
            log.exception("spec generation failed")
            with self.lock:
                track.status, track.error = "error", f"generation failed: {e}"
                self._changed()
            track.ready_event.set()
            return
        self._render_job(track)

    def _render_job(self, track: Track) -> None:
        try:
            t0 = time.time()
            self.engine.ensure_mp3(track.spec)
            log.info("rendered %s %r (%s) in %.2fs", track.genre, track.spec.name, track.source, time.time() - t0)
            with self.lock:
                track.status = "ready"
                self._changed()
        except Exception as e:
            log.exception("render failed")
            with self.lock:
                track.status, track.error = "error", f"render failed: {e}"
                self._changed()
        finally:
            track.ready_event.set()

    def _prune(self) -> None:
        keep = {t.spec.audio_hash() for t in (self.now_playing, self.up_next, *self.history) if t and t.spec}
        try:
            self.engine.prune(keep)
        except OSError:
            log.exception("cache prune failed")
