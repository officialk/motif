import copy

import pytest
from pydantic import ValidationError

from app.schema import GENRE_TEMPO, SongSpec, clamp_spec_dict
from tests.conftest import HARDCODED


def make(**changes):
    d = copy.deepcopy(HARDCODED)
    d.update(changes)
    return d


def test_valid_spec(spec):
    assert spec.genre == "lofi"
    assert spec.total_bars == 4 * 5
    assert spec.music_seconds == pytest.approx(20 * 4 * 60 / 80)
    assert spec.duration_seconds == pytest.approx(spec.music_seconds + spec.tail_seconds)
    assert [s["kind"] for s in spec.section_times()] == ["intro", "main", "breakdown", "variation", "outro"]
    assert spec.section_times()[1]["start"] == pytest.approx(spec.phrase_seconds)


def test_tempo_clamped_to_genre():
    lo, hi = GENRE_TEMPO["lofi"]
    assert SongSpec.model_validate(make(tempo=180)).tempo == hi
    assert SongSpec.model_validate(make(tempo=41)).tempo == lo


@pytest.mark.parametrize("bad", [
    {"bars": 3},
    {"bars": 17},
    {"scale": "chromatic"},
    {"key": "H"},
    {"genre": "polka"},
    {"swing": 0.9},
    {"chord_progression": []},
    {"chord_progression": [{"degree": 8}]},
    {"drum_pattern": {"kick": [100] * 15}},
    {"drum_pattern": {"cowbell": [0] * 16}},
    {"instruments": {"lead": 127}},
    {"instruments": {"drum_kit": 3}},
    {"structure": []},
    {"structure": [{"kind": "chorus", "repeats": 1}]},
    {"structure": [{"kind": "main", "repeats": 1}]},  # 12 s: too short
    {"structure": [{"kind": "main", "repeats": 16}] * 4},  # 12.8 min: too long
    {"loop_count": 4},  # removed field
    {"unknown_field": 1},
])
def test_invalid_specs_rejected(bad):
    with pytest.raises(ValidationError):
        SongSpec.model_validate(make(**bad))


def test_clamp_spec_dict_brings_numbers_into_range():
    raw = make(bars=40, swing=3, humanize=-1, seed=-5, reverb_tail_bars=99)
    raw["melody"] = {"density": 4, "register": "mid", "rhythm_seed": 1}
    raw["structure"] = [{"kind": "main", "repeats": 1}] * 2 + [{"kind": "outro", "repeats": 0}]
    spec = SongSpec.model_validate(clamp_spec_dict(raw))
    assert (spec.bars, spec.swing, spec.humanize, spec.seed, spec.reverb_tail_bars) == (16, 0.5, 0.0, 0, 4)
    assert spec.melody.density == 1.0 and spec.structure[-1].repeats == 1


def test_clamp_does_not_bypass_allowlist():
    with pytest.raises(ValidationError):
        SongSpec.model_validate(clamp_spec_dict(make(instruments={"lead": 115})))


def test_audio_hash(spec):
    assert spec.model_copy(update={"seed": 1}).audio_hash() != spec.audio_hash()
    assert SongSpec.model_validate(make()).audio_hash() == spec.audio_hash()
