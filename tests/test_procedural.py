import numpy as np
import pytest

from app.engine.arrange import arrange
from app.engine.fluidsynth_engine import FluidSynthEngine, read_wav_float
from app.generator import procedural
from app.schema import GENRE_TEMPO, GENRES, SongSpec


@pytest.mark.parametrize("genre", GENRES)
def test_generate_valid_for_every_genre(genre):
    for seed in range(25):
        spec = procedural.generate(genre, seed=seed)
        lo, hi = GENRE_TEMPO[genre]
        assert lo <= spec.tempo <= hi
        assert SongSpec.model_validate(spec.model_dump()) == spec
        assert arrange(spec).notes, f"{genre} seed {seed} produced no notes"


@pytest.mark.parametrize("genre", GENRES)
def test_same_seed_same_spec_and_notes(genre):
    a = procedural.generate(genre, seed=42)
    b = procedural.generate(genre, seed=42)
    assert a == b
    assert arrange(a).notes == arrange(b).notes
    assert procedural.generate(genre, seed=43) != a


def test_mood_steers_scale_and_tempo():
    bright = [procedural.generate("lofi", "happy sunny morning", seed=s) for s in range(30)]
    assert all(s.scale in ("major", "lydian", "mixolydian") for s in bright)
    calm = [procedural.generate("house", "calm", seed=s).tempo for s in range(30)]
    energy = [procedural.generate("house", "energetic", seed=s).tempo for s in range(30)]
    assert max(calm) < min(energy)


def test_notes_stay_inside_song():
    for genre in GENRES:
        spec = procedural.generate(genre, seed=7)
        arr = arrange(spec)
        assert all(0 <= n.start < n.end <= arr.music_ticks for n in arr.notes)


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    return FluidSynthEngine(cache_dir=tmp_path_factory.mktemp("cache"))


@pytest.mark.parametrize("genre", GENRES)
def test_songs_are_three_to_five_minutes(genre):
    for seed in range(25):
        spec = procedural.generate(genre, seed=seed)
        assert 180 <= spec.music_seconds <= 300, (genre, seed, spec.music_seconds)
        assert len({s.kind for s in spec.structure}) >= 4


@pytest.mark.parametrize("genre", GENRES)
def test_full_song_render_length(engine, genre):
    spec = procedural.generate(genre, seed=11)
    audio, rate = read_wav_float(engine.render(spec))
    assert abs(len(audio) / rate - spec.duration_seconds) < 0.010
    assert np.abs(audio).max() > 0.5


def test_render_is_deterministic(tmp_path):
    spec = procedural.generate("trip-hop", seed=5)
    a = FluidSynthEngine(cache_dir=tmp_path / "a").render(spec).read_bytes()
    b = FluidSynthEngine(cache_dir=tmp_path / "b").render(spec).read_bytes()
    assert a == b


def test_scale_structure_keeps_proportions():
    form = [{"kind": "intro", "repeats": 1}, {"kind": "main", "repeats": 2}, {"kind": "outro", "repeats": 1}]
    out = procedural.scale_structure(form, phrase_seconds=10, target=240)
    assert [s["kind"] for s in out] == ["intro", "main", "outro"]
    assert 180 <= sum(s["repeats"] for s in out) * 10 <= 300
    assert out[1]["repeats"] > out[0]["repeats"]


@pytest.mark.parametrize("prompt,genre", [
    ("rainy jazz with a tenor sax at 3am", "jazz"),
    ("80s neon night drive", "synthwave"),
    ("something to study to", "lofi"),
    ("8-bit boss battle", "chiptune"),
    ("gentle piano for reading", "classical-piano"),
    ("deep space drone", "ambient"),
])
def test_prompt_picks_radio(prompt, genre):
    assert procedural.genre_from_prompt(prompt) == genre


def test_prompt_instruments_applied():
    spec = procedural.generate("lofi", "late night with a tenor sax and strings", seed=3)
    assert spec.instruments.lead == 66
    spec = procedural.generate("classical-piano", "with a flute", seed=3)
    assert spec.instruments.lead == 73 and spec.instruments.keys == 0


def _brief(**kw):
    from app.generator.brief import SongBrief
    return SongBrief.model_validate({"seed": 123, **kw})


def test_brief_overrides_applied():
    brief = _brief(name="  My   Tune ", key="F#", scale="phrygian", tempo=100, density=0.9,
                   instruments={"lead": 65, "pad": 52, "bass": 33, "keys": 16})
    spec = procedural.generate("synthwave", "happy", brief=brief)
    assert spec.seed == 123 and spec.name == "My Tune" and spec.key == "F#" and spec.scale == "phrygian"
    assert spec.tempo == 100 and spec.melody.density == 0.9
    assert (spec.instruments.lead, spec.instruments.pad, spec.instruments.bass, spec.instruments.keys) == (65, 52, 33, 16)
    assert procedural.generate("synthwave", brief=brief, seed=5).seed == 5


def test_brief_energy_picks_tempo_and_tempo_clamped_to_genre():
    lo, hi = GENRE_TEMPO["house"]
    assert procedural.generate("house", brief=_brief(energy=0.0)).tempo == lo
    assert procedural.generate("house", brief=_brief(energy=1.0)).tempo == hi
    assert procedural.generate("house", brief=_brief(energy=1.0, tempo=40)).tempo == lo


@pytest.mark.parametrize("genre", GENRES)
def test_brief_deterministic_and_valid(genre):
    brief = _brief(energy=0.5, density=0.3, scale="lydian")
    a = procedural.generate(genre, "calm", brief=brief)
    assert a == procedural.generate(genre, "calm", brief=brief)
    assert arrange(a).notes
    assert procedural.generate(genre, "calm", brief=_brief(seed=124)) != a


def test_empty_brief_matches_seed_only():
    assert procedural.generate("lofi", brief=_brief(seed=7)) == procedural.generate("lofi", seed=7)
