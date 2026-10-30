# Motif

A local generative radio you open in the browser. The Python backend composes original 3–5 minute songs, renders them with FluidSynth and the browser plays one song straight into the next.

```
SongSpec (JSON, pydantic) -> sections x phrases -> notes -> MIDI (mido) -> fluidsynth -> WAV + ring-out -> ffmpeg MP3
```

Nothing is pre-recorded or downloaded. Every song is composed by code from a seed. The soundfont supplies only the instrument sounds.

## Features

- Spotify-style UI (Material icons, bundled as inline SVG so it works offline): radios (one per procedural genre), a prompt bar, playlists, liked songs, a queue, repeat off / all / one, an output device picker and a settings page.
- The prompt bar creates a playlist: one prompt gives 8 songs that follow it (named after the prompt, or titled by the AI), and starts playing it.
- Songs last 3–5 minutes and have sections: intro, main, variation, breakdown, build, return, outro.
- Songs in your library (liked songs and songs in playlists) are stored as seed + spec JSON in SQLite (`library/motif.db`), not as audio. Audio is re-rendered on demand and is identical for the same spec. Adding a song to a playlist does not like it; unliking keeps it while a playlist uses it.
- Download as MP3 or WAV. The file is rendered on the server (WAV can take 5–10 s).
- The AI never writes a song. It returns a small brief (a seed and a few choices such as name, key, tempo, energy, instruments) and the procedural generator builds the song from it.
- Optional backups of your own library to your own S3-compatible bucket, with restore.
- Optional accounts: an admin from the environment invites members by email; every member has their own player, library, AI and backups (see [Accounts](#accounts)).

## AI backends

AI is set up in the app only, per user (Settings → AI): backend, per-backend model, OpenAI-compatible base URL (with presets for OpenAI, Ollama, OpenRouter, LM Studio and Groq) and API keys. There are no AI environment variables; every user, the admin included, starts with AI off. Settings are kept in the database; API keys are stored in the same database, encrypted with AES-256-GCM. The master key is never in the database: it comes from `MOTIF_SECRET_KEY` (base64, 32 bytes) or, if that is unset, from `library/secret.key` (created once, mode 600). A copied database or an S3 backup therefore only holds ciphertext; keep the master key to restore saved keys from a backup (without it they show as not set and can be re-entered). The page only ever shows whether a key is set, never its value. A user's AI calls only ever use that user's own keys.

| Backend | Needs |
|---|---|
| Off (procedural only) | nothing |
| Claude CLI | the `claude` binary on PATH and its login (local runs, admin only: it uses the server's own login) |
| Claude API | an Anthropic API key |
| OpenAI-compatible | a base URL, a model and (except for local servers) an API key |

Base URL examples: OpenAI `https://api.openai.com/v1`, Ollama `http://localhost:11434/v1` (from Docker `http://host.docker.internal:11434/v1`), OpenRouter `https://openrouter.ai/api/v1`.

A prompt always asks the AI, in one call for the whole playlist (a title plus a brief per song; invalid briefs are skipped and missing songs are made procedurally); automatic radio songs ask it at the user's **AI share of radio**. The reply is JSON, validated by pydantic (`SongBrief`, extra fields rejected). Any error or timeout falls back to procedural generation. Nothing the model returns is executed. The Settings page has a test button (`POST /settings/ai/test`).

## Settings page

**AI generation.** The AI only picks a short brief per song (genre, key, tempo, instruments, name); the procedural engine writes every note.

| Backend | Fields | Notes |
|---|---|---|
| Off | none | Procedural only, no AI calls. |
| Claude CLI | CLI model | Uses the local `claude` command and its login; no key. The model is an alias such as `haiku` or `sonnet`. |
| Claude API | Model, Anthropic API key | Calls the Anthropic API. |
| OpenAI-compatible | Base URL (presets for OpenAI, Ollama, OpenRouter, LM Studio, Groq), Model, OpenAI API key | Any `/chat/completions` server. The key is optional for local servers such as Ollama or LM Studio; for Ollama the model is the name of a pulled model. |

- An empty model field uses the default for that backend.
- **AI share of radio**: the fraction of automatic radio songs that ask the AI for a brief. Prompted playlists always use the AI when a backend is on.
- **Timeout** (1–120 s): a slower reply falls back to a procedural song.
- **Test** checks the saved settings, so save first; it is disabled while a card has unsaved changes.

**Backups (S3).** Your own bucket for backups of your own data (needs the Backups feature). Off: nothing leaves the server.

- **Endpoint URL**: only for S3-compatible stores such as MinIO or Cloudflare R2; leave empty for AWS.
- **Region**: optional.
- **Backup every (hours)**: automatic backups; `0` turns them off. Manual backups and restores are in the Backups card.
- **Test connection** checks the saved bucket settings (turn S3 on and save first).

API keys and access keys are write-only: after saving, the page only shows **Saved** or **Not set**, and **Clear** removes a saved key. How they are stored is described under [AI backends](#ai-backends).

## Accounts

Motif runs in one of two modes:

- **Local mode** (default, `MOTIF_ADMIN_EMAIL` unset): no login. Everything runs as one built-in admin user. Members and invites are off. Keep it on localhost.
- **Login mode** (`MOTIF_ADMIN_EMAIL` and `MOTIF_ADMIN_PASSWORD` set): every page and API call needs a session, except the sign-in page, invite/reset links, the overlay page (it uses an overlay key) and the API docs. The admin account is created or updated from these variables on every start; the environment password always wins, and changing it signs the admin out everywhere. The first start in login mode gives everything made in local mode (liked songs, playlists, history, settings, keys) to the admin.

**Invites.** In Settings → Members the admin invites people by email. The link (`<MOTIF_PUBLIC_URL>/#/welcome?token=…`) works once and expires after 48 hours; the invitee sets a password and is signed in. Without SMTP settings, or when sending fails, the page shows the link so you can send it yourself. The admin can resend or revoke pending invites, send a password reset link (1 hour), remove a member (with everything they own), and turn off a member's downloads, AI, API keys, overlays or backups. A switch takes effect at once: a member's open overlay or API streams end when that feature goes off.

**Per user.** Everyone has their own player (queue, speaker, live sync), liked songs, playlists, history, app settings, AI settings and keys, backups, and API / overlay keys. Song audio is cached once on the server by hash and shared; who saved what is per user. **Admin only:** members (Settings → Members).

**Rendering.** All players share one renderer: at most `MOTIF_RENDER_WORKERS` songs render at the same time; the rest wait. A player with no open page or overlay for 10 minutes stops preparing next songs until someone connects again.

**Sessions** are an HttpOnly, SameSite=Lax cookie that lasts `MOTIF_SESSION_DAYS` (marked Secure over HTTPS). Passwords are hashed with scrypt; only SHA-256 hashes of session and link tokens are stored. Failed sign-ins are rate limited per email and per address. API and overlay keys work with `Authorization: Bearer …` and act as the user who made them (their player and library).

```sh
MOTIF_ADMIN_EMAIL=you@example.com
MOTIF_ADMIN_PASSWORD=choose-a-long-password
MOTIF_PUBLIC_URL=https://motif.example.com
MOTIF_SMTP_HOST=smtp.example.com
MOTIF_SMTP_USER=motif@example.com
MOTIF_SMTP_PASSWORD=app-password
MOTIF_SMTP_FROM=Motif <motif@example.com>
```

## Database

SQLite by default: `library/motif.db`, no setup. To use PostgreSQL instead, set `MOTIF_DATABASE_URL`:

```sh
export MOTIF_DATABASE_URL='postgresql://motif:password@db.example:5432/motif'
```

- Tables are created on first start. If that Postgres database is empty and a local `library/motif.db` has data, the local library is copied over once.
- If the URL is set but the server can't be reached, Motif refuses to start instead of silently falling back to SQLite (which would split your data). Leave it unset to use SQLite.
- The password in the URL is never logged or shown; Settings → Database shows only the type and host.
- Backups are SQLite files of one user's data for both databases, so a backup taken on one restores into the other.
- Saved API keys stay encrypted in either database; the master key (`MOTIF_SECRET_KEY` or `library/secret.key`) is still needed to read them.

PostgreSQL tests (skipped unless a throwaway database is configured; they drop and recreate the tables):

```sh
docker compose --profile postgres up -d postgres
```

```sh
MOTIF_TEST_DATABASE_URL=postgresql://motif:motif@127.0.0.1:5432/motif .venv/bin/python -m pytest -q tests/test_postgres.py
```

## Backups

Works with AWS S3, MinIO, Cloudflare R2 and similar. Each user (with the Backups feature) sets their own bucket, prefix, endpoint, region, access keys and interval in Settings → Backups; there is no S3 configuration in the environment, and only the keys saved there are used. Song audio is never stored in S3: it is rendered on demand and cached on the server.

- A backup is a SQLite file with only that user's data (liked songs, playlists, history, app and AI settings, encrypted keys, API keys), uploaded to `<prefix>backups/motif-YYYYmmdd-HHMMSS.db`. Create, list and restore from the Settings page or the API (`GET /backups`, `POST /backups`, `POST /backups/restore`).
- A restore is verified (valid SQLite with the expected tables) and replaces only that user's data; the backup settings and S3 keys in use stay. Whole-database backups from older versions restore too (all their library rows go to the restoring user).
- Scheduled backups run per user at their interval (0 = manual only) and stop when the Backups feature is turned off.

## Docker

Needs Docker with Compose. The soundfont is not in the image (32 MB; its licence asks for a link to the author). Put one General MIDI `.sf2` in `./soundfonts`; it is mounted read-only.

```sh
cp .env.example .env
```

```sh
docker compose up --build
```

Open http://127.0.0.1:8000. The port is published on localhost only. Songs and the database live in the `motif-data` volume (`/data`). The `claude-cli` AI backend is not available in the container; use `claude-api` or `openai`.

Optional local S3 with MinIO (bound to 127.0.0.1, ports 9000 and 9001):

```sh
MINIO_ROOT_USER=motifadmin MINIO_ROOT_PASSWORD=choose-a-password docker compose --profile minio up --build
```

Create a bucket in the console at http://127.0.0.1:9001, then in Settings → Backups use bucket `motif`, endpoint `http://minio:9000` and the MinIO user and password as access keys.

## Local install

Requirements: Python 3.12, `fluidsynth`, `ffmpeg` (`brew install fluid-synth ffmpeg`) and one General MIDI `.sf2` in `soundfonts/`. The repo is set up for GeneralUser GS; its licence is in `soundfonts/GeneralUser-GS-LICENSE.txt`.

```sh
python3.12 -m venv .venv
```

```sh
.venv/bin/pip install -r requirements.txt
```

Serve on http://127.0.0.1:8000 (localhost only):

```sh
.venv/bin/python -m app.main
```

Another port:

```sh
.venv/bin/python -m app.main --port 8080
```

Keyboard: space = play/pause, `n` = skip, `l` = like.

Dry run (prints a spec and does not render):

```sh
.venv/bin/python -m app.main --dry-run lofi
```

```sh
.venv/bin/python -m app.main --dry-run jazz --seed 42 --mood "late, smoky"
```

Tests:

```sh
.venv/bin/python -m pytest -q
```

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `MOTIF_HOST` | `127.0.0.1` | Bind address (`0.0.0.0` in the Docker image) |
| `MOTIF_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Host names the browser may use; other `Host` headers and cross-site writes are refused |
| `MOTIF_PORT` | `8000` | Server port |
| `MOTIF_DATA_DIR` | project root | Base for `cache/` and `library/` (`/data` in Docker) |
| `MOTIF_CACHE_DIR` | `<data>/cache` | Rendered-audio cache |
| `MOTIF_LIBRARY_DIR` | `<data>/library` | Library directory |
| `MOTIF_DB` | `<library>/motif.db` | SQLite file |
| `MOTIF_DATABASE_URL` | unset | `postgresql://user:pass@host:5432/db` to use PostgreSQL instead of SQLite |
| `MOTIF_SECRET_KEY` | `library/secret.key` | Master key (base64 of 32 random bytes) that encrypts saved AI keys and S3 credentials; generate with `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"` |
| `MOTIF_SOUNDFONT_DIR` | `soundfonts/` | Directory searched for `*.sf2` (`/soundfonts` in Docker) |
| `MOTIF_SOUNDFONT` | first `*.sf2` | Use this soundfont file |
| `MOTIF_CACHE_MAX` | `12` | Songs kept in the cache, about 50 MB each (least recently used removed first) |
| `MOTIF_DRY_RUN` | unset | `1` = same as `--dry-run` |
| `MOTIF_ADMIN_EMAIL` | unset | Admin account; set = login mode (see [Accounts](#accounts)) |
| `MOTIF_ADMIN_PASSWORD` | unset | Admin password (required with `MOTIF_ADMIN_EMAIL`; applied on every start) |
| `MOTIF_PUBLIC_URL` | request URL | Base URL for links in invite and reset emails, e.g. `https://motif.example.com` |
| `MOTIF_SESSION_DAYS` | `30` | How long a sign-in lasts |
| `MOTIF_RENDER_WORKERS` | `2` | Songs rendered at the same time across all users (others wait) |
| `MOTIF_SMTP_HOST` | unset | SMTP server for invite/reset emails; unset = links are shown to the admin |
| `MOTIF_SMTP_PORT` | `587` | SMTP port (465 for `ssl`) |
| `MOTIF_SMTP_USER` / `MOTIF_SMTP_PASSWORD` | unset | SMTP login (skipped when no user is set) |
| `MOTIF_SMTP_FROM` | SMTP user | Sender address, e.g. `Motif <motif@example.com>` |
| `MOTIF_SMTP_TLS` | `starttls` | `starttls`, `ssl` or `none` |

AI and S3 have no environment variables: each user sets them in the app.

## Overlay and remote control

Motif can be driven from outside — a stream overlay, Streamer.bot / Firebot reacting to Twitch channel points, or any script — while one place plays the audio.

**Sync.** The server is the single source of truth. Every page and the overlay keep a Server-Sent Events stream open (`GET /events`) and receive a full, versioned snapshot whenever anything changes, so an API call (skip, change radio, play a playlist) shows up everywhere within milliseconds.

**Speaker and remotes.** Exactly one client plays audio: the *speaker*. An overlay always becomes the speaker; a site tab becomes it only when nothing else is playing, or with **Play here**. Every other page is a remote: it shows the speaker's position and sends pause/seek/volume as commands. Only the speaker hands over to the next song at the end of the last bar.

**Keys.** There are two kinds, each shown once on creation (only a hash is stored). A key belongs to the user who made it and controls that user's player; members manage their own keys. If the admin turns off a member's API or overlay feature, that member's keys of that kind get 403 and their open streams end.

- **API keys** (`mk_…`, Settings → API keys) for tools and bots, with only the scopes they need (below). They can't open an overlay.
- **Overlay keys** (`mo_…`, Settings → Overlays) for overlay pages: they play the audio, show what's playing and advance at a song's end, nothing else. Only overlay keys open an overlay.

| Scope | Allows |
|---|---|
| `read` | `GET /state`, `/events`, `/history`, `/library`, `/playlists`, `/playlists/{id}`, `/genres`, `/audio/{id}.mp3` |
| `control` | `POST /skip`, `/repeat`, `/previous`, `/queue/jump`, `/genre`, `/random`, `/play`, `/playlists/{id}/play`, `/playlists/{id}/shuffle`, `/like`, `/control` (60/min) |
| `request` | `POST /prompt`, `POST /playlists/{id}/generate` (6/min; makes AI calls when a backend is on) |

Settings, keys, backups, downloads and library edits are never reachable with a key. Send the key as a header:

```sh
curl -X POST http://127.0.0.1:8000/skip -H "Authorization: Bearer mk_..."
```

```sh
curl -X POST http://127.0.0.1:8000/genre -H "Authorization: Bearer mk_..." -H "Content-Type: application/json" -d '{"genre": "jazz"}'
```

```sh
curl -X POST http://127.0.0.1:8000/control -H "Authorization: Bearer mk_..." -H "Content-Type: application/json" -d '{"action": "toggle"}'
```

**Overlay.** Add a Browser Source in OBS (or open it in a tab) with the URL shown when you create one in Settings → Overlays:

```
http://127.0.0.1:8000/overlay?layout=card&pos=bl&key=mo_...
```

- Each overlay has its own overlay key (revoke it to disconnect just that overlay). The overlay removes `key` from the address bar, keeps it for the tab's session (so OBS refreshes still work), and sends it only as an `Authorization` header; the server masks `key=` in its access log. `#key=…` also works. Without a key the overlay doesn't connect, so revoking the key always stops it.
- `layout=card|bar|mini`, `pos=bl|br|tl|tr`, `progress=0|1`, `audio=1|0` (`0` = display only, no sound), `viz=1` (live audio bars in the art), `name=…` (label in the site's **Play on** menu).
- In OBS tick **Control audio via OBS** to give the music its own mixer fader, and turn off **Shutdown source when not visible** so scene switches don't stop the music. In a normal browser tab, click once to allow sound.

**Events stream** (`GET /events?client=<id>&kind=overlay|site`): `event: state` with `data` = `{version, state, speaker, playback, command, clients, library_rev, server_time, account}` for your own player, where `account` is `{id, email, role, features}` (sent again when the admin changes your features); a `: ping` comment every 15 s keeps idle connections open.

**Twitch channel points.** Point a reward in Streamer.bot / Firebot / SAMMI at an HTTP request: e.g. "Skip song" → `POST /skip`, "Switch radio" → `POST /genre` with the viewer's text as `genre`, "Request a vibe" → `POST /prompt` with `{"text": "<viewer input>"}`, "Add a song" → `POST /playlists/<id>/generate` with `{"genre": "<viewer pick>"}` (the list of genre ids is at `GET /genres`; open pages update live). Use a key with `control` (and `request` for prompts).

## API

JSON in and out; full interactive docs (grouped by player, queue, live, library, playlists, settings, keys, backups, account, members); everything acts on the signed-in user's (or key owner's) own player and data are at `/docs`. Errors are `{"detail": "..."}` with a 4xx status (422 for an invalid body, e.g. an unknown genre). `GET /state` returns `{now, up_next, mode, at_end, stopped}`, and every player action answers with the same state.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/genres` | | genre ids a request can name (with display name and bpm range) and the AI backend. Interactive docs for every endpoint: `/docs` |
| GET | `/state` | | the player state |
| GET | `/history` | | persisted play history |
| POST | `/genre` | `{genre, mood?}` | start a radio |
| POST | `/random` | `{mood?}` | shuffle radios |
| POST | `/prompt` | `{text, count?}` | create a playlist of `count` (default 8, 2–20) songs from the prompt and play it |
| POST | `/playlists/{id}/generate` | `{genre?, prompt?, count?}` | add `count` (1–10) new songs to a playlist; `genre` pins the radio, otherwise the AI picks in the playlist's vibe; joins the queue if it's playing |
| POST | `/skip` | `{from_id?, manual?}` | next song (`from_id` guards double skips; `manual` = Next pressed, so Shuffle+ plays a playlist song instead of waiting for a new one). At the end of a playlist with repeat off, the first song is cued and paused (`stopped` in the state) |
| POST | `/previous` | | previous song in the queue (radio / Shuffle+: the last one played); `moved: false` when there is none |
| GET | `/queue` | | the play list as `items` with the playing song at `current`: a playlist's whole queue in play order (with `pos`, and `repeats` when it loops), or for a radio / Shuffle+ the songs played this session, the current one and the next |
| POST | `/queue/jump` | `{pos}` | play the playlist queue from that position |
| POST | `/repeat` | `{mode}` | `off`, `all` or `one` |
| POST | `/like` | `{id}` | save a song |
| GET | `/library` | | saved songs, newest first |
| PATCH | `/library/{id}` | `{name}` | rename |
| DELETE | `/library/{id}` | | unlike (kept while a playlist uses it) |
| POST | `/play` | `{id}` | play a saved song or a track |
| GET | `/playlists` | | all playlists |
| POST | `/playlists` | `{name, song_id?}` | create |
| GET | `/playlists/{id}` | | playlist with songs |
| PATCH | `/playlists/{id}` | `{name}` | rename |
| DELETE | `/playlists/{id}` | | delete |
| POST | `/playlists/{id}/songs` | `{id}` | add a song (saves it if needed) |
| DELETE | `/playlists/{id}/songs/{song_id}` | | remove a song |
| PUT | `/playlists/{id}/order` | `{song_ids}` | reorder |
| POST | `/playlists/{id}/shuffle` | `{mode}`: `off`, `on` or `auto` | switch shuffle for the playing playlist without restarting the song (`auto` = Shuffle+) |
| POST | `/playlists/{id}/play` | `{start?, shuffle?, plus?}` | play the playlist; `plus` is Shuffle+ (AI playlists): a radio on the playlist; each next song is one of its songs or, at the AI share of radio, a new one from its prompt (played, not saved). The id `liked` plays Liked songs |
| POST | `/download/{id}` | `{format: mp3\|wav}` | file attachment, rendered on the server |
| GET | `/audio/{track_id}.mp3` | | audio of a ready track |
| GET | `/settings` | | your `account`, `app`, `ai` (with the AI feature) and `backup` (with the Backups feature) settings |
| PUT | `/settings/ai` | any of `backend, ratio, timeout, model, openai_model, cli_model, base_url, anthropic_api_key, openai_api_key` | full settings (keys: non-empty saves, `""` clears, omitted keeps; never returned) |
| POST | `/settings/ai/test` | | `{ok, source, seconds, brief?, error?}` |
| PUT | `/settings/app` | any of `startup, repeat, prompt_count, visualizer` | full settings |
| PUT | `/settings/backup` | any of `bucket, prefix, endpoint, region, backup_interval_hours, aws_access_key_id, aws_secret_access_key` | full settings; applied live; `bucket: ""` turns backups off |
| POST | `/settings/backup/test` | | `{ok, seconds, error?}` |
| GET | `/backups` | | `{enabled, backups}` (your backups) |
| POST | `/backups` | | `{backup}` |
| POST | `/backups/restore` | `{key}` | `{ok, restored}`; replaces only your data |

## How it works

- `app/schema.py`: `SongSpec`, the only contract between generators and engines. Strict (`extra="forbid"`), range-checked, tempo clamped to the genre's range, only allowlisted GM programs per role (pad, lead, bass, keys) and GS drum kits on channel 10. `bars` is the length of one phrase; `structure` lists the sections, each playing the phrase `repeats` times.
- `app/generator/procedural.py`: rules per genre (tempo, scales, two progressions, drum and bass grids, keys styles, instruments, swing, humanize, song forms). Choices are random but driven by `seed`, so the same seed gives the same spec. Repeats are scaled so a song lasts 3–5 minutes. Mood words adjust choices within the genre's rules.
- `app/generator/ai.py`, `brief.py`: ask the configured backend for a `SongBrief` and pass it to `procedural.generate`.
- `app/engine/arrange.py`: turns a spec into notes, section by section, with voice-led chords, bass styles (root, root/fifth, octave, walking, arp, drone) and keys styles (comp, stabs, offbeat, arpeggio, broken, alberti).
- `app/engine/fluidsynth_engine.py`: writes MIDI, renders with fluidsynth and cuts the WAV to the music length plus a ring-out that fades to silence. Output is normalised to −1 dBFS, then encoded to MP3 (`libmp3lame -qscale:a 2`) and cached by spec hash.
- `app/library.py`: tables `songs`, `playlists`, `playlist_songs`, `history`, `settings`, every row owned by a user (`lib.for_user(id)`).
- `app/users.py`: each user's player (made on first use), AI settings and keys, and backup schedule.
- `app/storage.py`: per-user S3 backups and the backup scheduler.
- `web/`: plain HTML/CSS/JS. The page decodes the MP3 into an `AudioBufferSourceNode`; the prefetched song starts sample-accurately when the current song's last bar ends, so the ring-out overlaps the next start.

## Add a genre

1. Add the name to the `Genre` literal and a tempo range in `GENRE_TEMPO` in `app/schema.py`.
2. Add a `GenreRules(...)` entry to `RULES` in `app/generator/procedural.py`: scales, progressions as `(degree, ext, beats)` (give at least two), drum grids as 16-character strings (`X` accent, `x` hit, `o` ghost, `.` rest), bass grids and styles, keys styles, song forms (`STANDARD`, `CLUB`, `FLOWING`, `SONG`, or your own tuple of `(section kind, weight)`), and instruments from the role allowlists (`None` leaves the role out).
3. If it needs a new instrument, add the GM program to `ROLE_PROGRAMS`.
4. Run the tests. The procedural, length and render tests cover every genre.

## Add an engine

Subclass `app.engine.base.Engine`. Set a unique `name` and implement `render_wav(spec) -> Path`. It must write exactly `spec.duration_seconds` (music plus ring-out) to `self.wav_path(spec)` at 44.1 kHz. The base class handles caching, per-song locking, MP3 encoding and cache pruning. `app.engine.arrange.arrange(spec)` gives the notes if the engine works from them. Attach the new engine with `users.attach(...)` in `app/main.py`.

## Security

Without `MOTIF_ADMIN_EMAIL` Motif is a single-user app for your own machine and has **no login**; with it, every request needs a session or a key (see [Accounts](#accounts)).

- It binds to `127.0.0.1` by default, and Docker publishes it on `127.0.0.1` only. In local mode anyone who can reach the port can control it and spend the saved AI keys, so don't expose it to a network without login mode or an authenticating reverse proxy in front. Motif logs a warning when it listens on a non-loopback address without logins. Serve login mode over HTTPS.
- Requests with an unexpected `Host` header (DNS rebinding) or a foreign `Origin` on POST/PUT/PATCH/DELETE (cross-site requests) are refused. Set `MOTIF_ALLOWED_HOSTS` if you reach it under another name.
- AI keys and S3 credentials are encrypted at rest (AES-256-GCM), never returned by the API and only used for the user who saved them; keep `MOTIF_SECRET_KEY` / `library/secret.key` private.
- AI replies are parsed as JSON data and validated against allowlists; nothing a model returns is executed.

## Notes

- FluidSynth 2.6 wants options before the positional arguments: `fluidsynth -ni -F out.wav -r 44100 <sf2> <mid>`. With `-F` after the files, it treats `out.wav` as an input.
- Soundfont: GeneralUser GS by S. Christian Collins. Keep its licence file with the `.sf2` and link to the author when redistributing; it is not bundled in the Docker image.
- Temporary files (`cache/tmp-*`) are removed after each render. `cache/` is a bounded LRU cache; saved songs live in SQLite as seed + spec.
- A 3–5 minute song renders in about 4–8 s; the next song is rendered while the current one plays.
- All patterns are generic genre conventions such as four-on-the-floor, backbeat and ii–V–I; melodies are generated from seeds. Nothing quotes an existing song.
