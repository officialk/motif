# motif — local generative radio

## Rules

- Python 3.12, venv in .venv. FastAPI, pydantic, mido, jinja2 not needed.
- Audio chain: SongSpec -> mido MIDI -> `fluidsynth -ni <sf2> song.mid -F out.wav -r 44100` -> ffmpeg MP3.
- Soundfont lives in soundfonts/. Never download or execute anything not asked.
- LLM output is JSON only, validated by pydantic. Never execute LLM-written code.
- Run a test after each phase and show the result before the next phase.
- Temp files go in cache/. Clean them. Keep final songs in library/.
- Never reproduce existing songs or melodies. Original patterns only.
