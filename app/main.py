"""FastAPI app + CLI entry point.

Serve (http://127.0.0.1:8000):
    python -m app.main
Print a generated spec without rendering:
    python -m app.main --dry-run lofi

Every signed-in user has their own player, library, settings, AI and backups
(app/users.py); API and overlay keys act as the user who made them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import os
import random
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Literal, Optional

from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

from app import accounts as acct, apikeys, config, credentials, mailer, storage
from app.engine.fluidsynth_engine import FluidSynthEngine
from app.generator import ai, clean_mood, make_spec, procedural
from app.library import LIKED_ID, Library, LibraryError, safe_filename
from app.player import Player
from app.schema import GENRE_TEMPO, GENRES, Genre, SongSpec
from app.users import Users

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


class BackupSettingsBody(BaseModel):
    bucket: Optional[str] = Field(default=None, max_length=63)
    prefix: Optional[str] = Field(default=None, max_length=200)
    endpoint: Optional[str] = Field(default=None, max_length=500)
    region: Optional[str] = Field(default=None, max_length=64)
    backup_interval_hours: Optional[float] = None
    aws_access_key_id: Optional[str] = Field(default=None, max_length=512)
    aws_secret_access_key: Optional[str] = Field(default=None, max_length=512)


AI_KEYS = credentials.AI_NAMES
S3_KEYS = credentials.S3_NAMES


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


class LoginBody(BaseModel):
    email: str = Field(max_length=acct.EMAIL_MAX)
    password: str = Field(max_length=acct.PASSWORD_MAX)


class SetPasswordBody(BaseModel):
    token: str = Field(max_length=100)
    password: str = Field(min_length=acct.PASSWORD_MIN, max_length=acct.PASSWORD_MAX)


class ChangePasswordBody(BaseModel):
    current: str = Field(max_length=acct.PASSWORD_MAX)
    new: str = Field(min_length=acct.PASSWORD_MIN, max_length=acct.PASSWORD_MAX)


class InviteBody(BaseModel):
    email: str = Field(max_length=acct.EMAIL_MAX)
    features: Optional["FeaturesBody"] = None  # what the member gets; missing ones are on

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return acct.valid_email(v)


class FeaturesBody(BaseModel):
    download: Optional[bool] = None
    ai: Optional[bool] = None
    api: Optional[bool] = None
    overlay: Optional[bool] = None
    backup: Optional[bool] = None


class UserPatchBody(BaseModel):
    features: FeaturesBody


class MemberPasswordBody(BaseModel):
    password: str = Field(min_length=8, max_length=200)


InviteBody.model_rebuild()


API_TAGS = [
    {"name": "player", "description": "What plays, and controlling it. Answers are the player state."},
    {"name": "queue", "description": "The play list around the current song."},
    {"name": "live", "description": "Server-Sent Events and the speaker (the one client that plays audio)."},
    {"name": "library", "description": "Liked songs, play history and downloads."},
    {"name": "playlists", "description": "Playlists, including AI playlists made from a prompt."},
    {"name": "settings", "description": "Your app, AI and backup settings (the app's own page only)."},
    {"name": "keys", "description": "Your overlay and API keys (the app's own page only)."},
    {"name": "backups", "description": "Backups of your own data to your S3 bucket (the app's own page only)."},
    {"name": "account", "description": "Sign in and out, invite/reset links and your own password."},
    {"name": "members", "description": "Users, invites and per-member features (admin only)."},
]
_TAG_RULES = [  # first match wins
    (r"/(state|genres|genre|random|skip|previous|play|repeat|prompt|audio/.*)", "player"),
    (r"/queue(/.*)?", "queue"),
    (r"/(events|speaker/.*|playback|control)", "live"),
    (r"/(like|library(/.*)?|history|songs/.*|download/.*)", "library"),
    (r"/playlists(/.*)?", "playlists"),
    (r"/settings/keys(/.*)?", "keys"),
    (r"/settings(/.*)?", "settings"),
    (r"/backups(/.*)?", "backups"),
    (r"/auth(/.*)?", "account"),
    (r"/(users|invites)(/.*)?", "members"),
]
# Reachable without a session in login mode: the page and its files, the overlay page (it uses a key),
# API docs, and the sign-in endpoints. /auth/me answers 401 itself.
_PUBLIC_GET = re.compile(r"/|/overlay|/docs(/oauth2-redirect)?|/redoc|/openapi\.json|/genres|/auth/(me|token/[^/]+)"
                         r"|/[\w.-]+\.(html|js|css|map|ico|png|svg|webp|webmanifest|woff2?|txt)")
_PUBLIC_POST = re.compile(r"/auth/(login|logout|token)")
ADMIN = [Depends(acct.require_admin)]


def _tag_routes(app: FastAPI) -> None:
    """Group the endpoints in /docs by what they do."""
    for route in app.routes:
        path = getattr(route, "path", "")
        for pattern, tag in _TAG_RULES:
            if re.fullmatch(pattern, path.replace("{", "").replace("}", "")):
                route.tags = [tag]
                break


def create_app(player: Player | None = None, library: Library | None = None,
               accounts: acct.Accounts | None = None) -> FastAPI:
    """`accounts` defaults to the MOTIF_ADMIN_* environment (tests pass their own to turn login on).

    A given `player` (tests) becomes the admin's / local user's player; otherwise players are made
    per user on first use, all sharing one FluidSynth engine.
    """
    lib = library or Library()
    accts = accounts or acct.Accounts.from_config(lib.db)
    creds = credentials.Credentials(lib.db)
    keys = apikeys.ApiKeys(lib.db)
    users = Users(lib, creds, accts, cue=cue_startup_song)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.player is not None:
            users.attach(app.state.player.engine)
            users.adopt(accts.admin_id, app.state.player)
        else:
            users.attach(FluidSynthEngine())
            app.state.player = users.player(accts.admin_id)  # cued now, like a single-user start
        users.schedule_all()
        yield
        users.shutdown()

    app = FastAPI(title="Motif", lifespan=lifespan, openapi_tags=API_TAGS,
                  description="Local generative radio. Tools and bots use an API key (`Authorization: Bearer mk_…`) "
                              "with the scopes they need; see the README.")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.ALLOWED_HOSTS)

    _LIBRARY_EDIT = re.compile(r"/(like|library(/.*)?|songs/.*|backups/restore|playlists(/[^/]+(/songs(/.*)?|/order)?)?)")

    @app.middleware("http")
    async def library_sync(request: Request, call_next):
        """Any change to liked songs or playlists tells the user's open pages to reload their library."""
        response = await call_next(request)
        user = getattr(request.state, "user", None)
        if (request.method in ("POST", "PUT", "PATCH", "DELETE") and response.status_code < 400
                and _LIBRARY_EDIT.fullmatch(request.url.path) and user is not None):
            player_ = users.existing(user["id"])
            if player_ is not None:
                player_.library_touched()
        return response

    @app.middleware("http")
    async def access(request: Request, call_next):
        """API keys for tools/overlays; otherwise the same-origin browser rules and, in login mode, a session.

        With `Authorization: Bearer mk_…` (API key) or `mo_…` (overlay key) a request is allowed only within the key's scopes
        (settings, backups and library edits are never reachable with a key). Without a key,
        other web pages are blocked from changing anything: the API can forward a saved OpenAI
        key to the configured base URL, so settings must only change from this app's own page.

        The resolved user (dict id/email/role/features) is put on `request.state.user`; a key acts as
        the user who made it.
        """
        auth = request.headers.get("authorization")
        if auth is not None:
            token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
            key = await asyncio.to_thread(keys.verify, token) if token else None
            user = await asyncio.to_thread(accts.key_owner, key["user_id"]) if key else None
            if key is None or user is None:
                return JSONResponse({"detail": "invalid API key"}, status_code=401)
            scope = apikeys.required_scope(request.method, request.url.path)
            if scope is None or not apikeys.has_scope(key["scopes"], scope):
                return JSONResponse({"detail": "this API key can't do that"}, status_code=403)
            if not keys.allow(key["id"], scope):
                return JSONResponse({"detail": "too many requests for this API key"}, status_code=429)
            feature = "overlay" if key.get("kind") == "overlay" else "api"
            if not acct.has_feature(user, feature):
                return JSONResponse({"detail": f"{'Overlay' if feature == 'overlay' else 'API'} access is turned "
                                               "off for this account"}, status_code=403)
            request.state.api_key = key
            request.state.user = user
            return await call_next(request)
        # Cookie sessions are SameSite=Lax; this also refuses cross-site writes that would carry them.
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin is not None:
            if urlsplit(origin).hostname not in config.ALLOWED_HOSTS:
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        if not accts.login:
            request.state.user = dict(acct.LOCAL_USER)
            return await call_next(request)
        session = request.cookies.get(acct.COOKIE)
        user = await asyncio.to_thread(accts.session_user, session) if session else None
        if user is not None:
            request.state.user = user
            return await call_next(request)
        public = _PUBLIC_GET if request.method in ("GET", "HEAD") else _PUBLIC_POST if request.method == "POST" else None
        if public is not None and public.fullmatch(request.url.path):
            return await call_next(request)
        return JSONResponse({"detail": "not signed in"}, status_code=401)

    app.state.player = player  # the admin's / local user's player (set on start)
    app.state.library = lib
    app.state.accounts = accts
    app.state.users = users
    app.state.sse_ping = 15.0  # seconds between keep-alive comments on idle streams

    def uid(request: Request) -> str:
        return acct.current_user(request)["id"]

    def lib_of(request: Request) -> Library:
        """The request user's library."""
        return users.lib(uid(request))

    def p(request: Request) -> Player:
        """The request user's player."""
        return users.player(uid(request))

    def listening(request: Request) -> Player:
        """The request user's player, counted as listening activity: a locked phone may have lost its
        event stream but still reports, skips and fetches audio, so prefetching must go on."""
        player_ = p(request)
        player_.activity()
        return player_

    @app.exception_handler(LibraryError)
    async def library_error(_: Request, exc: LibraryError):
        return JSONResponse({"detail": str(exc)}, status_code=404 if str(exc).startswith("unknown") else 400)

    @app.exception_handler(storage.StorageError)
    async def storage_error(_: Request, exc: storage.StorageError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    def find_spec(request: Request, song_id: str) -> tuple[SongSpec, str]:
        """Spec of a saved song, a known track, or a track from the persisted history (the user's own)."""
        lib_ = lib_of(request)
        spec = lib_.spec(song_id)
        if spec is not None:
            return spec, (lib_.get(song_id) or {}).get("source", "procedural")
        t = p(request).get(song_id)
        if t is not None and t.spec is not None:
            return t.spec, t.source
        found = lib_.history_spec(song_id)
        if found is None:
            raise HTTPException(404, "unknown song")
        return found

    def save(request: Request, track_or_song_id: str, liked: bool) -> tuple[dict, bool]:
        """Store a track (seed + spec), liked or just for a playlist. Returns (entry, already)."""
        t = p(request).get(track_or_song_id)
        if t is not None and t.spec is None:
            raise HTTPException(409, "track is still being composed")
        spec, source = find_spec(request, track_or_song_id)
        return lib_of(request).save(spec, source, liked=liked)

    def need_feature(request: Request, name: str, what: str) -> dict:
        user = acct.current_user(request)
        if not acct.has_feature(user, name):
            raise HTTPException(403, f"{what} is turned off for this account")
        return user

    # --- radio / player ---------------------------------------------------

    @app.get("/genres")
    def genres(request: Request):
        """The radios a request can name (e.g. `genre` in POST /genre or /playlists/{id}/generate),
        with each one's display name and tempo range in bpm, and your AI backend."""
        user = getattr(request.state, "user", None)
        s = users.ai(user["id"], user)[0] if user else ai.AISettings()
        return {"genres": list(GENRES),
                "details": [{"id": g, "name": g.replace("-", " ").capitalize(), "tempo_bpm": list(GENRE_TEMPO[g])}
                            for g in GENRES],
                "ai": {"backend": s.backend, "model": s.effective_model}}

    @app.get("/state")
    def state(request: Request):
        """What plays now and next, and the play mode (radio, playlist, shuffle, repeat).

        Every player action below answers with this same state."""
        return p(request).state()

    @app.get("/queue")
    def queue(request: Request):
        """Now playing and what comes next: a playlist's remaining queue (`pos` per song, `repeats`
        when it loops), or for a radio / Shuffle+ just the next song."""
        return p(request).queue()

    @app.post("/queue/jump")
    def queue_jump(request: Request, body: JumpBody):
        """Play the playlist queue from this position."""
        if not listening(request).jump(body.pos):
            raise HTTPException(409, "nothing to play at that position")
        return p(request).state()

    @app.post("/previous")
    def previous(request: Request):
        """Back one song (the previous one in the queue, or on a radio the last one played)."""
        moved = listening(request).previous()
        return {"moved": moved, **p(request).state()}

    @app.get("/history")
    def history(request: Request, limit: int = Query(default=50, ge=1, le=200)):
        """Persisted play history (survives restarts), newest first."""
        return {"history": lib_of(request).history(limit)}

    @app.post("/genre")
    def genre(request: Request, body: GenreBody):
        """Play a genre's radio (optionally steered by a mood)."""
        p(request).play_genre(body.genre, clean_mood(body.mood))
        return p(request).state()

    @app.post("/prompt")
    def prompt(request: Request, body: PromptBody):
        """Free text -> a new playlist of songs that follow the prompt, played right away.

        With your AI on, one call returns a title and a brief per song (it may pick the radio); otherwise
        the prompt's words pick the radio. Songs are stored as seed + spec and rendered as they play.
        """
        text = clean_mood(body.text)
        if not text:
            raise HTTPException(400, "empty prompt")
        lib_ = lib_of(request)
        s, ai_keys = users.ai(uid(request), acct.current_user(request))
        use_ai = s.backend != "off"
        genre = None if use_ai else procedural.genre_from_prompt(text)
        count = body.count or load_app_settings(lib_).prompt_count
        title, songs = ai.make_playlist(genre, text, count, use_ai, settings=s, creds=ai_keys)
        made_by_ai = any(source != "procedural" for _, source in songs)
        pl = lib_.create_playlist(title, source="ai" if made_by_ai else None, prompt=text)
        for spec, source in songs:
            entry, _ = lib_.save(spec, source)
            lib_.add_to_playlist(pl["id"], entry["id"])
        p(request).play_playlist(pl["id"])
        return {"playlist": lib_.playlist(pl["id"]), "genre": songs[0][0].genre, **p(request).state()}

    @app.post("/playlists/{pl_id}/generate")
    def generate_into_playlist(request: Request, pl_id: str, body: GenerateBody | None = None):
        """Make new songs and append them to a playlist (with your AI on, in the playlist's vibe).

        A fresh random seed is picked for each song; `genre` pins the radio (e.g. a chat request).
        If the playlist is playing, the new songs join its queue.
        """
        body = body or GenerateBody()
        lib_ = lib_of(request)
        pl = lib_.playlist(pl_id) if pl_id != LIKED_ID else None
        if pl is None:
            raise HTTPException(404, "unknown playlist")
        added = add_generated(request, pl, body.genre, clean_mood(body.prompt), body.count)
        return {"added": added, "playlist": lib_.playlist(pl_id)}

    def add_generated(request: Request, pl: dict, genre: Optional[str], prompt: Optional[str],
                      count: int) -> list[dict]:
        pl_id = pl["id"]
        lib_ = lib_of(request)
        text = prompt or pl.get("prompt") or pl["name"]
        s, ai_keys = users.ai(uid(request), acct.current_user(request))
        use_ai = s.backend != "off"
        genre = genre or (None if use_ai else procedural.genre_from_prompt(text))
        existing = [{"name": s_["name"], "genre": s_["genre"]} for s_ in pl["songs"]]
        _, songs = ai.make_playlist(genre, text, count, use_ai, existing=existing, settings=s, creds=ai_keys)
        added = []
        for spec, source in songs:
            entry, _ = lib_.save(spec, source)
            lib_.add_to_playlist(pl_id, entry["id"])
            added.append(entry)
        p(request).playlist_grew(pl_id, [e["id"] for e in added])
        return added

    @app.post("/random")
    def random_(request: Request, body: RandomBody | None = None):
        """Play a random radio that changes genre every song."""
        p(request).play_random(clean_mood(body.mood) if body else None)
        return p(request).state()

    @app.post("/skip")
    def skip(request: Request, body: SkipBody | None = None):
        key = getattr(request.state, "api_key", None)
        if key and "control" not in key["scopes"] and not (body and body.from_id):
            raise HTTPException(403, "a speaker key can only advance at the end of the playing song (from_id)")
        moved = listening(request).skip(body.from_id if body else None,
                                manual=body is None or body.manual or not body.from_id)
        return {"skipped": moved, **p(request).state()}

    # --- live sync: SSE, speaker and remote control -----------------------------

    def account_public(user: dict) -> dict:
        return {k: user.get(k) for k in ("id", "email", "role", "features")}

    @app.get("/events")
    async def events(request: Request, client: str = Query(min_length=1, max_length=64),
                     kind: Literal["overlay", "site"] = "site",
                     name: Optional[str] = Query(default=None, max_length=40), audio: bool = True):
        """Server-Sent Events: a full versioned snapshot of your player now and after every change.

        Each snapshot also has `account` (id, email, role, features) so pages follow feature changes.
        A stream ends when its key is revoked, the feature it needs is turned off or the account is removed.
        """
        user = acct.current_user(request)
        user_id = user["id"]
        player_ = await asyncio.to_thread(users.player, user_id)
        key = getattr(request.state, "api_key", None)
        if kind == "overlay" and key is not None and key.get("kind") != "overlay":
            raise HTTPException(403, "overlays need an overlay key, not an API key")
        if kind == "overlay" and not acct.has_feature(user, "overlay"):
            raise HTTPException(403, "Overlay access is turned off for this account")
        feature = ("overlay" if kind == "overlay" or (key and key.get("kind") == "overlay")
                   else "api" if key else None)
        label = " ".join(name.split())[:40] if name else None
        player_.connect_client(client, kind, label or None, audio)

        session = None if key else request.cookies.get(acct.COOKIE)

        async def stream():
            last, seen, account = -1, player_.access_rev, user
            try:
                while not await request.is_disconnected():
                    version = await asyncio.to_thread(player_.wait_change, last, app.state.sse_ping)
                    if player_.closed or (key and key["id"] in keys.revoked):
                        break  # account removed or key revoked while connected: end the stream now
                    if player_.access_rev != seen:  # features changed (or a restore): may it go on?
                        seen = player_.access_rev
                        fresh = await asyncio.to_thread(users.user, user_id)
                        if fresh is None or (feature and not acct.has_feature(fresh, feature)):
                            break
                        if key and not await asyncio.to_thread(keys.active, key["id"]):
                            break
                        if session and accts.login and not await asyncio.to_thread(accts.session_user, session):
                            break  # signed out (e.g. the admin set a new password): the page shows the login
                        account = fresh
                    if version == last:
                        if key and not await asyncio.to_thread(keys.active, key["id"]):
                            break
                        yield ": ping\n\n"  # keep proxies and the client from timing out
                        continue
                    snap = player_.live()
                    snap["account"] = account_public(account)
                    last = snap["version"]
                    yield f"event: state\nid: {last}\ndata: {json.dumps(snap, separators=(',', ':'))}\n\n"
            finally:
                player_.disconnect_client(client)  # releases the speaker role if this was the last stream

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/speaker/claim")
    def claim_speaker(request: Request, body: ClaimBody):
        return {"speaker": listening(request).claim_speaker(body.client, body.kind, body.force)}

    @app.post("/speaker/assign")
    def assign_speaker(request: Request, body: IdBody):
        """Play on another connected player (an overlay, or this tab via its client id)."""
        try:
            return {"speaker": p(request).assign_speaker(body.id)}
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/playback")
    def playback(request: Request, body: PlaybackBody):
        """The speaker reports what it is doing so remotes can show the same position."""
        if not listening(request).report_playback(body.client, body.track_id, body.state, body.position, body.volume):
            raise HTTPException(409, "this client is not the speaker")
        return {"ok": True}

    @app.post("/control")
    def control(request: Request, body: ControlBody):
        """Remote control for the speaker: pause, resume, toggle, seek, volume."""
        if body.action == "seek" and body.t is None:
            raise HTTPException(422, "seek needs t (seconds)")
        if body.action == "volume" and body.volume is None:
            raise HTTPException(422, "volume needs volume (0..1)")
        try:
            return {"command": listening(request).send_command(body.action, body.t, body.volume)}
        except LookupError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/repeat")
    def repeat(request: Request, body: RepeatBody):
        """Repeat: off, all (a playlist loops) or one (the same song again)."""
        p(request).set_repeat(body.mode)
        return p(request).state()

    @app.get("/audio/{track_id}.mp3")
    def audio(request: Request, track_id: str):
        """The MP3 of a track that is playing or queued (status ready) on your player."""
        player_ = listening(request)
        t = player_.get(track_id)
        if t is None or t.status != "ready":
            raise HTTPException(404, "no such audio (yet)")
        path = player_.engine.mp3_path(t.spec)
        if not path.exists():  # pruned meanwhile
            path = player_.engine.ensure_mp3(t.spec)
        return FileResponse(path, media_type="audio/mpeg")

    @app.post("/download/{song_id}")
    def download(request: Request, song_id: str, body: DownloadBody | None = None):
        """Render on the server (cached) and send the file."""
        need_feature(request, "download", "Downloads")
        fmt = body.format if body else "mp3"
        spec, _ = find_spec(request, song_id)
        name = (lib_of(request).get(song_id) or {}).get("name") or spec.name
        engine = p(request).engine
        path = engine.render(spec) if fmt == "wav" else engine.ensure_mp3(spec)
        filename = f"{safe_filename(name)} ({spec.genre}).{fmt}"
        return FileResponse(path, media_type="audio/mpeg" if fmt == "mp3" else "audio/wav", filename=filename)

    # --- library ------------------------------------------------------------

    @app.post("/like")
    def like(request: Request, body: IdBody):
        """Save a track or song to Liked songs."""
        entry, already = save(request, body.id, liked=True)
        return {"entry": entry, "already": already}

    @app.get("/library")
    def library_(request: Request):
        """Liked songs, newest first."""
        return {"library": lib_of(request).entries()}

    @app.patch("/library/{song_id}")
    def rename_song(request: Request, song_id: str, body: NameBody):
        """Rename a saved song."""
        return {"entry": lib_of(request).rename_song(song_id, body.name)}

    @app.delete("/library/{song_id}")
    def unlike(request: Request, song_id: str):
        """Remove from Liked songs (kept while a playlist still uses it)."""
        lib_of(request).unlike(song_id)
        return {"ok": True}

    @app.delete("/songs/{song_id}")
    def delete_song(request: Request, song_id: str):
        """Delete a song everywhere in your library: seed + spec, playlist entries and history.

        `song_id` is a saved-song id (the spec hash) or a track id from the queue/history. The cached
        audio goes too unless another user still has the song or it is playing somewhere.
        """
        lib_, player_ = lib_of(request), p(request)
        if lib_.get(song_id) is not None or SONG_HASH.fullmatch(song_id):
            spec_hash = song_id
        else:
            spec, _ = find_spec(request, song_id)
            spec_hash = spec.audio_hash()
        removed = lib_.delete_song(spec_hash)
        in_use = player_.forget(spec_hash)
        if not removed["saved"] and not removed["history"] and not in_use:
            raise HTTPException(404, "unknown song")
        if not in_use and not lib_.saved_by_anyone(spec_hash) and not any(o.uses(spec_hash) for o in users.all()):
            player_.pool.submit(player_.engine.forget, spec_hash)  # else the cache prunes it later
        return {"deleted": removed}

    @app.post("/play")
    def play(request: Request, body: IdBody):
        """Play a saved song (song id) or replay a played track (track id)."""
        if lib_of(request).get(body.id) is not None:
            try:
                p(request).play_saved(body.id)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
        else:
            spec, source = find_spec(request, body.id)
            p(request).play_spec(spec, source)
        return p(request).state()

    # --- playlists ----------------------------------------------------------

    @app.get("/playlists")
    def playlists(request: Request):
        """All your playlists (without their songs)."""
        return {"playlists": lib_of(request).playlists()}

    @app.post("/playlists")
    def create_playlist(request: Request, body: NewPlaylistBody):
        """Create an empty playlist."""
        lib_ = lib_of(request)
        pl = lib_.create_playlist(body.name)
        if body.song_id:
            entry, _ = save(request, body.song_id, liked=False)
            pl = lib_.add_to_playlist(pl["id"], entry["id"])
        return {"playlist": pl}

    @app.get("/playlists/{pl_id}")
    def playlist(request: Request, pl_id: str):
        """One playlist with its songs. The id `liked` is Liked songs."""
        pl = lib_of(request).playlist(pl_id)
        if pl is None:
            raise HTTPException(404, "unknown playlist")
        return {"playlist": pl}

    @app.patch("/playlists/{pl_id}")
    def rename_playlist(request: Request, pl_id: str, body: NameBody):
        """Rename a playlist."""
        return {"playlist": lib_of(request).rename_playlist(pl_id, body.name)}

    @app.delete("/playlists/{pl_id}")
    def delete_playlist(request: Request, pl_id: str):
        """Delete a playlist (songs that are liked or in another playlist are kept)."""
        lib_of(request).delete_playlist(pl_id)
        return {"ok": True}

    @app.post("/playlists/{pl_id}/songs")
    def add_song(request: Request, pl_id: str, body: IdBody):
        """Add a track or saved song; unsaved tracks are saved first."""
        lib_ = lib_of(request)
        if lib_.playlist(pl_id) is None:
            raise HTTPException(404, "unknown playlist")
        entry, _ = save(request, body.id, liked=False)
        return {"playlist": lib_.add_to_playlist(pl_id, entry["id"]), "entry": entry}

    @app.delete("/playlists/{pl_id}/songs/{song_id}")
    def remove_song_from_playlist(request: Request, pl_id: str, song_id: str):
        """Remove a song from a playlist."""
        return {"playlist": lib_of(request).remove_from_playlist(pl_id, song_id)}

    @app.put("/playlists/{pl_id}/order")
    def reorder(request: Request, pl_id: str, body: OrderBody):
        """Set the playlist order (exactly its song ids)."""
        return {"playlist": lib_of(request).reorder_playlist(pl_id, body.song_ids)}

    @app.post("/playlists/{pl_id}/shuffle")
    def shuffle_playlist(request: Request, pl_id: str, body: ShuffleBody):
        """Switch shuffle for the playing playlist without restarting the current song."""
        try:
            p(request).set_playlist_shuffle(pl_id, body.mode)
        except LookupError as e:
            raise HTTPException(409, str(e)) from e
        return p(request).state()

    @app.post("/playlists/{pl_id}/play")
    def play_playlist(request: Request, pl_id: str, body: PlayPlaylistBody | None = None):
        body = body or PlayPlaylistBody()
        try:
            p(request).play_playlist(pl_id, body.start, body.shuffle or body.plus, plus=body.plus)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return p(request).state()

    # --- settings & backups (each user's own) ------------------------------------

    def settings_payload(user: dict) -> dict:
        """The user's settings. `ai` only with the AI feature, `backup` only with the backup feature;
        keys only as set / not set."""
        user_id = user["id"]
        out = {"account": {k: user.get(k) for k in ("id", "email", "role")},
               "app": load_app_settings(users.lib(user_id)).model_dump()}
        own_keys = users.keys(user_id)
        if acct.has_feature(user, "ai"):
            out["ai"] = users.ai_settings(user_id).public(own_keys)
            if not accts.is_primary(user_id):  # the CLI runs as the server's own login
                out["ai"]["backends"] = [b for b in out["ai"]["backends"] if b != "claude-cli"]
        if acct.has_feature(user, "backup"):
            st = users.backup_settings(user_id)
            out["backup"] = {**st.fields(), "bucket": st.bucket or None, "enabled": st.enabled,
                             "keys": {k: own_keys.status(k) for k in S3_KEYS}}
        return out

    def save_keys(user_id: str, values: dict) -> None:
        try:
            creds.update(values, user_id)
        except credentials.CredentialsError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/settings")
    def settings(request: Request):
        """Your settings (keys and secrets only as set / not set)."""
        return settings_payload(acct.current_user(request))

    @app.put("/settings/ai")
    def update_ai(request: Request, body: AISettingsBody):
        """Your AI backend, model, share of radio songs and provider keys (only ever used for you)."""
        user = need_feature(request, "ai", "AI")
        user_id = user["id"]
        changes = body.model_dump(exclude_none=True)
        ai_keys = {k: changes.pop(k, None) for k in AI_KEYS}
        try:
            new = ai.AISettings(**{**_fields(users.ai_settings(user_id)), **changes})
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        if new.backend == "claude-cli" and not accts.is_primary(user_id):
            raise HTTPException(400, "the claude CLI backend is only for the server's admin; "
                                     "use claude-api or openai with your own key")
        save_keys(user_id, ai_keys)
        users.lib(user_id).set_user_setting("ai", _fields(new))
        player_ = users.existing(user_id)
        if player_ is not None:
            player_.claude_ratio = users.ratio(user_id)
        return settings_payload(user)

    @app.post("/settings/ai/test")
    def test_ai(request: Request):
        user = need_feature(request, "ai", "AI")
        return ai.test_backend(*users.ai(user["id"], user))

    @app.put("/settings/app")
    def update_app(request: Request, body: AppSettingsBody):
        lib_ = lib_of(request)
        try:
            new = AppSettings(**{**load_app_settings(lib_).model_dump(), **body.model_dump(exclude_none=True)})
        except ValidationError as e:
            raise HTTPException(400, e.errors()[0]["msg"]) from e
        lib_.set_user_setting("app", new.model_dump())
        if body.repeat is not None:
            p(request).set_repeat(new.repeat)  # the default applies right away too
        return settings_payload(acct.current_user(request))

    @app.put("/settings/backup")
    def update_backup(request: Request, body: BackupSettingsBody):
        """Your S3 bucket and schedule for backups of your own data (applied immediately)."""
        user = need_feature(request, "backup", "Backups")
        user_id = user["id"]
        changes = body.model_dump(exclude_none=True)
        s3_keys = {k: changes.pop(k, None) for k in S3_KEYS}
        try:
            new = storage.BackupSettings(**{**users.backup_settings(user_id).fields(), **changes})
        except (TypeError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        save_keys(user_id, s3_keys)
        users.lib(user_id).set_user_setting("backup", new.fields())
        users.schedule(user_id)
        return settings_payload(user)

    @app.post("/settings/backup/test")
    def test_backup(request: Request):
        user_id = need_feature(request, "backup", "Backups")["id"]
        return storage.test_connection(users.backup_settings(user_id), users.keys(user_id))

    def backup_store(request: Request) -> tuple[str, storage.BackupStore]:
        user_id = need_feature(request, "backup", "Backups")["id"]
        store = users.backups(user_id)
        if store is None:
            raise HTTPException(400, "backups are not set up (Settings → Backups)")
        return user_id, store

    @app.get("/backups")
    def list_backups(request: Request):
        """Your backups in your bucket, newest first."""
        user_id = need_feature(request, "backup", "Backups")["id"]
        if not users.backup_settings(user_id).enabled:
            return {"enabled": False, "backups": []}
        return {"enabled": True, "backups": backup_store(request)[1].list()}

    @app.post("/backups")
    def backup(request: Request):
        """Back up your data now (library, playlists, history, settings, keys)."""
        user_id, store = backup_store(request)
        return {"backup": store.backup(lib.db, user_id)}

    @app.post("/backups/restore")
    def restore(request: Request, body: RestoreBody):
        """Replace your data with a backup (yours, or a whole-database backup of an older version).
        Your backup settings and S3 keys stay; nobody else's data changes."""
        user_id, store = backup_store(request)
        before = {k["id"] for k in keys.list(user_id)}
        with lib.lock:
            counts = store.restore(body.key, lib.db, user_id)
        keys.revoked.update(before - {k["id"] for k in keys.list(user_id)})
        player_ = users.existing(user_id)
        if player_ is not None:
            player_.claude_ratio = users.ratio(user_id)
            player_.access_changed()  # streams on replaced keys end
        return {"ok": True, "restored": counts}

    @app.get("/settings/keys")
    def list_keys(request: Request):
        """Your overlay and API keys (never the tokens)."""
        return {"keys": keys.list(uid(request))}

    @app.post("/settings/keys")
    def create_key(request: Request, body: NewKeyBody):
        """Create an overlay key (kind overlay) or an API key with scopes; the token is returned once.
        The key acts as you."""
        user = need_feature(request, "overlay" if body.kind == "overlay" else "api",
                            "Overlay access" if body.kind == "overlay" else "API access")
        try:
            key, token = keys.create(body.name, body.scopes, body.kind, user_id=user["id"])
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"key": key, "token": token}

    @app.delete("/settings/keys/{key_id}")
    def revoke_key(request: Request, key_id: str):
        """Revoke one of your keys; streams using it close right away."""
        user_id = uid(request)
        if not keys.revoke(key_id, user_id):
            raise HTTPException(404, "unknown key")
        player_ = users.existing(user_id)
        if player_ is not None:
            with player_.changed:  # wake every stream so one using this key ends right away
                player_.changed.notify_all()
        return {"ok": True}

    # --- accounts -------------------------------------------------------------

    def account_error(e: acct.AccountError) -> HTTPException:
        return HTTPException(e.status, str(e))

    def set_session(request: Request, response: Response, token: str) -> None:
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
        response.set_cookie(acct.COOKIE, token, max_age=int(accts.session_seconds), path="/",
                            httponly=True, samesite="lax", secure=secure)

    def link_for(request: Request, token: str) -> str:
        base = config.PUBLIC_URL or str(request.base_url).rstrip("/")
        return f"{base}/#/welcome?token={token}"

    @app.get("/auth/me")
    def me(request: Request):
        """Who is signed in, and whether this server uses logins at all (`login: false` = local mode)."""
        return {"user": acct.current_user(request), "login": accts.login}

    @app.post("/auth/login")
    def login(request: Request, response: Response, body: LoginBody):
        """Sign in; sets the session cookie. Failed attempts are rate limited per email and address."""
        try:
            accts.require_login_mode()
            user, token = accts.login_user(body.email, body.password, request.client.host if request.client else "")
        except acct.AccountError as e:
            raise account_error(e) from None
        set_session(request, response, token)
        return {"user": user}

    @app.post("/auth/logout")
    def logout(request: Request, response: Response):
        token = request.cookies.get(acct.COOKIE)
        if token and accts.login:
            accts.end_session(token)
        response.delete_cookie(acct.COOKIE, path="/")
        return {"ok": True}

    @app.get("/auth/token/{token}")
    def token_info(token: str):
        """What an invite or reset link is for (`kind` invite | reset); 404 when unknown, used or expired."""
        info = accts.peek_token(token) if accts.login else None
        if info is None:
            raise HTTPException(404, "this link is invalid or has expired")
        return info

    @app.post("/auth/token")
    def use_token(request: Request, response: Response, body: SetPasswordBody):
        """Set a password from an invite or reset link, and sign in."""
        try:
            accts.require_login_mode()
            user, token = accts.use_token(body.token, body.password)
        except acct.AccountError as e:
            raise account_error(e) from None
        set_session(request, response, token)
        return {"user": user}

    @app.post("/auth/password")
    def change_password(request: Request, body: ChangePasswordBody):
        """Change your own password (other sessions are signed out)."""
        user = acct.current_user(request)
        if getattr(request.state, "api_key", None) is not None:
            raise HTTPException(403, "not with an API key")
        try:
            accts.require_login_mode()
            accts.change_password(user["id"], body.current, body.new, request.cookies.get(acct.COOKIE))
        except acct.AccountError as e:
            raise account_error(e) from None
        return {"ok": True}

    def members_only_in_login_mode() -> None:
        try:
            accts.require_login_mode()
        except acct.AccountError as e:
            raise account_error(e) from None

    def send_link(request: Request, email: str, token: str, kind: str) -> dict:
        """Email the link; hand it back to the admin only when it couldn't be sent."""
        link = link_for(request, token)
        sent = (mailer.send_invite if kind == "invite" else mailer.send_reset)(email, link)
        return {"sent": sent, "link": None if sent else link}

    @app.get("/users", dependencies=ADMIN)
    def list_users():
        """Members, pending invites and whether email sending is configured."""
        members_only_in_login_mode()
        return {"users": accts.users(), "invites": accts.invites(), "smtp": mailer.configured()}

    @app.post("/users/invite", dependencies=ADMIN)
    def invite(request: Request, body: InviteBody):
        """Invite by email (48 h link). `link` is returned when the email couldn't be sent."""
        members_only_in_login_mode()
        try:
            inv, token = accts.invite(body.email, body.features.model_dump(exclude_none=True) if body.features else None)
        except acct.AccountError as e:
            raise account_error(e) from None
        return {"invite": inv, **send_link(request, inv["email"], token, "invite")}

    @app.post("/invites/{invite_id}/resend", dependencies=ADMIN)
    def resend_invite(request: Request, invite_id: str):
        members_only_in_login_mode()
        try:
            inv, token = accts.resend_invite(invite_id)
        except acct.AccountError as e:
            raise account_error(e) from None
        return {"invite": inv, **send_link(request, inv["email"], token, "invite")}

    @app.delete("/invites/{invite_id}", dependencies=ADMIN)
    def revoke_invite(invite_id: str):
        members_only_in_login_mode()
        try:
            accts.revoke_invite(invite_id)
        except acct.AccountError as e:
            raise account_error(e) from None
        return {"ok": True}

    @app.post("/users/{user_id}/reset", dependencies=ADMIN)
    def reset_password(request: Request, user_id: str):
        """Password reset link (1 h) for a user; `link` when it couldn't be emailed."""
        members_only_in_login_mode()
        try:
            email, token = accts.reset_token(user_id)
        except acct.AccountError as e:
            raise account_error(e) from None
        return send_link(request, email, token, "reset")

    @app.put("/users/{user_id}/password", dependencies=ADMIN)
    def set_user_password(request: Request, user_id: str, body: MemberPasswordBody):
        """Set a member's password yourself (they are signed out everywhere)."""
        members_only_in_login_mode()
        try:
            accts.set_password(user_id, body.password, by=acct.current_user(request)["id"])
        except acct.AccountError as e:
            raise account_error(e) from None
        users.access_changed(user_id)  # open pages get a 401 and show the login
        return {"ok": True}

    @app.patch("/users/{user_id}", dependencies=ADMIN)
    def update_user(request: Request, user_id: str, body: UserPatchBody):
        """Turn a member's features (download, ai, api, overlay) on or off."""
        members_only_in_login_mode()
        try:
            user = accts.set_features(user_id, body.features.model_dump(exclude_none=True),
                                      by=acct.current_user(request)["id"])
        except acct.AccountError as e:
            raise account_error(e) from None
        users.access_changed(user_id)  # open streams, AI and backups follow at once
        return {"user": user}

    @app.delete("/users/{user_id}", dependencies=ADMIN)
    def delete_user(request: Request, user_id: str):
        """Remove a member and everything they own (their player and open streams stop)."""
        members_only_in_login_mode()
        try:
            accts.delete_user(user_id, by=acct.current_user(request)["id"])
        except acct.AccountError as e:
            raise account_error(e) from None
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
        mimetypes.add_type("application/manifest+json", ".webmanifest")
        app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")
    return app


def _fields(s: "ai.AISettings") -> dict:
    return asdict(s)


def load_app_settings(lib: Library) -> AppSettings:
    """The library's user's app settings."""
    try:
        return AppSettings(**(lib.user_setting("app") or {}))
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
    """Cue what Settings -> App asks for on start (of the server, or of a user's player): the last
    played song (if it can be rebuilt), a random radio, a specific radio, or nothing. Also applies the
    default repeat mode.

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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="motif", description="Local generative radio")
    ap.add_argument("genre", nargs="?", help="genre for --dry-run (default: random)")
    ap.add_argument("--dry-run", action="store_true", default=os.environ.get("MOTIF_DRY_RUN") == "1",
                    help="generate a spec and print it without rendering (env MOTIF_DRY_RUN=1)")
    ap.add_argument("--mood", help="mood/prompt text for --dry-run")
    ap.add_argument("--seed", type=int, help="seed for --dry-run (procedural)")
    ap.add_argument("--port", type=int, default=config.PORT)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.dry_run:
        genre = args.genre or random.choice(GENRES)
        if genre not in GENRES:
            ap.error(f"unknown genre {genre!r}; choose from {', '.join(GENRES)}")
        mood = clean_mood(args.mood)
        t0 = time.time()
        if args.seed is not None:
            spec, source = procedural.generate(genre, mood, seed=args.seed), "procedural"
        else:
            spec, source = make_spec(genre, mood, False)
        print(json.dumps({"source": source, "seconds": round(time.time() - t0, 2), "hash": spec.audio_hash(),
                          "minutes": round(spec.music_seconds / 60, 2), "spec": spec.model_dump(mode="json")},
                         indent=2))
        return 0

    import uvicorn

    if config.HOST not in ("127.0.0.1", "localhost", "::1"):
        if not config.ADMIN_EMAIL:
            log.warning("listening on %s without logins (MOTIF_ADMIN_EMAIL unset): anyone who can reach this port "
                        "can control it and use the saved API keys. Set an admin or keep it behind localhost.",
                        config.HOST)
    logging.getLogger("uvicorn.access").addFilter(RedactKeyFilter())
    uvicorn.run(create_app(), host=config.HOST, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
