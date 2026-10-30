"""Server-side radio state: now playing, prefetched next, history, play mode.

Modes: one genre (a "radio"), shuffle (random radio each song), or a playlist
(prompts create one; songs are re-rendered from their saved spec). A playlist is
the queue: in order or shuffled, it stops at the end unless repeat is "all".
Shuffle+ turns a playlist into a radio: each next song is either one of its songs
or, at the "AI share of radio" rate, a new one made from its prompt (played, not saved).
Repeat: off | all (playlists wrap) | one (the same song again); Shuffle+ ignores it.

The browser does the actual playback; when a song's last bar ends it starts
the prefetched one and tells the server (POST /skip with the id it was playing) and the server
promotes the prefetched track and starts preparing the one after.

Each user has their own Player (see app/users.py). A player nobody has listened to
for IDLE_AFTER seconds (no connected page or overlay, and no playback report, skip,
control call or audio fetch, see Player.activity) parks new prefetch work
(spec generation and renders) until a client connects again; the song that is
"now playing" is always prepared.
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
IDLE_AFTER = 600.0  # seconds without a connected client before prefetching pauses


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
    playlist_source: Optional[str] = None  # "ai" for playlists the AI composed
    playlist_shuffle: bool = False
    playlist_plus: bool = False  # Shuffle+: a radio built on the playlist and its prompt
    playlist_prompt: Optional[str] = None
    order: list[str] = field(default_factory=list)  # song ids in play order (the queue)
    bag: list[str] = field(default_factory=list)  # Shuffle+: playlist songs not yet played this round

    def leave_playlist(self) -> None:
        self.playlist_id, self.playlist_name, self.playlist_shuffle, self.order = None, None, False, []
        self.playlist_source, self.playlist_plus, self.playlist_prompt, self.bag = None, False, None, []

    def public(self, now: "Track | None") -> dict:
        d = {"genre": self.genre, "shuffle": self.shuffle, "mood": self.mood, "repeat": self.repeat,
             "playlist": None}
        if self.playlist_id:
            d["playlist"] = {
                "id": self.playlist_id, "name": self.playlist_name, "source": self.playlist_source,
                "shuffle": self.playlist_shuffle, "plus": self.playlist_plus,
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
        self.library_rev = 0  # bumped when songs/playlists change, so pages reload their library
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
        self.stopped = False  # a playlist ended: its first song is cued, paused, until play is pressed
        self._rng = random.Random()
        self.idle_since: Optional[float] = time.monotonic()  # no client connected since (None = one is)
        self._parked: dict[str, tuple] = {}  # track id -> (job, args) held back while idle
        self.access_rev = 0  # bumped when the owner's account changes: open streams re-check access
        self.closed = False  # the owner was removed: streams end

    # --- commands -------------------------------------------------------

    def play_genre(self, genre: str, mood: str | None = None) -> None:
        if genre not in GENRES:
            raise ValueError(f"unknown genre {genre!r}")
        with self.lock:
            self.mode.leave_playlist()
            self.mode.genre, self.mode.shuffle, self.mode.mood = genre, False, mood
            self.stopped = False
            self._replace_now(self._new_track(genre, mood, prefer_claude=bool(mood)))

    def play_random(self, mood: str | None = None) -> None:
        with self.lock:
            self.mode.leave_playlist()
            self.mode.shuffle, self.mode.mood = True, mood
            self.stopped = False
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
            self.stopped = False
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
            self.stopped = False
            self._replace_now(track)
            return track

    def play_playlist(self, pl_id: str, start: str | None = None, shuffle: bool = False,
                      plus: bool = False) -> Track:
        """Play a playlist as the queue (in order, or shuffled). With plus: Shuffle+ (a radio on it)."""
        if self.library is None:
            raise LookupError("no library")
        pl = self.library.playlist(pl_id)
        if pl is None:
            raise LookupError("unknown playlist")
        ids = list(pl["song_ids"])
        if not ids:
            raise ValueError("playlist is empty")
        if start is not None and start not in ids:
            raise LookupError("song is not in this playlist")
        with self.lock:
            self.mode.playlist_id, self.mode.playlist_name = pl["id"], pl["name"]
            self.mode.playlist_source, self.mode.playlist_prompt = pl.get("source"), pl.get("prompt")
            self.mode.shuffle, self.mode.mood = False, None
            self.stopped = False
            self._arrange(ids, "auto" if plus else "on" if shuffle else "off", start)
            if plus:
                track = self._bag_track(first=start)
            else:
                track = self._playlist_track(self.mode.order.index(start) if start else 0)
            if track is None:
                self.mode.leave_playlist()
                raise ValueError("no playable songs in this playlist")
            self.mode.genre = track.genre
            self._replace_now(track)
            return track

    def _arrange(self, ids: list[str], shuffle: str, current: Optional[str]) -> None:
        """Set the queue for a shuffle mode; `current` (a song id) is the one playing or about to."""
        self.mode.playlist_shuffle, self.mode.playlist_plus = shuffle != "off", shuffle == "auto"
        rest = [i for i in ids if i != current]
        if shuffle == "off":
            self.mode.order = ids
        else:
            self._rng.shuffle(rest)
            self.mode.order = ([current] if current in ids else []) + rest
        self.mode.bag = list(rest) if shuffle == "auto" else []

    def set_playlist_shuffle(self, pl_id: str, mode: str) -> None:
        """Change the playing playlist's shuffle in place: "off", "on", or "auto" (Shuffle+).

        The current song keeps playing; only what comes after it changes.
        """
        if mode not in ("off", "on", "auto"):
            raise ValueError("shuffle must be off, on or auto")
        with self.lock:
            if self.mode.playlist_id != pl_id or self.library is None:
                raise LookupError("this playlist is not playing")
            pl = self.library.playlist(pl_id)
            if pl is None:
                raise LookupError("unknown playlist")
            now = self.now_playing
            current = now.library_id if now is not None else None
            self._arrange(list(pl["song_ids"]), mode, current)
            if now is not None:
                now.playlist_pos = self.mode.order.index(current) if current in self.mode.order else None
                self.up_next = self._make_next()
            self._changed()

    def set_repeat(self, mode: str) -> None:
        if mode not in REPEAT_MODES:
            raise ValueError(f"repeat must be one of {', '.join(REPEAT_MODES)}")
        with self.lock:
            self.mode.repeat = mode
            # Re-plan the prefetched song so the change applies to what plays next.
            if self.now_playing is not None:
                self.up_next = self._make_next()
            self._changed()

    def library_touched(self) -> None:
        with self.lock:
            self.library_rev += 1
            self._changed()

    def playlist_grew(self, pl_id: str, song_ids: list[str]) -> None:
        """Songs were appended to a playlist: pages reload it, and if it is playing they join the queue."""
        with self.lock:
            self.library_rev += 1
            if self.mode.playlist_id == pl_id:
                new = [i for i in song_ids if i not in self.mode.order]
                self.mode.order.extend(new)
                if self.mode.playlist_plus:
                    self.mode.bag.extend(new)
                elif self.now_playing is not None and self.up_next is None:
                    self.up_next = self._make_next()  # it was about to end: the new songs play instead
            self._changed()

    def skip(self, from_id: str | None = None, manual: bool = False) -> bool:
        """Advance to the prefetched track. With from_id, only if that is still playing (no double skips).

        At the end of a playlist (repeat off) it stops: the first song is cued, paused.
        """
        with self.lock:
            if from_id and (self.now_playing is None or self.now_playing.id != from_id):
                return False
            if self.now_playing is None and self.mode.genre is None:
                return False
            nxt = self.up_next
            pending = None
            if (manual and self.mode.playlist_plus and nxt is not None and nxt.library_id is None
                    and nxt.status != "ready"):
                # Shuffle+ "next" while a new song is still being made: play one of the playlist's
                # songs now and keep the new one for after it.
                pending, nxt = nxt, self._bag_track()
            if nxt is None or nxt.status == "error":
                nxt = self._make_next()
            if nxt is None:
                self._stop_at_start()
                return True
            self.stopped = False
            self._replace_now(nxt)
            if pending is not None:
                self.up_next = pending
                self._changed()
            return True

    def previous(self) -> bool:
        """Back one song: the previous one in the queue, or (radio, Shuffle+) the last one played.

        The page restarts the current song itself when it is more than a few seconds in.
        Returns False when there is nothing before it.
        """
        with self.lock:
            now = self.now_playing
            if now is None:
                return False
            if self.mode.playlist_id and not self.mode.playlist_plus:
                pos = now.playlist_pos if now.playlist_pos is not None else 0
                if pos == 0 and self.mode.repeat != "all":
                    return False
                track = self._playlist_track((pos - 1) % len(self.mode.order))
                if track is None:
                    return False
                self.stopped = False
                self._replace_now(track, record=False)
                return True
            prev = next((t for t in reversed(self.history) if t.spec is not None), None)
            if prev is None:
                return False
            self.history.remove(prev)
            self.stopped = False
            self._replace_now(self._again(prev), record=False)
            if now.spec is not None:  # forward again returns to the song we left (if it was composed)
                self.up_next = self._again(now)
                self._changed()
            return True

    def jump(self, pos: int) -> bool:
        """Play the queue from `pos` (a click on a song in the queue)."""
        with self.lock:
            if not self.mode.playlist_id or self.mode.playlist_plus or not 0 <= pos < len(self.mode.order):
                return False
            track = self._playlist_track(pos)
            if track is None:
                return False
            self.stopped = False
            self._replace_now(track)
            return True

    def queue(self) -> dict:
        """The play list as one list with the current song at `current`.

        A playlist (shuffle off/on) lists its whole queue in play order (`pos` per song, `repeats`
        when it loops). A radio or Shuffle+ lists the songs played this session, the current one
        and the next.
        """
        with self.lock:
            now = self.now_playing
            radio = not (self.mode.playlist_id and not self.mode.playlist_plus)
            out = {"items": [], "current": None, "radio": radio,
                   "repeats": not radio and self.mode.repeat == "all"}
            if radio:
                items = [t.public() for t in list(self.history)[-50:]]
                if now is not None:
                    out["current"] = len(items)
                    items.append(now.public())
                if self.up_next is not None:
                    items.append(self.up_next.public())
                out["items"] = items
                return out
            order = list(self.mode.order)
            cur = now.playlist_pos if now is not None else None
        entries = {i: self.library.get(i) for i in set(order)} if self.library is not None else {}
        out["items"] = [{**entries[i], "pos": p} for p, i in enumerate(order) if entries.get(i)]
        out["current"] = next((n for n, e in enumerate(out["items"]) if e["pos"] == cur), None)
        return out

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
                # a playlist about to end (no next song), and one that ended (first song cued, paused)
                "at_end": self.up_next is None and bool(self.mode.playlist_id), "stopped": self.stopped,
                "mode": self.mode.public(now),
            }

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        """The owner is gone: end every open stream and stop working."""
        with self.lock:
            self.closed = True
            self._parked.clear()
            self._changed()
        self.shutdown()

    def access_changed(self) -> None:
        """The owner's features changed: streams re-check what they may still do."""
        with self.lock:
            self.access_rev += 1
            self._changed()

    def uses(self, spec_hash: str) -> bool:
        """Is this song playing or queued here?"""
        with self.lock:
            return any(t and t.spec and t.spec.audio_hash() == spec_hash for t in (self.now_playing, self.up_next))

    def hashes(self) -> set[str]:
        """Audio this player still needs, read without the lock (another player's prune calls it)."""
        try:
            tracks = (self.now_playing, self.up_next, *list(self.history))
        except RuntimeError:  # history changed meanwhile; the next prune catches up
            return set()
        return {t.spec.audio_hash() for t in tracks if t is not None and t.spec is not None}

    # --- idle: no client for a while -> no prefetching ------------------------

    def idle(self) -> bool:
        return self.idle_since is not None and time.monotonic() - self.idle_since > IDLE_AFTER

    def activity(self) -> None:
        """The user is listening without an open stream (a locked phone keeps playing, reports,
        skips and fetches audio): restart the idle clock and prepare what was held back."""
        with self.lock:
            if self.closed or self.idle_since is None:
                return  # a client is connected: never idle
            self.idle_since = time.monotonic()
            if self._parked:
                self._resume()

    def _park(self, track: Track, job, *args) -> bool:
        """Hold back work for a track that isn't playing while nobody listens. True if parked."""
        with self.lock:
            if self.closed or (track is not self.now_playing and self.idle()):
                if not self.closed:
                    self._parked[track.id] = (job, args)
                return True
            return False

    def _resume(self, only: Optional[Track] = None) -> None:
        """Start parked work again: one track (it became "now"), or what still matters (a client came)."""
        if only is not None:
            parked = self._parked.pop(only.id, None)
            if parked:
                self.pool.submit(parked[0], only, *parked[1])
            return
        parked, self._parked = self._parked, {}
        for t in (self.now_playing, self.up_next):
            if t is not None and t.id in parked:
                job, args = parked[t.id]
                self.pool.submit(job, t, *args)

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
                    "library_rev": self.library_rev,
                    "server_time": time.time()}

    def connect_client(self, client: str, kind: str, name: Optional[str], audio: bool) -> None:
        """A page or overlay opened its event stream (a client may briefly hold two while reconnecting)."""
        with self.lock:
            self.idle_since = None
            self._resume()
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
                if not self.clients:
                    self.idle_since = time.monotonic()
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
            if self.stopped and state == "playing" and self.now_playing is not None \
                    and track_id == self.now_playing.id:
                self.stopped = False  # play was pressed after the playlist ended
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

    def _replace_now(self, track: Track, record: bool = True) -> None:
        old = self.now_playing
        if record and old is not None and old.status == "ready":
            self.history.append(old)
            if self.on_history:
                try:
                    self.on_history(old)
                except Exception:
                    log.exception("history hook failed")
        self.now_playing = track
        self._resume(track)  # parked as a prefetch while idle: it plays now, so prepare it
        # Prefetch: as soon as a song is chosen, start on the next one.
        self.up_next = self._make_next()
        self._prune()
        self._changed()

    def _stop_at_start(self) -> None:
        """The playlist ended (repeat off): cue its first song, paused, like a finished album."""
        track = self._playlist_track(0)
        if track is None:
            return
        self.stopped = True
        self._replace_now(track)

    def _make_next(self) -> Optional[Track]:
        now = self.now_playing
        if self.mode.playlist_id and self.mode.playlist_plus:
            return self._plus_next()
        if self.mode.repeat == "one" and now is not None and now.spec is not None:
            return self._again(now)
        if self.mode.playlist_id:
            pos = now.playlist_pos if now and now.playlist_pos is not None else -1
            if pos + 1 < len(self.mode.order) or self.mode.repeat == "all":
                return self._playlist_track(pos + 1)
            return None  # end of the playlist: it stops
        return self._new_track(self._next_genre(), self.mode.mood)

    def _plus_next(self) -> Optional[Track]:
        """Shuffle+: a new song from the playlist's prompt (AI share of radio), else one of its songs."""
        if self.claude_ratio > 0 and self._rng.random() < self.claude_ratio:
            return self._new_track(None, self.mode.playlist_prompt or self.mode.playlist_name, prefer_claude=True)
        return self._bag_track()

    def _bag_track(self, first: Optional[str] = None) -> Optional[Track]:
        """Shuffle+: the next playlist song in a shuffled round; a new round starts when it runs out."""
        if first is not None:
            track = self._library_track(first)
            if track is not None:
                return track
        for _ in range(2):
            while self.mode.bag:
                track = self._library_track(self.mode.bag.pop(0))
                if track is not None:
                    return track
            now = self.now_playing.library_id if self.now_playing else None
            self.mode.bag = [i for i in self.mode.order if i != now] or list(self.mode.order)
            self._rng.shuffle(self.mode.bag)
        return None

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
        """A saved song: rebuilt from its stored spec (audio from the cache or a fresh render)."""
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
        if self._park(track, self._job, use_claude):
            return
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
        if self._park(track, self._render_job):
            return
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
