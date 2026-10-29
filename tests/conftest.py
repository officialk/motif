import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CLAUDE_BACKEND", "off")
os.environ.setdefault("AI_BACKEND", "off")
os.environ["MUSIC_ALLOWED_HOSTS"] = "127.0.0.1,localhost,testserver"

from app.engine.fluidsynth_engine import FluidSynthEngine  # noqa: E402
from app.generator import make_spec, procedural  # noqa: E402
from app.library import Library  # noqa: E402
from app.main import create_app  # noqa: E402
from app.player import Player  # noqa: E402
from app.schema import SongSpec  # noqa: E402

GRID = lambda *on, v=100: [v if i in on else 0 for i in range(16)]  # noqa: E731

HARDCODED = {
    "genre": "lofi",
    "name": "Test Window Rain",
    "tempo": 80,
    "key": "D",
    "scale": "dorian",
    "bars": 4,
    "chord_progression": [
        {"degree": 1, "ext": "7", "beats": 4},
        {"degree": 4, "ext": "7", "beats": 4},
        {"degree": 6, "ext": "7", "beats": 4},
        {"degree": 5, "ext": "sus4", "beats": 4},
    ],
    "alt_progression": [{"degree": 4, "ext": "9", "beats": 8}, {"degree": 5, "ext": "7", "beats": 8}],
    "structure": [
        {"kind": "intro", "repeats": 1},
        {"kind": "main", "repeats": 1},
        {"kind": "breakdown", "repeats": 1},
        {"kind": "variation", "repeats": 1},
        {"kind": "outro", "repeats": 1},
    ],
    "drum_pattern": {
        "kick": GRID(0, 7, 10),
        "snare": GRID(4, 12),
        "closed_hat": GRID(0, 2, 4, 6, 8, 10, 12, 14, v=70),
    },
    "bass_pattern": {"style": "root_fifth", "steps": GRID(0, 7, 10, v=90), "octave": 2},
    "keys_style": "comp",
    "melody": {"density": 0.4, "register": "mid", "rhythm_seed": 7},
    "instruments": {"pad": 89, "lead": 11, "bass": 33, "keys": 4, "drum_kit": 8},
    "swing": 0.25,
    "humanize": 0.3,
    "reverb_tail_bars": 2,
    "seed": 1234,
}


@pytest.fixture
def spec() -> SongSpec:
    return SongSpec.model_validate(HARDCODED)


@pytest.fixture
def tmp_cache(tmp_path):
    d = tmp_path / "cache"
    d.mkdir()
    return d


def short_spec_maker(genre, mood, use_claude):
    """Same generator, but ~40 s songs so API tests stay fast."""
    spec, source = make_spec(genre, mood, use_claude)
    structure = procedural.scale_structure(
        [s.model_dump() for s in spec.structure], spec.phrase_seconds, 40, lo=30, hi=60)
    return SongSpec.model_validate({**spec.model_dump(), "structure": structure}), source


@pytest.fixture
def client(tmp_cache, tmp_path):
    lib = Library(tmp_path / "library")
    player = Player(FluidSynthEngine(cache_dir=tmp_cache), short_spec_maker,
                    on_history=lambda t: lib.record_history(t.id, t.spec, t.source), library=lib)
    with TestClient(create_app(player, lib)) as c:
        c.player = player
        c.library = lib
        yield c


def wait_ready(client, track_id):
    t = client.player.get(track_id)
    assert t.ready_event.wait(30), "render timed out"
    assert t.status == "ready", t.error
