import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["MOTIF_ALLOWED_HOSTS"] = "127.0.0.1,localhost,testserver"
# Never the real library, database, key or mail server: everything defaults into a throwaway dir.
os.environ["MOTIF_DATA_DIR"] = tempfile.mkdtemp(prefix="motif-test-")
atexit.register(shutil.rmtree, os.environ["MOTIF_DATA_DIR"], True)
for _name in ("MOTIF_LIBRARY_DIR", "MOTIF_CACHE_DIR", "MOTIF_DB", "MOTIF_DATABASE_URL", "MOTIF_SECRET_KEY",
              "MOTIF_ADMIN_EMAIL", "MOTIF_ADMIN_PASSWORD", "MOTIF_PUBLIC_URL", "MOTIF_SMTP_HOST", "MOTIF_SMTP_USER",
              "MOTIF_SMTP_PASSWORD", "MOTIF_SMTP_FROM"):
    os.environ.pop(_name, None)

from app import config  # noqa: E402
from app.accounts import Accounts  # noqa: E402
from app.engine.base import Engine  # noqa: E402
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


ADMIN_EMAIL, ADMIN_PASSWORD = "admin@example.com", "admin-pass-1"


@pytest.fixture(autouse=True)
def isolated(tmp_path_factory, monkeypatch):
    """Never touch the real database, secrets, master key or a mail server from tests."""
    root = tmp_path_factory.mktemp("data")
    monkeypatch.setattr(config, "LIBRARY_DIR", root / "library")
    monkeypatch.setattr(config, "DB_PATH", root / "library" / "motif.db")
    monkeypatch.setattr(config, "CACHE_DIR", root / "cache")
    monkeypatch.setattr(config, "DATABASE_URL", None)
    monkeypatch.setattr(config, "ADMIN_EMAIL", None)
    monkeypatch.setattr(config, "ADMIN_PASSWORD", None)
    monkeypatch.setattr(config, "PUBLIC_URL", None)
    monkeypatch.setattr(config, "SMTP_HOST", None)
    monkeypatch.delenv("MOTIF_SECRET_KEY", raising=False)
    yield


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


class FakeEngine(Engine):
    """Writes placeholder audio instantly (account tests don't need real renders)."""

    name = "fake"

    def render_wav(self, spec):
        wav = self.wav_path(spec)
        wav.write_bytes(b"RIFF" + spec.audio_hash().encode())
        return wav

    def render(self, spec):
        with self._lock(spec):
            if not self.is_cached(spec):
                with self._slots:
                    self.render_wav(spec)
                    self.mp3_path(spec).write_bytes(b"ID3" + spec.audio_hash().encode())
            return self.wav_path(spec)


def fake_player(tmp_path, lib):
    return Player(FakeEngine(cache_dir=tmp_path / "fake-cache"), short_spec_maker,
                  on_history=lambda t: lib.record_history(t.id, t.spec, t.source), library=lib)


@pytest.fixture
def login_client(tmp_path):
    """Login mode, signed in as the env admin. `c.member(email, **features)` adds a signed-in member client.
    Every player renders with the instant FakeEngine."""
    lib = Library(tmp_path / "library")
    accounts = Accounts(lib.db, ADMIN_EMAIL, ADMIN_PASSWORD)
    player = fake_player(tmp_path, lib)
    app = create_app(player, lib, accounts=accounts)
    # members' players: nothing cued on creation, short songs
    app.state.users.cue = lambda p, lib_: None
    app.state.users.make_spec = lambda uid, _make=app.state.users.make_spec: _short(_make(uid))
    with TestClient(app) as c:
        assert c.post("/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).status_code == 200
        c.player, c.library, c.accounts, c.users = player, lib, accounts, app.state.users
        c.member = lambda email="member@example.com", password="member-pass-1", **features: \
            add_member(c, email, password, **features)
        yield c


def _short(maker):
    def short(genre, mood, use_ai):
        spec, source = maker(genre, mood, use_ai)
        structure = procedural.scale_structure(
            [s.model_dump() for s in spec.structure], spec.phrase_seconds, 40, lo=30, hi=60)
        return SongSpec.model_validate({**spec.model_dump(), "structure": structure}), source
    return short


def add_member(admin, email, password, **features):
    """Invite + accept through the API; returns a TestClient signed in as the new member."""
    r = admin.post("/users/invite", json={"email": email, "features": features or None})
    assert r.status_code == 200, r.text
    token = r.json()["link"].split("token=")[1]
    m = TestClient(admin.app)
    r = m.post("/auth/token", json={"token": token, "password": password})
    assert r.status_code == 200, r.text
    m.user = r.json()["user"]
    return m


def wait_ready(client, track_id):
    t = client.player.get(track_id)
    assert t.ready_event.wait(30), "render timed out"
    assert t.status == "ready", t.error
