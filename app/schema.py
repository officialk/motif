"""SongSpec: the only contract between generators and engines.

Everything an engine needs to render a song is in here, validated and bounded.
LLM output is parsed into this model and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import warnings
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Genre = Literal[
    "lofi", "ambient", "synthwave", "jazz", "techno",
    "chiptune", "classical-piano", "house", "trip-hop",
    "drum-and-bass", "hip-hop", "reggae", "bossa-nova", "funk",
    "cinematic", "disco", "city-pop", "blues",
]
GENRES: tuple[str, ...] = Genre.__args__  # type: ignore[attr-defined]

Key = Literal["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
KEYS: tuple[str, ...] = Key.__args__  # type: ignore[attr-defined]

# Seven-note scales only, so chords can be stacked in thirds from any degree.
SCALES: dict[str, tuple[int, ...]] = {
    "major": (0, 2, 4, 5, 7, 9, 11),
    "minor": (0, 2, 3, 5, 7, 8, 10),
    "dorian": (0, 2, 3, 5, 7, 9, 10),
    "mixolydian": (0, 2, 4, 5, 7, 9, 10),
    "lydian": (0, 2, 4, 6, 7, 9, 11),
    "phrygian": (0, 1, 3, 5, 7, 8, 10),
    "harmonic_minor": (0, 2, 3, 5, 7, 8, 11),
}
Scale = Literal["major", "minor", "dorian", "mixolydian", "lydian", "phrygian", "harmonic_minor"]

# Tempo range per genre (bpm). Tempo is clamped into this range.
GENRE_TEMPO: dict[str, tuple[int, int]] = {
    "lofi": (68, 90),
    "ambient": (56, 80),
    "synthwave": (84, 118),
    "jazz": (88, 160),
    "techno": (122, 136),
    "chiptune": (120, 160),
    "classical-piano": (60, 110),
    "house": (118, 128),
    "trip-hop": (70, 96),
    "drum-and-bass": (160, 176),
    "hip-hop": (84, 96),
    "reggae": (68, 90),
    "bossa-nova": (115, 140),
    "funk": (96, 116),
    "cinematic": (60, 100),
    "disco": (114, 126),
    "city-pop": (100, 120),
    "blues": (70, 120),
}

# GM program numbers (0-indexed) allowed per role. Anything else is rejected.
ROLE_PROGRAMS: dict[str, frozenset[int]] = {
    "pad": frozenset({48, 49, 50, 51, 52, 53, 54, 88, 89, 90, 91, 92, 93, 94, 95}),
    "lead": frozenset({0, 4, 5, 8, 9, 11, 12, 22, 24, 26, 40, 56, 60, 61, 65, 66, 68, 71, 73, 80, 81, 82, 84, 87}),
    "bass": frozenset({32, 33, 34, 35, 36, 37, 38, 39, 43, 80, 81}),
    "keys": frozenset({0, 1, 2, 4, 5, 7, 8, 11, 16, 17, 18, 24, 25, 26, 27, 46, 48, 61, 80, 81}),
}
# Drum kits live on MIDI channel 10 (bank 128); these are the GS kit programs.
DRUM_KITS: frozenset[int] = frozenset({0, 8, 16, 24, 25, 32, 40, 48})

DrumPiece = Literal["kick", "snare", "clap", "rim", "closed_hat", "open_hat", "ride", "shaker", "tom_low", "tom_high", "crash"]
# GM percussion note per kit piece.
DRUM_NOTES: dict[str, int] = {
    "kick": 36, "snare": 38, "clap": 39, "rim": 37, "closed_hat": 42,
    "open_hat": 46, "ride": 51, "shaker": 70, "tom_low": 45, "tom_high": 50, "crash": 49,
}

BassStyle = Literal["root", "root_fifth", "octave", "walking", "arp", "drone"]
KeysStyle = Literal["sustain", "stabs", "offbeat", "arpeggio", "broken", "comp", "alberti"]
ChordExt = Literal["triad", "7", "9", "sus2", "sus4"]
Register = Literal["low", "mid", "high"]
SectionKind = Literal["intro", "main", "variation", "breakdown", "build", "return", "outro"]

STEPS = 16  # 16th-note grid, one bar of 4/4
BEATS_PER_BAR = 4
MIN_SONG_SECONDS = 30
MAX_SONG_SECONDS = 600
MIN_TAIL_SECONDS = 2.0


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Chord(_Strict):
    degree: int = Field(ge=1, le=7, description="Scale degree of the chord root, 1-7")
    ext: ChordExt = "triad"
    beats: int = Field(default=4, ge=1, le=16, description="Duration in beats")


class Section(_Strict):
    kind: SectionKind
    repeats: int = Field(default=1, ge=1, le=16, description="How many times the base phrase (`bars` bars) plays")


class BassPattern(_Strict):
    style: BassStyle = "root"
    steps: list[int] = Field(description="16 velocities (0 = rest), one per 16th note")
    octave: int = Field(default=2, ge=1, le=3)

    @field_validator("steps")
    @classmethod
    def _grid(cls, v: list[int]) -> list[int]:
        return _check_grid(v)


# `register` is a fine field name; pydantic only warns because ABCMeta has a method called that.
warnings.filterwarnings("ignore", message='Field name "register"')


class Melody(_Strict):
    enabled: bool = True
    density: float = Field(default=0.5, ge=0.0, le=1.0, description="Share of 16th steps with a note onset")
    register: Register = "mid"
    rhythm_seed: int = Field(default=0, ge=0, le=2**31 - 1)
    motif_bars: int = Field(default=1, ge=1, le=2)


class Instruments(_Strict):
    pad: Optional[int] = None
    lead: Optional[int] = None
    bass: Optional[int] = None
    keys: Optional[int] = None
    drum_kit: Optional[int] = Field(default=None, description="GS drum kit program on channel 10, null = no drums")

    @model_validator(mode="after")
    def _allowlist(self) -> "Instruments":
        for role, allowed in ROLE_PROGRAMS.items():
            prog = getattr(self, role)
            if prog is not None and prog not in allowed:
                raise ValueError(f"program {prog} not allowed for role {role}")
        if self.drum_kit is not None and self.drum_kit not in DRUM_KITS:
            raise ValueError(f"drum kit {self.drum_kit} not allowed")
        return self


def _check_grid(v: list[int]) -> list[int]:
    if len(v) != STEPS:
        raise ValueError(f"grid must have {STEPS} steps, got {len(v)}")
    for x in v:
        if not 0 <= x <= 127:
            raise ValueError("velocities must be 0-127")
    return v


class SongSpec(_Strict):
    genre: Genre
    name: str = Field(min_length=1, max_length=60)
    tempo: float = Field(ge=40, le=200, description="BPM; clamped into the genre range")
    key: Key
    scale: Scale
    bars: int = Field(ge=4, le=16, description="Bars in one phrase; sections are whole phrases")
    chord_progression: list[Chord] = Field(min_length=1, max_length=16)
    alt_progression: Optional[list[Chord]] = Field(
        default=None, min_length=1, max_length=16, description="Used in variation and breakdown sections",
    )
    structure: list[Section] = Field(min_length=1, max_length=12, description="Song form, played once in order")
    drum_pattern: dict[DrumPiece, list[int]] = Field(default_factory=dict, description="Kit piece -> 16 velocities")
    drum_fill: bool = Field(default=False, description="Drum fill at the end of each section")
    bass_pattern: BassPattern
    keys_style: KeysStyle = "sustain"
    melody: Melody
    instruments: Instruments
    swing: float = Field(default=0.0, ge=0.0, le=0.5, description="Off-beat delay as a fraction of the swing unit")
    swing_grid: Literal[8, 16] = Field(default=16, description="Swing 8th notes (jazz) or 16th notes (lofi, house)")
    humanize: float = Field(default=0.0, ge=0.0, le=1.0)
    reverb_tail_bars: int = Field(default=1, ge=0, le=4, description="Ring-out after the last bar")
    seed: int = Field(ge=0, le=2**31 - 1)

    @field_validator("drum_pattern")
    @classmethod
    def _drums(cls, v: dict[str, list[int]]) -> dict[str, list[int]]:
        for grid in v.values():
            _check_grid(grid)
        return v

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("empty name")
        return v

    @model_validator(mode="after")
    def _clamp_tempo(self) -> "SongSpec":
        lo, hi = GENRE_TEMPO[self.genre]
        self.tempo = round(min(max(self.tempo, lo), hi), 2)
        if not MIN_SONG_SECONDS <= self.music_seconds <= MAX_SONG_SECONDS:
            raise ValueError(
                f"song is {self.music_seconds:.0f}s; structure must give {MIN_SONG_SECONDS}-{MAX_SONG_SECONDS}s"
            )
        return self

    # --- derived values -------------------------------------------------

    @property
    def bar_seconds(self) -> float:
        return BEATS_PER_BAR * 60.0 / self.tempo

    @property
    def phrase_seconds(self) -> float:
        return self.bars * self.bar_seconds

    @property
    def total_bars(self) -> int:
        return self.bars * sum(sec.repeats for sec in self.structure)

    @property
    def music_seconds(self) -> float:
        """Length up to the end of the last bar (where the next song starts)."""
        return self.total_bars * self.bar_seconds

    @property
    def tail_seconds(self) -> float:
        return max(MIN_TAIL_SECONDS, self.reverb_tail_bars * self.bar_seconds)

    @property
    def duration_seconds(self) -> float:
        """Length of the rendered file: music plus ring-out."""
        return self.music_seconds + self.tail_seconds

    def section_times(self) -> list[dict]:
        out, t = [], 0.0
        for sec in self.structure:
            out.append({"kind": sec.kind, "start": round(t, 3)})
            t += sec.repeats * self.phrase_seconds
        return out

    def audio_hash(self) -> str:
        """Hash of everything that affects the audio."""
        blob = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(blob.encode()).hexdigest()[:16]


# Numeric bounds used to clamp untrusted dicts (LLM output) before validation.
_CLAMPS: dict[tuple[str, ...], tuple[float, float]] = {
    ("bars",): (4, 16),
    ("swing",): (0.0, 0.5),
    ("humanize",): (0.0, 1.0),
    ("reverb_tail_bars",): (0, 4),
    ("seed",): (0, 2**31 - 1),
    ("melody", "density"): (0.0, 1.0),
    ("melody", "rhythm_seed"): (0, 2**31 - 1),
    ("melody", "motif_bars"): (1, 2),
    ("bass_pattern", "octave"): (1, 3),
}


def clamp_spec_dict(raw: dict) -> dict:
    """Clamp known numeric fields of an untrusted dict into range.

    Only scalars are clamped; structure and allowlists are still enforced by
    SongSpec validation afterwards.
    """
    out = json.loads(json.dumps(raw))  # deep copy, JSON types only
    for path, (lo, hi) in _CLAMPS.items():
        node = out
        for p in path[:-1]:
            node = node.get(p) if isinstance(node, dict) else None
        if isinstance(node, dict) and isinstance(node.get(path[-1]), (int, float)) and not isinstance(node.get(path[-1]), bool):
            val = min(max(node[path[-1]], lo), hi)
            node[path[-1]] = int(val) if isinstance(lo, int) else float(val)
    tempo = out.get("tempo")
    if out.get("genre") in GENRE_TEMPO and isinstance(tempo, (int, float)) and not isinstance(tempo, bool):
        lo, hi = GENRE_TEMPO[out["genre"]]
        out["tempo"] = min(max(tempo, lo), hi)
    for prog in ("chord_progression", "alt_progression"):
        if isinstance(out.get(prog), list):
            out[prog] = out[prog][:16]
            for ch in out[prog]:
                if isinstance(ch, dict) and isinstance(ch.get("beats"), int):
                    ch["beats"] = min(max(ch["beats"], 1), 16)
    if isinstance(out.get("structure"), list):
        out["structure"] = out["structure"][:12]
        for sec in out["structure"]:
            if isinstance(sec, dict) and isinstance(sec.get("repeats"), int):
                sec["repeats"] = min(max(sec["repeats"], 1), 16)
    if isinstance(out.get("name"), str):
        out["name"] = out["name"][:60]
    return out
