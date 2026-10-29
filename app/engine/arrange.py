"""SongSpec -> note events. Engine-agnostic, deterministic for a given spec.

A song is a list of sections; each section plays the base phrase (`spec.bars`
bars) `repeats` times. The section kind decides which parts play and how:
intros and outros are thin, breakdowns drop the drums, builds bring them back,
variations use the second progression and a contrasting melody, returns bring
the main theme back with more energy. The last bar is a held tonic cadence.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from app.schema import BEATS_PER_BAR, DRUM_NOTES, KEYS, SCALES, STEPS, Chord, SongSpec

TPB = 480  # ticks per beat
STEP = TPB // 4  # one 16th note
BAR = STEP * STEPS

CH_PAD, CH_LEAD, CH_BASS, CH_KEYS, CH_DRUMS = 0, 1, 2, 3, 9


@dataclass(frozen=True)
class Note:
    channel: int
    start: int
    end: int
    pitch: int
    vel: int


@dataclass
class Arrangement:
    notes: list[Note]
    music_ticks: int  # end of the last bar
    programs: dict[int, int] = field(default_factory=dict)  # channel -> program
    drum_kit: int | None = None


@dataclass(frozen=True)
class ChordSpan:
    start: int  # tick
    end: int
    chord: Chord


@dataclass(frozen=True)
class Layer:
    """What each part does in a section kind."""
    bass: str  # "on" | "off" | "drone" | "late" (last repeat only) | "early" (first repeat only)
    drums: str  # "full" | "full+" | "light" | "build" | "off" | "early_light"
    lead: str  # "A" | "B" | "A+" | "sparse" | "off"
    keys: str  # "style" (the spec's keys_style) | "sustain"
    energy: float  # velocity scale
    alt: bool = False  # use alt_progression when there is one


LAYERS: dict[str, Layer] = {
    "intro": Layer(bass="late", drums="light", lead="off", keys="style", energy=0.75),
    "main": Layer(bass="on", drums="full", lead="A", keys="style", energy=0.9),
    "variation": Layer(bass="on", drums="full+", lead="B", keys="style", energy=1.0, alt=True),
    "breakdown": Layer(bass="drone", drums="off", lead="sparse", keys="sustain", energy=0.72, alt=True),
    "build": Layer(bass="on", drums="build", lead="off", keys="style", energy=0.88),
    "return": Layer(bass="on", drums="full+", lead="A+", keys="style", energy=1.0),
    "outro": Layer(bass="early", drums="early_light", lead="sparse", keys="style", energy=0.75),
}


@dataclass(frozen=True)
class Phrase:
    kind: str
    layer: Layer
    start: int  # tick
    index: int  # global phrase number
    rep: int  # repeat number inside the section
    reps: int
    final: bool  # last phrase of the song

    @property
    def last_in_section(self) -> bool:
        return self.rep == self.reps - 1


class Theory:
    """Scale/chord helpers for one spec."""

    def __init__(self, spec: SongSpec):
        self.scale = SCALES[spec.scale]
        self.pc = KEYS.index(spec.key)

    def deg_to_semi(self, idx: int) -> int:
        """Diatonic index (0 = tonic, may be negative/over 7) -> semitones from tonic."""
        return self.scale[idx % 7] + 12 * (idx // 7)

    @staticmethod
    def chord_indexes(chord: Chord) -> list[int]:
        r = chord.degree - 1
        ext = {
            "triad": [0, 2, 4], "7": [0, 2, 4, 6], "9": [0, 2, 4, 6, 8],
            "sus2": [0, 1, 4], "sus4": [0, 3, 4],
        }[chord.ext]
        return [r + e for e in ext]

    @staticmethod
    def is_chord_tone(idx: int, chord: Chord) -> bool:
        return any((idx - c) % 7 == 0 for c in Theory.chord_indexes(chord))

    def tonic_midi(self, octave: int) -> int:
        return 12 * (octave + 1) + self.pc

    def voicing(self, chord: Chord, center: int, prev: list[int] | None) -> list[int]:
        """Chord tones placed near `center` (or the previous voicing) for smooth voice leading."""
        target = sum(prev) / len(prev) if prev else center
        base = self.tonic_midi(4)
        pitches = []
        for idx in self.chord_indexes(chord):
            p = base + self.deg_to_semi(idx)
            while p - target > 6:
                p -= 12
            while target - p > 6:
                p += 12
            pitches.append(p)
        return sorted(set(pitches))


def phrase_spans(spec: SongSpec, ph: Phrase, phrase_ticks: int) -> list[ChordSpan]:
    prog = spec.alt_progression if ph.layer.alt and spec.alt_progression else spec.chord_progression
    end_at = ph.start + phrase_ticks - (BAR if ph.final else 0)
    spans: list[ChordSpan] = []
    t, i = ph.start, 0
    while t < end_at:
        ch = prog[i % len(prog)]
        end = min(t + ch.beats * TPB, end_at)
        spans.append(ChordSpan(t, end, ch))
        t, i = end, i + 1
    if ph.final:  # cadence: one bar of tonic
        first = spec.chord_progression[0]
        spans.append(ChordSpan(end_at, end_at + BAR, Chord(degree=1, ext=first.ext if first.degree == 1 else "triad", beats=4)))
    return spans


def chord_at(spans: list[ChordSpan], tick: int) -> ChordSpan:
    for s in spans:
        if s.start <= tick < s.end:
            return s
    return spans[-1] if tick >= spans[-1].start else spans[0]


class _Placer:
    """Applies swing and humanize, keeps notes inside the song."""

    def __init__(self, spec: SongSpec, song_ticks: int, rng: random.Random):
        self.swing = spec.swing
        self.grid = spec.swing_grid
        self.hum = spec.humanize
        self.end = song_ticks
        self.rng = rng
        self.notes: list[Note] = []

    def step_tick(self, step_index: int) -> int:
        """Swung tick of a 16th step counted from the song start."""
        t = step_index * STEP
        pos = step_index % 4
        if self.grid == 16 and pos % 2 == 1:
            t += int(self.swing * STEP)
        elif self.grid == 8 and pos:
            # Delay the off-beat 8th by 2*swing 16ths; the 16ths around it follow proportionally.
            t += int(self.swing * STEP * (2 if pos == 2 else 1))
        return t

    def add(self, ch: int, start: int, dur: int, pitch: int, vel: float, jitter: bool = True) -> None:
        vel = int(vel)
        if jitter and self.hum > 0:
            start += int(self.rng.uniform(-1, 1) * self.hum * 18)
            vel += int(self.rng.uniform(-1, 1) * self.hum * 14)
        start = max(0, min(start, self.end - 2))
        end = min(start + max(dur, 1), self.end)
        if end <= start or not 0 <= pitch <= 127:
            return
        self.notes.append(Note(ch, start, end, pitch, max(1, min(127, vel))))


def phrases(spec: SongSpec) -> list[Phrase]:
    phrase_ticks = spec.bars * BAR
    out, n = [], 0
    total = sum(s.repeats for s in spec.structure)
    for sec in spec.structure:
        for rep in range(sec.repeats):
            out.append(Phrase(sec.kind, LAYERS[sec.kind], n * phrase_ticks, n, rep, sec.repeats, n == total - 1))
            n += 1
    return out


def arrange(spec: SongSpec) -> Arrangement:
    phrase_ticks = spec.bars * BAR
    song_ticks = spec.total_bars * BAR
    th = Theory(spec)
    ins = spec.instruments
    mel = _MelodyMaterial(spec) if ins.lead is not None and spec.melody.enabled else None

    def placer(part: str) -> _Placer:
        return _Placer(spec, song_ticks, random.Random(f"{spec.seed}:{part}"))

    parts = {name: placer(name) for name in ("pad", "keys", "bass", "drums", "melody")}
    voicing_state: dict[str, list[int] | None] = {"pad": None, "keys": None}
    for ph in phrases(spec):
        spans = phrase_spans(spec, ph, phrase_ticks)
        if ins.pad is not None:
            _pad(th, spans, parts["pad"], ph, voicing_state)
        if ins.keys is not None:
            _keys(spec, th, spans, parts["keys"], ph, voicing_state)
        if ins.bass is not None:
            _bass(spec, th, spans, parts["bass"], ph)
        if ins.drum_kit is not None and spec.drum_pattern:
            _drums(spec, parts["drums"], ph)
        if mel is not None:
            mel.phrase(th, spans, parts["melody"], ph)

    programs = {ch: prog for ch, prog in (
        (CH_PAD, ins.pad), (CH_LEAD, ins.lead), (CH_BASS, ins.bass), (CH_KEYS, ins.keys),
    ) if prog is not None}
    notes = [n for p in parts.values() for n in p.notes]
    return Arrangement(notes=notes, music_ticks=song_ticks, programs=programs, drum_kit=ins.drum_kit)


# --- parts --------------------------------------------------------------

def _pad(th: Theory, spans: list[ChordSpan], p: _Placer, ph: Phrase, state: dict) -> None:
    vel = 58 * (0.85 + 0.15 * ph.layer.energy)
    for s in spans:
        v = th.voicing(s.chord, 57, state["pad"])
        state["pad"] = v
        for pitch in v:
            p.add(CH_PAD, s.start, s.end - s.start, pitch, vel, jitter=False)


_STAB_RHYTHMS = [[0, 6, 10], [0, 3, 8, 14], [2, 6, 12], [0, 7, 10, 13], [0, 4, 11]]
_COMP_RHYTHMS = [[0, 6], [2, 10], [0, 7, 12], [3, 10], [6, 14], [0, 11]]


def _keys(spec: SongSpec, th: Theory, spans: list[ChordSpan], p: _Placer, ph: Phrase, state: dict) -> None:
    style = "sustain" if ph.layer.keys == "sustain" else spec.keys_style
    e = ph.layer.energy
    voicings = {}
    for s in spans:
        state["keys"] = th.voicing(s.chord, 62, state["keys"])
        voicings[s.start] = state["keys"]
    # One stab rhythm per song so the hook is recognisable.
    stab = random.Random(f"{spec.seed}:stab").choice(_STAB_RHYTHMS)

    if style == "sustain":
        for s in spans:
            for pitch in voicings[s.start]:
                p.add(CH_KEYS, s.start, s.end - s.start, pitch, 64 * e)
        return

    body = spans[:-1] if ph.final else spans
    if ph.final:  # cadence chord held
        last = spans[-1]
        for pitch in voicings[last.start]:
            p.add(CH_KEYS, last.start, last.end - last.start, pitch, 62)
    body_end = body[-1].end
    for bar_start in range(ph.start, body_end, BAR):
        if style == "stabs":
            steps = stab
        elif style == "offbeat":
            steps = [2, 6, 10, 14]
        elif style == "comp":
            steps = p.rng.choice(_COMP_RHYTHMS)
        elif style in ("arpeggio", "alberti"):
            steps = list(range(16))
        else:  # broken: 8th notes
            steps = list(range(0, 16, 2))
        for n, st in enumerate(steps):
            step = bar_start // STEP + st
            tick = step * STEP
            span = chord_at(body, tick)
            v = voicings[span.start]
            if style in ("stabs", "offbeat", "comp"):
                length = STEP * (3 if style == "comp" else 1)
                vel = (78 if style != "comp" else 66) * e
                for pitch in v:
                    p.add(CH_KEYS, p.step_tick(step), length, pitch, vel)
            elif style == "arpeggio":
                up = v + [x + 12 for x in v]
                seq = up + up[-2:0:-1]
                p.add(CH_KEYS, p.step_tick(step), int(STEP * 0.9), seq[n % len(seq)], (70 if n % 4 else 84) * e)
            elif style == "alberti":
                low = [x - 12 for x in v]
                order = [low[0], low[-1], low[len(low) // 2], low[-1]]
                p.add(CH_KEYS, p.step_tick(step), int(STEP * 0.95), order[n % 4], (58 if n % 4 else 68) * e)
            else:
                order = [v[0] - 12, v[-1], v[len(v) // 2], v[-1] + 12 if n % 4 == 3 else v[-1]]
                p.add(CH_KEYS, p.step_tick(step), STEP * 2, order[n % 4], 62 * e)


def _bass(spec: SongSpec, th: Theory, spans: list[ChordSpan], p: _Placer, ph: Phrase) -> None:
    mode = ph.layer.bass
    if mode == "off" or (mode == "late" and not (ph.reps > 1 and ph.last_in_section)) \
            or (mode == "early" and not (ph.reps > 1 and ph.rep == 0)):
        if not ph.final:
            return
    bp = spec.bass_pattern
    base = th.tonic_midi(bp.octave)
    if base > 45:
        base -= 12
    e = ph.layer.energy

    def pitch_of(idx: int) -> int:
        semi = th.deg_to_semi(idx)
        p_ = base + semi % 12 + 12 * (semi // 12 if semi < 0 else 0)
        return p_ - 12 if p_ > base + 8 and p_ - 12 >= 24 else p_

    body = spans[:-1] if ph.final else spans
    if ph.final:
        last = spans[-1]
        p.add(CH_BASS, last.start, last.end - last.start, pitch_of(0), 84, jitter=False)
        if mode in ("off", "late", "early"):
            return

    if bp.style == "drone" or mode == "drone":
        for s in body:
            p.add(CH_BASS, s.start, s.end - s.start, pitch_of(s.chord.degree - 1), 76 * e, jitter=False)
        return

    onsets = [i for i, v in enumerate(bp.steps) if v]
    if not onsets:
        return
    body_end = body[-1].end
    for bar_start in range(ph.start, body_end, BAR):
        bar_step = bar_start // STEP
        bar_onsets = [(bar_step + i, bp.steps[i]) for i in onsets]
        for n, (step, vel) in enumerate(bar_onsets):
            tick = step * STEP
            span = chord_at(body, tick)
            root = span.chord.degree - 1
            tones = Theory.chord_indexes(span.chord)
            next_step = bar_onsets[n + 1][0] if n + 1 < len(bar_onsets) else bar_step + STEPS
            next_tick = next_step * STEP
            if bp.style == "root":
                pitch = pitch_of(root)
            elif bp.style == "root_fifth":
                pitch = pitch_of(root if n % 2 == 0 else root + 4)
            elif bp.style == "octave":
                pitch = pitch_of(root) + (12 if n % 2 else 0)
            elif bp.style == "arp":
                pitch = pitch_of(tones[n % len(tones)])
            else:  # walking: chord tone on the change, steps in between, approach note at the end
                if tick == span.start:
                    pitch = pitch_of(root)
                elif next_tick >= span.end:
                    nxt = chord_at(spans, span.end).chord.degree - 1
                    pitch = pitch_of(nxt) + p.rng.choice((-1, 1))
                else:
                    pitch = pitch_of(root + p.rng.choice((1, 2, 4, -1)))
            dur = int((next_tick - tick) * (0.95 if bp.style == "walking" else 0.85))
            if bp.style != "walking":
                dur = min(dur, span.end - tick)
            p.add(CH_BASS, p.step_tick(step), max(dur, STEP // 2), pitch, vel * e)


_LIGHT = ("closed_hat", "open_hat", "ride", "shaker", "rim")
_TIME = ("closed_hat", "ride", "shaker")


def _drums(spec: SongSpec, p: _Placer, ph: Phrase) -> None:
    mode = ph.layer.drums
    if mode == "early_light":
        mode = "light" if ph.rep == 0 and not ph.final else "off"
    e = ph.layer.energy
    bars = spec.bars - (1 if ph.final else 0)
    if ph.final:  # land the cadence
        bar0 = (ph.start + bars * BAR) // STEP
        p.add(CH_DRUMS, bar0 * STEP, STEP, DRUM_NOTES["kick"], 100, jitter=False)
        p.add(CH_DRUMS, bar0 * STEP, STEP, DRUM_NOTES["crash"], 90, jitter=False)
    if mode == "off":
        return
    rng = random.Random(f"{spec.seed}:drums:{ph.index}")
    pattern = spec.drum_pattern
    for bar in range(bars):
        bar_step = ph.start // STEP + bar * STEPS
        last_bar = bar == bars - 1
        fill = spec.drum_fill and last_bar and ph.last_in_section and not ph.final and mode != "light"
        if mode == "light":
            pieces = {k: v for k, v in pattern.items() if k in _LIGHT}
        elif mode == "build":
            progress = (ph.rep * bars + bar) / max(1, ph.reps * bars)
            keep = ("kick",) if progress < 0.5 else ("kick", *_TIME, "clap", "snare")
            pieces = {k: v for k, v in pattern.items() if k in keep}
        else:
            pieces = dict(pattern)
        for piece, grid in pieces.items():
            note = DRUM_NOTES[piece]
            for step, vel in enumerate(grid):
                if not vel or (fill and step >= 12 and piece != "kick"):
                    continue
                p.add(CH_DRUMS, p.step_tick(bar_step + step), STEP // 2, note, vel * e)
            if mode == "full+" and piece in _TIME:  # a few ghost notes for lift
                for step in range(1, 16, 2):
                    if not grid[step] and rng.random() < 0.18:
                        p.add(CH_DRUMS, p.step_tick(bar_step + step), STEP // 2, note, 45 * e)
        if bar == 0 and ph.rep == 0 and mode in ("full", "full+") and ph.index > 0:
            p.add(CH_DRUMS, bar_step * STEP, STEP, DRUM_NOTES["crash"], 95 * e)
        if mode == "build" and last_bar and ph.last_in_section:
            for i, step in enumerate(range(8, 16)):  # snare roll into the next section
                p.add(CH_DRUMS, p.step_tick(bar_step + step), STEP // 2, DRUM_NOTES["snare"], 60 + i * 7)
        elif fill:
            toms = [DRUM_NOTES["tom_high"], DRUM_NOTES["tom_high"], DRUM_NOTES["tom_low"], DRUM_NOTES["snare"]]
            for i, step in enumerate(range(12, 16)):
                p.add(CH_DRUMS, p.step_tick(bar_step + step), STEP // 2, toms[i], 70 + i * 10)


# --- melody -------------------------------------------------------------

def _motif_rhythm(rng: random.Random, length: int, density: float) -> list[int]:
    weights = []
    for i in range(length):
        s = i % STEPS
        w = 1.6 if s == 0 else 1.25 if s % 4 == 0 else 0.95 if s % 2 == 0 else 0.4
        weights.append(w)
    onsets = [i for i in range(length - 2) if rng.random() < min(0.95, density * weights[i])]
    if not onsets or onsets[0] > 2:
        onsets.insert(0, rng.choice((0, 2)) if density < 0.6 else 0)
    if len(onsets) < 2:
        onsets.append(length // 2)
    return sorted(set(onsets))


def _motif_pitches(rng: random.Random, n: int, start: int) -> list[int]:
    moves = [-2, -1, 1, 2, 0, 3, -3, 4, -4]
    weights = [0.17, 0.25, 0.25, 0.14, 0.05, 0.05, 0.05, 0.02, 0.02]
    out = [start]
    for _ in range(n - 1):
        nxt = out[-1] + rng.choices(moves, weights)[0]
        if abs(nxt) > 6:  # stay in range around the register center
            nxt = out[-1] - (nxt - out[-1])
        out.append(nxt)
    return out


class _MelodyMaterial:
    """Two contrasting motifs per song; every phrase is built from them."""

    def __init__(self, spec: SongSpec):
        m = spec.melody
        self.spec = spec
        rng = random.Random(f"{spec.seed}:{m.rhythm_seed}:motif")
        self.mlen = m.motif_bars * STEPS
        first = spec.chord_progression[0]
        self.first_degree = first.degree
        start = rng.choice(Theory.chord_indexes(first)) % 7
        # Theme A and its answer
        self.a_rhythm = _motif_rhythm(rng, self.mlen, m.density)
        self.a = _motif_pitches(rng, len(self.a_rhythm), start)
        self.a_answer = _motif_pitches(rng, len(self.a_rhythm), rng.choice((2, 4, 5)))
        # Theme B: its own rhythm and contour for the variation sections
        self.b_rhythm = _motif_rhythm(rng, self.mlen, min(1.0, m.density * 1.15))
        self.b = _motif_pitches(rng, len(self.b_rhythm), rng.choice((1, 3, 4)))
        self.b_answer = _motif_pitches(rng, len(self.b_rhythm), rng.choice((0, 2, 5)))
        self.octave = {"low": 4, "mid": 4, "high": 5}[m.register]
        self.center = {"low": 0, "mid": 4, "high": 2}[m.register]

    def _mutate(self, pitches: list[int], rng: random.Random) -> list[int]:
        """Small change per repeat so a theme never comes back exactly the same."""
        out = list(pitches)
        for _ in range(1 + (len(out) > 5)):
            i = rng.randrange(1, len(out)) if len(out) > 1 else 0
            out[i] += rng.choice((-2, -1, 1, 2))
        return out

    def phrase(self, th: Theory, spans: list[ChordSpan], p: _Placer, ph: Phrase) -> None:
        mode = ph.layer.lead
        if mode == "off":
            if ph.final:
                self._cadence(th, spans, p)
            return
        spec = self.spec
        rng = random.Random(f"{spec.seed}:melody:{ph.index}")
        if mode == "B":
            rhythm, main, answer = self.b_rhythm, self.b, self.b_answer
        else:
            rhythm, main, answer = self.a_rhythm, self.a, self.a_answer
        if ph.rep > 0:
            main = self._mutate(main, rng)
        lift = 7 if mode == "A+" and spec.melody.register != "high" else 0
        sparse = mode == "sparse"

        bars = spec.bars - (1 if ph.final else 0)
        units = max(1, bars // spec.melody.motif_bars)
        form = ["P", "P2", "Q", "P3"]
        events: list[tuple[int, int]] = []  # (step from song start, diatonic index)
        for u in range(units):
            kind = form[u % 4] if units > 1 else "P"
            off = ph.start // STEP + u * self.mlen
            r = rhythm
            span = chord_at(spans, off * STEP)
            if kind == "P2":
                shift = ((span.chord.degree - self.first_degree + 3) % 7) - 3
                r = rhythm[:-2] + rhythm[-1:] if len(rhythm) >= 3 else rhythm
                ps = [x + shift for x in main[: len(r)]]
            elif kind == "Q":
                ps = answer
            else:
                ps = list(main)
            if sparse:
                r, ps = r[::2], ps[::2]
            events += [(off + st, d + lift) for st, d in zip(r, ps)]
        if events and (ph.last_in_section or ph.final):
            st, d = events[-1]
            events[-1] = (st, 7 * (d // 7) if abs(d) >= 4 else 0)

        end_step = (ph.start + bars * BAR) // STEP
        e = ph.layer.energy
        for i, (step, d) in enumerate(events):
            if step >= end_step:
                break
            span = chord_at(spans, step * STEP)
            if step % 4 == 0 and not Theory.is_chord_tone(d, span.chord):
                for delta in (1, -1, 2, -2):  # snap strong beats to the nearest chord tone
                    if Theory.is_chord_tone(d + delta, span.chord):
                        d += delta
                        break
            nxt = events[i + 1][0] if i + 1 < len(events) else end_step
            length = min(nxt - step, 16 if sparse else 8) * STEP
            vel = (96 if step % 4 == 0 else 84 if step % 2 == 0 else 74) * e
            p.add(CH_LEAD, p.step_tick(step), int(length * 0.92), self._pitch(th, d), vel)
        if ph.final:
            self._cadence(th, spans, p)

    def _pitch(self, th: Theory, d: int) -> int:
        pitch = th.tonic_midi(self.octave) + th.deg_to_semi(d + self.center)
        while pitch > 91:
            pitch -= 12
        while pitch < 55:
            pitch += 12
        return pitch

    def _cadence(self, th: Theory, spans: list[ChordSpan], p: _Placer) -> None:
        last = spans[-1]
        p.add(CH_LEAD, last.start, last.end - last.start, self._pitch(th, -self.center), 82, jitter=False)
