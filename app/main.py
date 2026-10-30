"""FastAPI app + CLI entry point.

Serve (http://127.0.0.1:8000):
    python -m app.main
Print a generated spec without rendering:
    python -m app.main --dry-run lofi
Back up / restore the database to S3:
    python -m app.main --backup
    python -m app.main --restore motif/backups/motif-20260101-120000.db
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Literal, Optional

from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

from app import apikeys, config, credentials, storage
from app.engine.fluidsynth_engine import FluidSynthEngine
from app.generator import ai, clean_mood, make_spec, procedural
from app.library import LIKED_ID, Library, LibraryError, safe_filename
from app.player import Player
from app.schema import GENRE_TEMPO, GENRES, Genre, SongSpec

log = logging.getLogger("motif")
SONG_HASH = re.compile(r"[0-9a-f]{16}")


class GenreBody(BaseModel):
    genre: Genre = Field(description="A genre id from GET /genres")
    mood: Optional[str] = Field(default=None, max_length=2000)


class GenerateBody(BaseModel):
    genre: Optional[Genre] = Field(default=None, description="Radio for the new songs; null = AI or prompt picks")
    prompt: Optional[str] = Field(default=None, max_length=200,
                                  description="Feel of the new songs (default: the prompt the playlist was made from)")
    count: int = Field(default=1, ge=1, le=10)


class PromptBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    count: Optional[int] = Field(default=None, ge=2, le=20, description="Songs in the playlist (default: app setting)")


STARTUP_CHOICES = ("last", "random", "none", *(f"genre:{g}" for g in GENRES))


class AppSettings(BaseModel):
    """Defaults set in Settings -> App (stored in the database, applied on every device)."""
    startup: str = "last"  # last | random | none | genre:<name>
    repeat: Literal["off", "all", "one"] = "off"
    prompt_count: int = Field(default=8, ge=2, le=20)
    visualizer: bool = True

    @field_validator("startup")
    @classmethod
    def _startup(cls, v: str) -> str:
        if v not in STARTUP_CHOICES:
            raise ValueError("startup must be one of last, random, none or genre:<name>")
        return v


class AppSettingsBody(BaseModel):
    startup: Optional[str] = None
    repeat: Optional[Literal["off", "all", "one"]] = None
    prompt_count: Optional[int] = Field(default=None, ge=2, le=20)
    visualizer: Optional[bool] = None


class RandomBody(BaseModel):
    mood: Optional[str] = Field(default=None, max_length=2000)


class SkipBody(BaseModel):
    from_id: Optional[str] = None
    manual: bool = Field(default=False, description="Next was pressed (vs. the song ended)")


class RepeatBody(BaseModel):
    mode: Literal["off", "all", "one"]


class IdBody(BaseModel):
    id: str = Field(max_length=64)


class DownloadBody(BaseModel):
    format: Literal["mp3", "wav"] = "mp3"


class NameBody(BaseModel):
    name: str = Field(max_length=200)


class NewPlaylistBody(BaseModel):
    name: str = Field(max_length=200)
    song_id: Optional[str] = Field(default=None, max_length=64, description="Track or song id to add first")


class OrderBody(BaseModel):
    song_ids: list[str] = Field(max_length=2000)


class JumpBody(BaseModel):
    pos: int = Field(ge=0)


class ShuffleBody(BaseModel):
    mode: Literal["off", "on", "auto"] = Field(description="auto = Shuffle+: new songs are mixed in")


class PlayPlaylistBody(BaseModel):
    start: Optional[str] = Field(default=None, max_length=64)
    shuffle: bool = False
    plus: bool = Field(default=False, description="Shuffle+: shuffle and mix in newly generated songs, "
                                                  "making more whenever the playlist runs out")


class AISettingsBody(BaseModel):
    backend: Optional[str] = None
    ratio: Optional[float] = None
    model: Optional[str] = Field(default=None, max_length=200)
    openai_model: Optional[str] = Field(default=None, max_length=200)
    cli_model: Optional[str] = Field(default=None, max_length=200)
    base_url: Optional[str] = Field(default=None, max_length=500)
    timeout: Optional[float] = None
    # write-only secrets: "" clears the saved value, None leaves it unchanged
    anthropic_api_key: Optional[str] = Field(default=None, max_length=512)
    openai_api_key: Optional[str] = Field(default=None, max_length=512)


class StorageSettingsBody(BaseModel):
    bucket: Optional[str] = Field(default=None, max_length=63)
    prefix: Optional[str] = Field(default=None, max_length=200)
    endpoint: Optional[str] = Field(default=None, max_length=500)
    region: Optional[str] = Field(default=None, max_length=64)
    media: Optional[bool] = None
    backup_interval_hours: Optional[float] = None
    aws_access_key_id: Optional[str] = Field(default=None, max_length=512)
    aws_secret_access_key: Optional[str] = Field(default=None, max_length=512)


AI_KEYS = ("anthropic_api_key", "openai_api_key")
S3_KEYS = ("aws_access_key_id", "aws_secret_access_key")


class ClaimBody(BaseModel):
    client: str = Field(min_length=1, max_length=64)
    kind: Literal["overlay", "site"] = "site"
    force: bool = False


class PlaybackBody(BaseModel):
    client: str = Field(min_length=1, max_length=64)
    track_id: Optional[str] = Field(default=None, max_length=64)
    state: Literal["playing", "paused"]
    position: float = Field(ge=0, le=36000)
    volume: float = Field(default=1.0, ge=0, le=1)


class ControlBody(BaseModel):
    action: Literal["pause", "resume", "toggle", "seek", "volume"]
    t: Optional[float] = Field(default=None, ge=0, le=36000)
    volume: Optional[float] = Field(default=None, ge=0, le=1)


class NewKeyBody(BaseModel):
    name: str = Field(max_length=200)
    kind: Literal["api", "overlay"] = "api"
    scopes: list[str] = Field(default_factory=list, max_length=8, description="API keys only")


class RestoreBody(BaseModel):
    key: str = Field(max_length=500)


API_TAGS = [
    {"name": "player", "description": "What plays, and controlling it. Answers are the player state."},
    {"name": "queue", "description": "The play list around the current song."},
    {"name": "live", "description": "Server-Sent Events and the speaker (the one client that plays audio)."},
    {"name": "library", "description": "Liked songs, play history and downloads."},
    {"name": "playlists", "description": "Playlists, including AI playlists made from a prompt."},
    {"name": "settings", "description": "App, AI and storage settings (the app's own page only)."},
    {"name": "keys", "description": "Overlay and API keys (the app's own page only)."},
    {"name": "admin", "description": "Database backups (the app's own page only)."},
]
_TAG_RULES = [  # first match wins
    (r"/(state|genres|genre|random|skip|previous|play|repeat|prompt|audio/.*)", "player"),
    (r"/queue(/.*)?", "queue"),
    (r"/(events|speaker/.*|playback|control)", "live"),
    (r"/(like|library(/.*)?|history|songs/.*|download/.*)", "library"),
    (r"/playlists(/.*)?", "playlists"),
    (r"/settings/keys(/.*)?", "keys"),
    (r"/settings(/.*)?", "settings"),
    (r"/admin/.*", "admin"),
]


def _tag_routes(app: FastAPI) -> None:
    """Group the endpoints in /docs by what they do."""
    for route in app.routes:
        path = getattr(route, "path", "")
        for pattern, tag in _TAG_RULES:
            if re.fullmatch(pattern, path.replace("{", "").replace("}", "")):
                route.tags = [tag]
                break


def create_app(player: Player | None = None, library: Library | None = None,
               backups: storage.BackupStore | None = None) -> FastAPI:
    lib = library or Library()
    creds = credentials.Credentials(lib.db)
    keys = apikeys.ApiKeys(lib.db)
    credentials.use(creds)
    scheduler: list[storage.BackupScheduler] = []

    def apply_storage() -> None:
        """(Re)build S3 media + backups from the current storage settings; restart scheduled backups."""
        settings = app.state.storage_settings
        try:
            media, backups_ = storage.from_settings(settings, creds)
        except Exception:  # e.g. boto3 can't build a client from these settings
            log.exception("S3 storage could not be configured; staying local")
            media, backups_ = storage.MediaStore(), None
        app.state.backups = backups_
        if app.state.player is not None:
            app.state.player.engine.media = media
        while scheduler:
            scheduler.pop().stop()
        if backups_ and settings.backup_interval_hours > 0:
            scheduler.append(storage.BackupScheduler(backups_, lib.db, settings.backup_interval_hours))
            scheduler[0].start()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _load_ai_settings(lib)
        if app.state.player is None:
            app.state.player = Player(
                FluidSynthEngine(), make_spec, claude_ratio=_ai_ratio(),
                on_history=lambda t: lib.record_history(t.id, t.spec, t.source), library=lib,
            )
            apply_storage()
            cue_startup_song(app.state.player, lib)
        yield
        while scheduler:
            scheduler.pop().stop()
        app.state.player.shutdown()

    app = FastAPI(title="Motif", lifespan=lifespan, openapi_tags=API_TAGS,
                  description="Local generative radio. Tools and bots use an API key (`Authorization: Bearer mk_…`) "
                              "with the scopes they need; see the README.")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.ALLOWED_HOSTS)

    _LIBRARY_EDIT = re.compile(r"/(like|library(/.*)?|songs/.*|admin/restore|playlists(/[^/]+(/songs(/.*)?|/order)?)?)")

    @app.middleware("http")
    async def library_sync(request: Request, call_next):
        """Any change to liked songs or playlists tells open pages to reload their library."""
        response = await call_next(request)
        if (request.method in ("POST", "PUT", "PATCH", "DELETE") and response.status_code < 400
                and _LIBRARY_EDIT.fullmatch(request.url.path) and app.state.player is not None):
            app.state.player.library_touched()
        return response

    @app.middleware("http")
    async def access(request: Request, call_next):
        """API keys for tools/overlays; otherwise the same-origin browser rules.

        With `Authorization: Bearer mk_…` (API key) or `mo_…` (overlay key) a request is allowed only within the key's scopes
        (settings, admin and library edits are never reachable with a key). Without a key,
        other web pages are blocked from changing anything: the API can forward the OpenAI
        key to the configured base URL, so settings must only change from this app's own page.
        """
        auth = request.headers.get("authorization")
        if auth is not None:
            token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            key = await asyncio.to_thread(keys.verify, token) if token else None
            if key is None:
                return JSONResponse({"detail": "invalid API key"}, status_code=401)
            scope = apikeys.required_scope(request.method, request.url.path)
            if scope is None or not apikeys.has_scope(key["scopes"], scope):
                return JSONResponse({"detail": "this API key can't do that"}, status_code=403)
            if not keys.allow(key["id"], scope):
                return JSONResponse({"detail": "too many requests for this API key"}, status_code=429)
            request.state.api_key = key
            return await call_next(request)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin is not None:
            if urlsplit(origin).hostname not in config.ALLOWED_HOSTS:
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    app.state.player = player
    app.state.library = lib
    app.state.sse_ping = 15.0  # seconds between keep-alive comments on idle streams
    app.state.backups = backups
    app.state.storage_settings = _load_storage_settings(lib)

    def p() -> Player:
        return app.state.player

    @app.exception_handler(LibraryError)
    async def library_error(_: Request, exc: LibraryError):
        return JSONResponse({"detail": str(exc)}, status_code=404 if str(exc).startswith("unknown") else 400)

    @app.exception_handler(storage.StorageError)
    async def storage_error(_: Request, exc: storage.StorageError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def find_spec(song_id: str) -> tuple[SongSpec, str]:
        """Spec of a saved song, a known track, or a track from the persisted history."""
        spec = lib.spec(song_id)
        if spec is not None:
            return spec, (lib.get(song_id) or {}).get("source", "procedural")
        t = p().get(song_id)
        if t is not None and t.spec is not None:
            return t.spec, t.source
        found = lib.history_spec(song_id)
        if found is None:
            raise HTTPException(404, "unknown song")
        return found

    def save(track_or_song_id: str, liked: bool) -> tuple[dict, bool]:
        """Store a track (seed + spec), liked or just for a playlist. Returns (entry, already)."""
        t = p().get(track_or_song_id)
        if t is not None and t.spec is None:
            raise HTTPException(409, "track is still being composed")
        spec, source = find_spec(track_or_song_id)
        entry, already = lib.save(spec, source, liked=liked)
        if not already and liked:
            p().pool.submit(_store_media, p(), spec, "mp3")
        return entry, already

    # --- radio / player ---------------------------------------------------

    @app.get("/genres")
    def genres():
        """The radios a request can name (e.g. `genre` in POST /genre or /playlists/{id}/generate),
        with each one's display name and tempo range in bpm."""
        s = ai.get_settings()
        return {"genres": list(GENRES),
                "details": [{"id": g, "name": g.replace("-", " ").capitalize(), "tempo_bpm": list(GENRE_TEMPO[g])}
                            for g in GENRES],
                "ai": {"backend": s.backend, "model": s.model}}

    @app.get("/state")
    def state():
        """What plays now and next, and the play mode (radio, playlist, shuffle, repeat).

        Every player action below answers with this same state."""
        return p().state()

    @app.get("/queue")
    def queue():
        """Now playing and what comes next: a playlist's remaining queue (`pos` per song, `repeats`
        when it loops), or for a radio / Shuffle+ just the next song."""
        return p().queue()

    @app.post("/queue/jump")
    def queue_jump(body: JumpBody):
        """Play the playlist queue from this position."""
        if not p().jump(body.pos):
            raise HTTPException(409, "nothing to play at that position")
        return p().state()

    @app.post("/previous")
    def previous():
        """Back one song (the previous one in the queue, or on a radio the last one played)."""
        moved = p().previous()
        return {"moved": moved, **p().state()}

    @app.get("/history")
    def history(limit: int = Query(default=50, ge=1, le=200)):
        """Persisted play history (survives restarts), newest first."""
        return {"history": lib.history(limit)}

    @app.post("/genre")
    def genre(body: GenreBody):
        """Play a genre's radio (optionally steered by a mood)."""
        p().play_genre(body.genre, clean_mood(body.mood))
        return p().state()

    @app.post("/prompt")
    def prompt(body: PromptBody):
        """Free text -> a new playlist of songs that follow the prompt, played right away.

        With AI on, one call returns a title and a brief per song (it may pick the radio); otherwise
        the prompt's words pick the radio. Songs are stored as seed + spec and rendered as they play.
        """
        text = clean_mood(body.text)
        if not text:
            raise HTTPException(400, "empty prompt")
        use_ai = ai.get_settings().backend != "off"
        genre = None if use_ai else procedural.genre_from_prompt(text)
        count = body.count or load_app_settings(lib).prompt_count
        title, songs = ai.make_playlist(genre, text, count, use_ai)
        made_by_ai = any(source != "procedural" for _, source in songs)
        pl = lib.create_playlist(title, source="ai" if made_by_ai else None, prompt=text)
        for spec, source in songs:
            entry, _ = lib.save(spec, source)
            lib.add_to_playlist(pl["id"], entry["id"])
        p().play_playlist(pl["id"])
        return {"playlist": lib.playlist(pl["id"]), "genre": songs[0][0].genre, **p().state()}

    @app.post("/playlists/{pl_id}/generate")
    def generate_into_playlist(pl_id: str, body: GenerateBody | None = None):
        """Make new songs and append them to a playlist (with AI on, in the playlist's vibe).

        A fresh random seed is picked for each song; `genre` pins the radio (e.g. a chat request).
        If the playlist is playing, the new songs join its queue.
        """
        body = body or GenerateBody()
        pl = lib.playlist(pl_id) if pl_id != LIKED_ID else None
        if pl is None:
            raise HTTPException(404, "unknown playlist")
        added = add_generated(pl, body.genre, clean_mood(body.prompt), body.count)
        return {"added": added, "playlist": lib.playlist(pl_id)}

    def add_generated(pl: dict, genre: Optional[str], prompt: Optional[str], count: int) -> list[dict]:
        pl_id = pl["id"]
        text = prompt or pl.get("prompt") or pl["name"]
        use_ai = ai.get_settings().backend != "off"
        genre = genre or (None if use_ai else procedural.genre_from_prompt(text))
        existing = [{"name": s["name"], "genre": s["genre"]} for s in pl["songs"]]
        _, songs = ai.make_playlist(genre, text, count, use_ai, existing=existing)
        added = []
        for spec, source in songs:
            entry, _ = lib.save(spec, source)
            lib.add_to_playlist(pl_id, entry["id"])
            added.append(entry)
        p().playlist_grew(pl_id, [e["id"] for e in added])
        return added

    @app.post("/random")
    def random_(body: RandomBody | None = None):
        """Play a random radio that changes genre every song."""
        p().play_random(clean_mood(body.mood) if body else None)
        return p().state()

    @app.post("/skip")
    def skip(request: Request, body: SkipBody | None = None):
        key = getattr(request.state, "api_key", None)
        if key and "control" not in key["scopes"] and not (body and body.from_id):
            raise HTTPException(403, "a speaker key can only advance at the end of the playing song (from_id)")
        moved = p().skip(body.from_id if body else None, manual=body is None or body.manual or not body.from_id)
        return {"skipped": moved, **p().state()}

    # --- live sync: SSE, speaker and remote control -----------------------------

    @app.get("/events")
    async def events(request: Request, client: str = Query(min_length=1, max_length=64),
                     kind: Literal["overlay", "site"] = "site",
                     name: Optional[str] = Query(default=None, max_length=40), audio: bool = True):
        """Server-Sent Events: a full versioned snapshot now and after every change."""
        player = p()
        key = getattr(request.state, "api_key", None)
        if kind == "overlay" and key is not None and key.get("kind") != "overlay":
            raise HTTPException(403, "overlays need an overlay key, not an API key")
        label = " ".join(name.split())[:40] if name else None
        player.connect_client(client, kind, label or None, audio)

        async def stream():
            last = -1
            try:
                while not await request.is_disconnected():
                    if key and key["id"] in keys.revoked:
                        break  # revoked while connected: end the stream (and its speaker role) now
                    version = await asyncio.to_thread(player.wait_change, last, app.state.sse_ping)
                    if version == last:
                        if key and not await asyncio.to_thread(keys.active, key["id"]):
                            break
                        yield ": ping\n\n"  # keep proxies and the client from timing out
                        continue
                    snap = player.live()
                    last = snap["version"]
                    yield f"event: state\nid: {last}\ndata: {json.dumps(snap, separators=(',', ':'))}\n\n"
            finally:
                player.disconnect_client(client)  # releases the speaker role if this was the last stream

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/speaker/claim")
    def claim_speaker(body: ClaimBody):
        return {"speaker": p().claim_speaker(body.client, body.kind, body.force)}

    @app.post("/speaker/assign")
    def assign_speaker(body: IdBody):
        """Play on another connected player (an overlay, or this tab via its client id)."""
        try:
            return {"speaker": p().assign_speaker(body.id)}
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/playback")
    def playback(body: PlaybackBody):
        """The speaker reports what it is doing so remotes can show the same position."""
        if not p().report_playback(body.client, body.track_id, body.state, body.position, body.volume):
            raise HTTPException(409, "this client is not the speaker")
        return {"ok": True}

    @app.post("/control")
    def control(body: ControlBody):
        """Remote control for the speaker: pause, resume, toggle, seek, volume."""
        if body.action == "seek" and body.t is None:
            raise HTTPException(422, "seek needs t (seconds)")
        if body.action == "volume" and body.volume is None:
            raise HTTPException(422, "volume needs volume (0..1)")
        try:
            return {"command": p().send_command(body.action, body.t, body.volume)}
        except LookupError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/repeat")
    def repeat(body: RepeatBody):
        """Repeat: off, all (a playlist loops) or one (the same song again)."""
        p().set_repeat(body.mode)
        return p().state()

    @app.get("/audio/{track_id}.mp3")
    def audio(track_id: str):
        """The MP3 of a track that is playing or queued (status ready)."""
        t = p().get(track_id)
        if t is None or t.status != "ready":
            raise HTTPException(404, "no such audio (yet)")
        path = p().engine.mp3_path(t.spec)
        if not path.exists():  # pruned meanwhile
            path = p().engine.ensure_mp3(t.spec)
        return FileResponse(path, media_type="audio/mpeg")

    @app.post("/download/{song_id}")
    def download(song_id: str, body: DownloadBody | None = None):
        """Render on the server (cached) and send the file; also copies it to S3 media when configured."""
        fmt = body.format if body else "mp3"
        spec, _ = find_spec(song_id)
        name = (lib.get(song_id) or {}).get("name") or spec.name
        path = p().engine.render(spec) if fmt == "wav" else p().engine.ensure_mp3(spec)
        p().pool.submit(_store_media, p(), spec, fmt)
        filename = f"{safe_filename(name)} ({spec.genre}).{fmt}"
        return FileResponse(path, media_type="audio/mpeg" if fmt == "mp3" else "audio/wav", filename=filename)

    # --- library ------------------------------------------------------------

    @app.post("/like")
    def like(body: IdBody):
        """Save a track or song to Liked songs."""
        entry, already = save(body.id, liked=True)
        return {"entry": entry, "already": already}

    @app.get("/library")
    def library_():
        """Liked songs, newest first."""
        return {"library": lib.entries()}

    @app.patch("/library/{song_id}")
    def rename_song(song_id: str, body: NameBody):
        """Rename a saved song."""
        return {"entry": lib.rename_song(song_id, body.name)}

    @app.delete("/library/{song_id}")
    def unlike(song_id: str):
        """Remove from Liked songs (kept while a playlist still uses it)."""
        lib.unlike(song_id)
        return {"ok": True}

    @app.delete("/songs/{song_id}")
    def delete_song(song_id: str):
        """Delete a song everywhere: seed + spec, playlist entries, history and cached audio.

        `song_id` is a saved-song id (the spec hash) or a track id from the queue/history.
        """
        if lib.get(song_id) is not None or SONG_HASH.fullmatch(song_id):
            spec_hash = song_id
        else:
            spec, _ = find_spec(song_id)
            spec_hash = spec.audio_hash()
        removed = lib.delete_song(spec_hash)
        in_use = p().forget(spec_hash)
        if not removed["saved"] and not removed["history"] and not in_use:
            raise HTTPException(404, "unknown song")
        if not in_use:  # keep audio a playing/queued song still needs; the cache prunes it later
            p().pool.submit(p().engine.forget, spec_hash)
        return {"deleted": removed}

    @app.post("/play")
    def play(body: IdBody):
        """Play a saved song (song id) or replay a played track (track id)."""
        if lib.get(body.id) is not None:
            try:
                p().play_saved(body.id)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
        else:
            spec, source = find_spec(body.id)
            p().play_spec(spec, source)
        return p().state()

    # --- playlists ----------------------------------------------------------

    @app.get("/playlists")
    def playlists():
        """All playlists (without their songs)."""
        return {"playlists": lib.playlists()}

    @app.post("/playlists")
    def create_playlist(body: NewPlaylistBody):
        """Create an empty playlist."""
        pl = lib.create_playlist(body.name)
        if body.song_id:
            entry, _ = save(body.song_id, liked=False)
            pl = lib.add_to_playlist(pl["id"], entry["id"])
        return {"playlist": pl}

    @app.get("/playlists/{pl_id}")
    def playlist(pl_id: str):
        """One playlist with its songs. The id `liked` is Liked songs."""
        pl = lib.playlist(pl_id)
        if pl is None:
            raise HTTPException(404, "unknown playlist")
        return {"playlist": pl}

    @app.patch("/playlists/{pl_id}")
    def rename_playlist(pl_id: str, body: NameBody):
        """Rename a playlist."""
        return {"playlist": lib.rename_playlist(pl_id, body.name)}

    @app.delete("/playlists/{pl_id}")
    def delete_playlist(pl_id: str):
        """Delete a playlist (songs that are liked or in another playlist are kept)."""
        lib.delete_playlist(pl_id)
        return {"ok": True}

    @app.post("/playlists/{pl_id}/songs")
    def add_song(pl_id: str, body: IdBody):
        """Add a track or saved song; unsaved tracks are saved first."""
        if lib.playlist(pl_id) is None:
            raise HTTPException(404, "unknown playlist")
        entry, _ = save(body.id, liked=False)
        return {"playlist": lib.add_to_playlist(pl_id, entry["id"]), "entry": entry}

    @app.delete("/playlists/{pl_id}/songs/{song_id}")
    def remove_song_from_playlist(pl_id: str, song_id: str):
        """Remove a song from a playlist."""
        return {"playlist": lib.remove_from_playlist(pl_id, song_id)}

    @app.put("/playlists/{pl_id}/order")
    def reorder(pl_id: str, body: OrderBody):
        """Set the playlist order (exactly its song ids)."""
        return {"playlist": lib.reorder_playlist(pl_id, body.song_ids)}

    @app.post("/playlists/{pl_id}/shuffle")
    def shuffle_playlist(pl_id: str, body: ShuffleBody):
        """Switch shuffle for the playing playlist without restarting the current song."""
        try:
            p().set_playlist_shuffle(pl_id, body.mode)
        except LookupError as e:
            raise HTTPException(409, str(e)) from e
        return p().state()

    @app.post("/playlists/{pl_id}/play")
    def play_playlist(pl_id: str, body: PlayPlaylistBody | None = None):
        body = body or PlayPlaylistBody()
        try:
            p().play_playlist(pl_id, body.start, body.shuffle or body.plus, plus=body.plus)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return p().state()

    # --- settings & storage -----------------------------------------------------

    def settings_payload() -> dict:
        st: storage.StorageSettings = app.state.storage_settings
        return {
            "app": load_app_settings(lib).model_dump(),
            "ai": ai.get_settings().public(),
            "storage": {
                **st.fields(), "bucket": st.bucket or None, "enabled": st.enabled,
                "keys": {k: creds.status(k) for k in S3_KEYS},
            },
        }

    def save_keys(values: dict) -> None:
        try:
            creds.update(values)
        except credentials.CredentialsError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/settings")
    def settings():
        """All settings (keys and secrets only as set / not set)."""
        return settings_payload()

    @app.put("/settings/ai")
    def update_ai(body: AISettingsBody):
        changes = body.model_dump(exclude_none=True)
        keys = {k: changes.pop(k, None) for k in AI_KEYS}
        try:
            new = ai.AISettings(**{**_fields(ai.get_settings()), **changes})
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        save_keys(keys)
        ai.set_settings(new)
        lib.set_setting("ai", _fields(new))
        p().claude_ratio = _ai_ratio()
        return settings_payload()

    @app.put("/settings/app")
    def update_app(body: AppSettingsBody):
        try:
            new = AppSettings(**{**load_app_settings(lib).model_dump(), **body.model_dump(exclude_none=True)})
        except ValidationError as e:
            raise HTTPException(400, e.errors()[0]["msg"]) from e
        lib.set_setting("app", new.model_dump())
        if body.repeat is not None:
            p().set_repeat(new.repeat)  # the default applies right away too
        return settings_payload()

    @app.put("/settings/storage")
    def update_storage(body: StorageSettingsBody):
        changes = body.model_dump(exclude_none=True)
        keys = {k: changes.pop(k, None) for k in S3_KEYS}
        try:
            new = storage.StorageSettings(**{**app.state.storage_settings.fields(), **changes})
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        save_keys(keys)
        app.state.storage_settings = new
        lib.set_setting("storage", new.fields())
        apply_storage()
        return settings_payload()

    @app.post("/settings/storage/test")
    def test_storage():
        return storage.test_connection(app.state.storage_settings, creds)

    @app.post("/settings/ai/test")
    def test_ai():
        return ai.test_backend()

    @app.get("/settings/keys")
    def list_keys():
        """Overlay and API keys (never the tokens)."""
        return {"keys": keys.list()}

    @app.post("/settings/keys")
    def create_key(body: NewKeyBody):
        """Create an overlay key (kind overlay) or an API key with scopes; the token is returned once."""
        try:
            key, token = keys.create(body.name, body.scopes, body.kind)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"key": key, "token": token}

    @app.delete("/settings/keys/{key_id}")
    def revoke_key(key_id: str):
        """Revoke a key; streams using it close right away."""
        if not keys.revoke(key_id):
            raise HTTPException(404, "unknown key")
        with p().changed:  # wake every stream so one using this key ends right away
            p().changed.notify_all()
        return {"ok": True}

    @app.get("/admin/backups")
    def list_backups():
        if app.state.backups is None:
            return {"enabled": False, "backups": []}
        return {"enabled": True, "backups": app.state.backups.list()}

    @app.post("/admin/backup")
    def backup():
        if app.state.backups is None:
            raise HTTPException(400, "S3 storage is not configured (Settings → Storage)")
        return {"backup": app.state.backups.backup(lib.db)}

    @app.post("/admin/restore")
    def restore(body: RestoreBody):
        if app.state.backups is None:
            raise HTTPException(400, "S3 storage is not configured (Settings → Storage)")
        with lib.lock:
            app.state.backups.restore(body.key, lib.db)
        _load_ai_settings(lib)
        p().claude_ratio = _ai_ratio()
        # Keep using the bucket we restored from even if the backup holds older storage settings.
        lib.set_setting("storage", app.state.storage_settings.fields())
        return {"ok": True}

    @app.get("/overlay", include_in_schema=False)
    def overlay():
        """OBS/browser overlay page. It needs an overlay key (?key=mo_… or #key=…)."""
        # The key may arrive as ?key=…: don't let it leak through referrers or caches (and the access
        # log redacts it, see RedactKeyFilter).
        return FileResponse(config.WEB_DIR / "overlay.html", media_type="text/html",
                            headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})

    _tag_routes(app)

    if config.WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")
    return app


def _fields(s: "ai.AISettings") -> dict:
    return asdict(s)


def _store_media(player: Player, spec: SongSpec, ext: str) -> None:
    try:
        player.engine.store(spec, ext)
    except Exception:  # media backup must never break playback
        log.exception("could not copy %s.%s to media storage", spec.audio_hash(), ext)


def _load_ai_settings(lib: Library) -> None:
    saved = lib.get_setting("ai")
    if not saved:
        return
    try:
        ai.set_settings(ai.AISettings(**{**_fields(ai.AISettings.from_config()), **saved}))
    except (TypeError, ValueError):
        log.warning("ignoring invalid saved AI settings")


def _load_storage_settings(lib: Library) -> storage.StorageSettings:
    """Settings saved from the UI win over the S3_* environment defaults."""
    base = storage.StorageSettings.from_config()
    saved = lib.get_setting("storage")
    if not saved:
        return base
    try:
        return storage.StorageSettings(**{**base.fields(), **saved})
    except (TypeError, ValueError):
        log.warning("ignoring invalid saved storage settings")
        return base


def load_app_settings(lib: Library) -> AppSettings:
    try:
        return AppSettings(**(lib.get_setting("app") or {}))
    except ValidationError:
        log.warning("ignoring invalid saved app settings")
        return AppSettings()


class RedactKeyFilter(logging.Filter):
    """Mask API keys passed as `key=` query parameters (overlay URLs) in log lines."""

    PATTERN = re.compile(r"(key=)[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(self.PATTERN.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = self.PATTERN.sub(r"\1***", record.msg)
        return True


def cue_startup_song(player: Player, lib: Library) -> None:
    """Cue what Settings -> App asks for on start: the last played song (if it can be rebuilt), a random
    radio, a specific radio, or nothing. Also applies the default repeat mode.

    It is only cued (rendered and shown); the browser starts playback when the user presses play.
    """
    prefs = load_app_settings(lib)
    player.set_repeat(prefs.repeat)
    if prefs.startup == "none":
        return
    if prefs.startup.startswith("genre:"):
        player.play_genre(prefs.startup.split(":", 1)[1])
        log.info("cued the %s radio", prefs.startup.split(":", 1)[1])
        return
    if prefs.startup == "last":
        for h in lib.history(limit=20):
            found = lib.history_spec(h["id"])
            if found is not None:
                spec, source = found
                player.play_spec(spec, source)
                log.info("cued the last played song %r", spec.name)
                return
    genre = random.choice(GENRES)
    player.play_genre(genre)
    log.info("cued a %s radio song", genre)


def _ai_ratio() -> float:
    s = ai.get_settings()
    return s.ratio if s.backend != "off" else 0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="motif", description="Local generative radio")
    ap.add_argument("genre", nargs="?", help="genre for --dry-run (default: random)")
    ap.add_argument("--dry-run", action="store_true", default=os.environ.get("MOTIF_DRY_RUN") == "1",
                    help="generate a spec and print it without rendering (env MOTIF_DRY_RUN=1)")
    ap.add_argument("--mood", help="mood/prompt text for --dry-run")
    ap.add_argument("--seed", type=int, help="seed for --dry-run (procedural)")
    ap.add_argument("--ai", "--claude", dest="ai", action="store_true", help="--dry-run through the AI backend")
    ap.add_argument("--backup", action="store_true", help="back up the database to S3 and exit")
    ap.add_argument("--restore", metavar="KEY", help="restore the database from an S3 backup key and exit")
    ap.add_argument("--port", type=int, default=config.PORT)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.backup or args.restore:
        lib = Library()
        creds = credentials.Credentials(lib.db)
        _, backups = storage.from_settings(_load_storage_settings(lib), creds)
        if backups is None:
            ap.error("S3 storage is not configured (Settings → Storage, or S3_BUCKET)")
        if args.backup:
            print(json.dumps(backups.backup(lib.db)))
        else:
            backups.restore(args.restore, lib.db)
            print(json.dumps({"restored": args.restore}))
        return 0

    if args.dry_run:
        genre = args.genre or (None if args.ai else random.choice(GENRES))
        if genre is not None and genre not in GENRES:
            ap.error(f"unknown genre {genre!r}; choose from {', '.join(GENRES)}")
        mood = clean_mood(args.mood)
        t0 = time.time()
        if args.seed is not None:
            spec, source = procedural.generate(genre or random.choice(GENRES), mood, seed=args.seed), "procedural"
        else:
            spec, source = make_spec(genre, mood, args.ai)
        print(json.dumps({"source": source, "seconds": round(time.time() - t0, 2), "hash": spec.audio_hash(),
                          "minutes": round(spec.music_seconds / 60, 2), "spec": spec.model_dump(mode="json")},
                         indent=2))
        return 0

    import uvicorn

    if config.HOST not in ("127.0.0.1", "localhost", "::1"):
        log.warning("listening on %s: Motif has no login, so anyone who can reach this port can control it "
                    "and use the saved API keys. Keep it behind localhost or an authenticating proxy.", config.HOST)
    logging.getLogger("uvicorn.access").addFilter(RedactKeyFilter())
    uvicorn.run(create_app(), host=config.HOST, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
