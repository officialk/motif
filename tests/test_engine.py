import numpy as np
import pytest

from app.engine.arrange import BAR, STEP, arrange, phrases
from app.engine.fluidsynth_engine import FluidSynthEngine, read_wav_float


@pytest.fixture
def engine(tmp_cache):
    return FluidSynthEngine(cache_dir=tmp_cache)


def test_render_hardcoded_spec_to_mp3(engine, spec):
    wav = engine.render(spec)
    mp3 = engine.mp3_path(spec)
    assert wav.exists() and mp3.exists()
    assert mp3.stat().st_size > 100_000
    assert not list(engine.cache_dir.glob("tmp-*"))  # temp files cleaned


def test_song_length_exact_and_ends_silent(engine, spec):
    audio, rate = read_wav_float(engine.render(spec))
    assert abs(len(audio) / rate - spec.duration_seconds) < 0.010
    assert np.abs(audio[-200:]).max() < 1e-3  # faded ring-out
    music_end = round(spec.music_seconds * rate)
    assert np.abs(audio[:music_end]).max() > 0.5  # normalised, not silent


def test_sections_shape_the_arrangement(spec):
    arr = arrange(spec)
    assert arr.music_ticks == spec.total_bars * BAR
    assert all(0 <= n.start < n.end <= arr.music_ticks for n in arr.notes)
    by_phrase = {ph.kind: ph for ph in phrases(spec)}

    def count(kind, channel):
        # ignore the last 16th: humanize may pull the next section's downbeat slightly early
        ph = by_phrase[kind]
        return sum(1 for n in arr.notes if n.channel == channel and ph.start <= n.start < ph.start + spec.bars * BAR - STEP)

    drums, lead = 9, 1
    assert count("breakdown", drums) == 0  # breakdown drops the drums
    assert count("intro", lead) == 0  # melody enters after the intro
    assert count("main", drums) > count("intro", drums)
    last_bar = arr.music_ticks - BAR
    assert {n.channel for n in arr.notes if n.start >= last_bar} >= {0, 1, 2}  # held cadence


def test_cache_hit_reuses_files(engine, spec):
    wav = engine.render(spec)
    assert engine.render(spec) == wav
    assert engine.is_cached(spec)
