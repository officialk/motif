"""FluidSynth engine: arrangement -> MIDI (mido) -> fluidsynth -> song with a natural ring-out."""

from __future__ import annotations

import os
import struct
import subprocess
import uuid
import wave
from pathlib import Path

import mido
import numpy as np

from app import config
from app.engine.arrange import CH_BASS, CH_DRUMS, CH_KEYS, CH_LEAD, CH_PAD, TPB, Arrangement, arrange
from app.engine.base import Engine
from app.schema import SongSpec

# channel -> (volume CC7, pan CC10)
_MIX = {
    CH_PAD: (72, 64),
    CH_LEAD: (96, 72),
    CH_BASS: (100, 64),
    CH_KEYS: (84, 54),
    CH_DRUMS: (100, 64),
}
TAIL_FADE_S = 1.5  # fade over the end of the ring-out so the file ends in silence
PEAK = 10 ** (-1.0 / 20)  # normalise to -1 dBFS


def build_midi(spec: SongSpec, arr: Arrangement) -> mido.MidiFile:
    mid = mido.MidiFile(ticks_per_beat=TPB, type=0)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    events: list[tuple[int, int, mido.Message | mido.MetaMessage]] = []
    reverb = min(127, 30 + 18 * spec.reverb_tail_bars)

    events.append((0, 0, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(spec.tempo))))
    for ch, prog in arr.programs.items():
        events.append((0, 1, mido.Message("program_change", channel=ch, program=prog)))
    if arr.drum_kit is not None:
        events.append((0, 1, mido.Message("program_change", channel=CH_DRUMS, program=arr.drum_kit)))
    for ch, (vol, pan) in _MIX.items():
        events.append((0, 1, mido.Message("control_change", channel=ch, control=7, value=vol)))
        events.append((0, 1, mido.Message("control_change", channel=ch, control=10, value=pan)))
        events.append((0, 1, mido.Message("control_change", channel=ch, control=91, value=reverb)))
    events.append((0, 1, mido.Message("control_change", channel=CH_PAD, control=93, value=50)))

    # Same pitch on the same channel must not overlap, or the first note-off cuts the second.
    notes = sorted(arr.notes, key=lambda n: (n.channel, n.pitch, n.start))
    for i, n in enumerate(notes):
        end = n.end
        if i + 1 < len(notes) and notes[i + 1].channel == n.channel and notes[i + 1].pitch == n.pitch:
            end = min(end, notes[i + 1].start)
        if end <= n.start:
            continue
        events.append((n.start, 3, mido.Message("note_on", channel=n.channel, note=n.pitch, velocity=n.vel)))
        events.append((end, 2, mido.Message("note_off", channel=n.channel, note=n.pitch, velocity=0)))

    # Keep the file running past the last bar so fluidsynth renders the whole ring-out.
    tail_ticks = round((spec.tail_seconds + 1.0) * spec.tempo / 60.0 * TPB)
    events.append((arr.music_ticks + tail_ticks, 9, mido.MetaMessage("marker", text="end")))

    events.sort(key=lambda e: (e[0], e[1]))
    now = 0
    for t, _, msg in events:
        track.append(msg.copy(time=t - now))
        now = t
    track.append(mido.MetaMessage("end_of_track", time=0))
    return mid


def read_wav_float(path: Path) -> tuple[np.ndarray, int]:
    """Read a PCM16 or float32 WAV into float32 (frames, channels)."""
    data = path.read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError(f"{path} is not a WAV file")
    pos, fmt, channels, rate, bits, payload = 12, None, 2, config.SAMPLE_RATE, 16, b""
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt, channels, rate = struct.unpack("<HHI", body[:8])
            bits = struct.unpack("<H", body[14:16])[0]
            if fmt == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE: real format in the subformat GUID
                fmt = struct.unpack("<H", body[24:26])[0]
        elif cid == b"data":
            payload = body
        pos += 8 + size + (size & 1)
    if fmt == 3 and bits == 32:
        arr = np.frombuffer(payload, dtype="<f4")
    elif fmt == 1 and bits == 16:
        arr = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
    else:
        raise ValueError(f"unsupported WAV format {fmt}/{bits}")
    return arr.reshape(-1, channels).astype(np.float32), rate


def write_wav16(path: Path, audio: np.ndarray, rate: int) -> None:
    pcm = np.clip(np.round(audio * 32767.0), -32768, 32767).astype("<i2")
    tmp = path.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(audio.shape[1])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    tmp.replace(path)


def fit_length(audio: np.ndarray, frames: int, fade_frames: int) -> np.ndarray:
    """Cut or pad to exactly `frames` and fade the last `fade_frames` to silence."""
    if len(audio) < frames:
        audio = np.vstack([audio, np.zeros((frames - len(audio), audio.shape[1]), np.float32)])
    out = audio[:frames].copy()
    fade = min(fade_frames, frames)
    if fade:
        out[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)[:, None]
    return out


class FluidSynthEngine(Engine):
    name = "fluidsynth"

    def __init__(self, soundfont: Path | None = None, cache_dir: Path | None = None, gain: float = 0.4,
                 media=None):
        super().__init__(cache_dir, media)
        self.soundfont = Path(soundfont or config.find_soundfont())
        self.gain = gain

    def render_wav(self, spec: SongSpec) -> Path:
        rate = config.SAMPLE_RATE
        arr = arrange(spec)
        tag = f"tmp-{spec.audio_hash()}-{uuid.uuid4().hex[:6]}"
        mid_path = self.cache_dir / f"{tag}.mid"
        raw_path = self.cache_dir / f"{tag}.raw.wav"
        try:
            build_midi(spec, arr).save(mid_path)
            subprocess.run(
                ["fluidsynth", "-ni", "-q", "-g", str(self.gain), "-O", "float", "-r", str(rate),
                 "-F", str(raw_path), str(self.soundfont), str(mid_path)],
                check=True, capture_output=True, timeout=300,
                env={**os.environ, "LANG": "C"},
            )
            audio, got_rate = read_wav_float(raw_path)
            if got_rate != rate:
                raise RuntimeError(f"fluidsynth rendered at {got_rate} Hz, expected {rate}")
            frames = round(spec.duration_seconds * rate)
            fade = min(int(TAIL_FADE_S * rate), round(spec.tail_seconds * rate))
            out = fit_length(audio, frames, fade)
            peak = float(np.max(np.abs(out))) or 1.0
            out *= PEAK / peak
            dest = self.wav_path(spec)
            write_wav16(dest, out, rate)
            return dest
        finally:
            mid_path.unlink(missing_ok=True)
            raw_path.unlink(missing_ok=True)
