# music

A local generative radio you open in the browser. The Python backend composes original 3–5 minute songs, renders them with FluidSynth and the browser plays one song straight into the next.

```
SongSpec (JSON, pydantic) -> sections x phrases -> notes -> MIDI (mido) -> fluidsynth -> WAV + ring-out -> ffmpeg MP3
```

Nothing is pre-recorded or downloaded. Every song is composed by code from a seed. The soundfont supplies only the instrument sounds.

## Features

- Spotify-style UI: radios (one per procedural genre), a prompt bar, playlists, liked songs, a queue, repeat off / all / one, an output device picker and a settings page.
- Songs last 3–5 minutes and have sections: intro, main, variation, breakdown, build, return, outro.
- Saved (liked) songs are stored as seed + spec JSON in SQLite (`library/music.db`), not as audio. Audio is re-rendered on demand and is identical for the same spec.
- Download as MP3 or WAV. The file is rendered on the server (WAV can take 5–10 s).
- The AI never writes a song. It returns a small brief (a seed and a few choices such as name, key, tempo, energy, instruments) and the procedural generator builds the song from it.
- Optional S3-compatible storage: media of saved/exported songs, and database backup and restore.

## AI backends

Set in `.env` or on the Settings page (persisted in SQLite).

| Backend | `AI_BACKEND` | Needs |
|---|---|---|
| Off (procedural only) | `off` | nothing |
| Claude CLI | `claude-cli` | the `claude` binary on PATH (local runs only) |
| Claude API | `claude-api` | `ANTHROPIC_API_KEY` |
| OpenAI-compatible | `openai` | `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL` |

OpenAI-compatible examples:

```sh
# OpenAI
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

```sh
# Ollama (from Docker use host.docker.internal; locally use localhost)
OPENAI_BASE_URL=http://host.docker.internal:11434/v1
OPENAI_MODEL=llama3.1
OPENAI_API_KEY=ollama
```

```sh
# OpenRouter
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_MODEL=openai/gpt-4o-mini
```

A prompt always asks the AI; automatic songs ask it with probability `AI_RATIO`. The reply is JSON, validated by pydantic (`SongBrief`, extra fields rejected). Any error or timeout falls back to procedural generation. Nothing the model returns is executed. The Settings page has a test button (`POST /settings/ai/test`).

## S3 storage

Works with AWS S3, MinIO, Cloudflare R2 and similar. Set `S3_BUCKET` to enable it; credentials come from the standard `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.

- Media: audio of saved and exported songs is stored under `<S3_PREFIX>media/<hash>.mp3` and fetched from there before re-rendering (`S3_MEDIA=0` turns this off).
- Backups: a consistent snapshot of the SQLite database (SQLite backup API) is uploaded to `<S3_PREFIX>backups/music-YYYYmmdd-HHMMSS.db`. Create, list and restore from the Settings page or the API (`/admin/backup`, `/admin/backups`, `/admin/restore`). A restore is verified (valid SQLite with the expected tables) before it replaces the live database.
- Scheduled backups: `S3_BACKUP_INTERVAL_HOURS` (0 = manual only).

## Docker

Needs Docker with Compose. The soundfont is not in the image (32 MB; its licence asks for a link to the author). Put one General MIDI `.sf2` in `./soundfonts`; it is mounted read-only.

```sh
cp .env.example .env
```

```sh
docker compose up --build
```

Open http://127.0.0.1:8000. The port is published on localhost only. Songs and the database live in the `music-data` volume (`/data`). The `claude-cli` backend is not available in the container; use `claude-api` or `openai`.

Optional local S3 with MinIO (bound to 127.0.0.1, ports 9000 and 9001):

```sh
MINIO_ROOT_USER=musicadmin MINIO_ROOT_PASSWORD=choose-a-password docker compose --profile minio up --build
```

Create a bucket in the console at http://127.0.0.1:9001, then set in `.env`:

```sh
S3_BUCKET=music
S3_ENDPOINT_URL=http://minio:9000
AWS_ACCESS_KEY_ID=musicadmin
AWS_SECRET_ACCESS_KEY=choose-a-password
```

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
| `MUSIC_HOST` | `127.0.0.1` | Bind address (`0.0.0.0` in the Docker image) |
| `MUSIC_ALLOWED_HOSTS` | `127.0.0.1,localhost` | Host names the browser may use; other `Host` headers and cross-site writes are refused |
| `MUSIC_PORT` | `8000` | Server port |
| `MUSIC_DATA_DIR` | project root | Base for `cache/` and `library/` (`/data` in Docker) |
| `MUSIC_CACHE_DIR` | `<data>/cache` | Rendered-audio cache |
| `MUSIC_LIBRARY_DIR` | `<data>/library` | Library directory |
| `MUSIC_DB` | `<library>/music.db` | SQLite file |
| `MUSIC_SOUNDFONT_DIR` | `soundfonts/` | Directory searched for `*.sf2` (`/soundfonts` in Docker) |
| `MUSIC_SOUNDFONT` | first `*.sf2` | Use this soundfont file |
| `MUSIC_CACHE_MAX` | `12` | Songs kept in the cache, about 50 MB each (least recently used removed first) |
| `MUSIC_DRY_RUN` | unset | `1` = same as `--dry-run` |
| `AI_BACKEND` | `off` | `off`, `claude-cli`, `claude-api`, `openai` (`CLAUDE_BACKEND=cli\|api` still accepted) |
| `AI_RATIO` | `0.25` | Share of automatic songs that ask the AI (`CLAUDE_RATIO` accepted) |
| `AI_TIMEOUT` | `20` | Seconds before falling back to procedural (`CLAUDE_TIMEOUT` accepted) |
| `ANTHROPIC_API_KEY` | | Key for `claude-api` |
| `CLAUDE_MODEL` | `claude-haiku-5-5` | Model for `claude-api` |
| `CLAUDE_CLI` / `CLAUDE_CLI_MODEL` | `claude` / `haiku` | Binary and model alias for `claude-cli` |
| `OPENAI_API_KEY` | | Key for the OpenAI-compatible server |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Server base URL |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model name |
| `OPENAI_JSON_MODE` | `1` | `0` if the server rejects JSON response format |
| `S3_BUCKET` | empty | Bucket; empty = local only |
| `S3_PREFIX` | `music/` | Key prefix |
| `S3_ENDPOINT_URL` | AWS | Custom endpoint (MinIO, R2) |
| `S3_REGION` | boto3 default | Region |
| `S3_MEDIA` | `1` | `0` = do not store audio in S3 |
| `S3_BACKUP_INTERVAL_HOURS` | `0` | Scheduled backups; 0 = manual only |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | | S3 credentials (read by boto3) |

## API

JSON in and out. Errors are `{"detail": "..."}` with a 4xx status. `GET /state` returns `{now, up_next, history, mode}`.

| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/genres` | | genres and AI backend |
| GET | `/state`, `/now`, `/queue` | | state / now + mode / up next |
| GET | `/history` | | persisted play history |
| POST | `/genre` | `{genre, mood?}` | start a radio |
| POST | `/random` | `{mood?}` | shuffle radios |
| POST | `/prompt` | `{text}` | start a prompt station |
| POST | `/skip` | `{from_id?}` | next song (`from_id` guards double skips) |
| POST | `/repeat` | `{mode}` | `off`, `all` or `one` |
| POST | `/like` | `{id}` | save a song |
| GET | `/library` | | saved songs, newest first |
| PATCH | `/library/{id}` | `{name}` | rename |
| DELETE | `/library/{id}` | | delete (also from playlists) |
| POST | `/play` | `{id}` | play a saved song or a track |
| GET | `/playlists` | | all playlists |
| POST | `/playlists` | `{name, song_id?}` | create |
| GET | `/playlists/{id}` | | playlist with songs |
| PATCH | `/playlists/{id}` | `{name}` | rename |
| DELETE | `/playlists/{id}` | | delete |
| POST | `/playlists/{id}/songs` | `{id}` | add a song (saves it if needed) |
| DELETE | `/playlists/{id}/songs/{song_id}` | | remove a song |
| PUT | `/playlists/{id}/order` | `{song_ids}` | reorder |
| POST | `/playlists/{id}/play` | `{start?, shuffle?}` | play the playlist |
| POST | `/download/{id}` | `{format: mp3\|wav}` | file attachment, rendered on the server |
| GET | `/audio/{track_id}.mp3` | | audio of a ready track |
| GET | `/settings` | | AI and storage settings |
| PUT | `/settings/ai` | any of `backend, ratio, model, cli_model, base_url, timeout` | persisted settings |
| POST | `/settings/ai/test` | | `{ok, source, seconds, brief?, error?}` |
| GET | `/admin/backups` | | `{enabled, backups}` |
| POST | `/admin/backup` | | `{backup}` |
| POST | `/admin/restore` | `{key}` | `{ok}` |

## How it works

- `app/schema.py`: `SongSpec`, the only contract between generators and engines. Strict (`extra="forbid"`), range-checked, tempo clamped to the genre's range, only allowlisted GM programs per role (pad, lead, bass, keys) and GS drum kits on channel 10. `bars` is the length of one phrase; `structure` lists the sections, each playing the phrase `repeats` times.
- `app/generator/procedural.py`: rules per genre (tempo, scales, two progressions, drum and bass grids, keys styles, instruments, swing, humanize, song forms). Choices are random but driven by `seed`, so the same seed gives the same spec. Repeats are scaled so a song lasts 3–5 minutes. Mood words adjust choices within the genre's rules.
- `app/generator/ai.py`, `brief.py`: ask the configured backend for a `SongBrief` and pass it to `procedural.generate`.
- `app/engine/arrange.py`: turns a spec into notes, section by section, with voice-led chords, bass styles (root, root/fifth, octave, walking, arp, drone) and keys styles (comp, stabs, offbeat, arpeggio, broken, alberti).
- `app/engine/fluidsynth_engine.py`: writes MIDI, renders with fluidsynth and cuts the WAV to the music length plus a ring-out that fades to silence. Output is normalised to −1 dBFS, then encoded to MP3 (`libmp3lame -qscale:a 2`) and cached by spec hash.
- `app/library.py`: SQLite tables `songs`, `playlists`, `playlist_songs`, `history`, `settings`.
- `app/storage.py`: S3 media store, backups and the backup scheduler.
- `web/`: plain HTML/CSS/JS. The page decodes the MP3 into an `AudioBufferSourceNode`; the prefetched song starts sample-accurately when the current song's last bar ends, so the ring-out overlaps the next start.

## Add a genre

1. Add the name to the `Genre` literal and a tempo range in `GENRE_TEMPO` in `app/schema.py`.
2. Add a `GenreRules(...)` entry to `RULES` in `app/generator/procedural.py`: scales, progressions as `(degree, ext, beats)` (give at least two), drum grids as 16-character strings (`X` accent, `x` hit, `o` ghost, `.` rest), bass grids and styles, keys styles, song forms (`STANDARD`, `CLUB`, `FLOWING`, `SONG`, or your own tuple of `(section kind, weight)`), and instruments from the role allowlists (`None` leaves the role out).
3. If it needs a new instrument, add the GM program to `ROLE_PROGRAMS`.
4. Run the tests. The procedural, length and render tests cover every genre.

## Add an engine

Subclass `app.engine.base.Engine`. Set a unique `name` and implement `render_wav(spec) -> Path`. It must write exactly `spec.duration_seconds` (music plus ring-out) to `self.wav_path(spec)` at 44.1 kHz. The base class handles caching, per-song locking, MP3 encoding and cache pruning. `app.engine.arrange.arrange(spec)` gives the notes if the engine works from them. Pass the new engine to `Player(...)` in `app/main.py`.

## Notes

- FluidSynth 2.6 wants options before the positional arguments: `fluidsynth -ni -F out.wav -r 44100 <sf2> <mid>`. With `-F` after the files, it treats `out.wav` as an input.
- Soundfont: GeneralUser GS by S. Christian Collins. Keep its licence file with the `.sf2` and link to the author when redistributing; it is not bundled in the Docker image.
- Temporary files (`cache/tmp-*`) are removed after each render. `cache/` is a bounded LRU cache; saved songs live in SQLite as seed + spec.
- A 3–5 minute song renders in about 4–8 s; the next song is rendered while the current one plays.
- All patterns are generic genre conventions such as four-on-the-floor, backbeat and ii–V–I; melodies are generated from seeds. Nothing quotes an existing song.
