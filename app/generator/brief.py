"""SongBrief: the only thing an AI backend may return.

A brief is a handful of optional musical choices; the procedural generator turns
it into a full SongSpec. The seed is never the AI's: the server always picks it
(randomly) and uses it only for the details the brief leaves open. Allowlists are
enforced here.
"""

from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schema import ROLE_PROGRAMS, BassStyle, Genre, Key, KeysStyle, Scale


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BriefInstruments(_Strict):
    pad: Optional[int] = None
    lead: Optional[int] = None
    bass: Optional[int] = None
    keys: Optional[int] = None

    @field_validator("pad", "lead", "bass", "keys")
    @classmethod
    def _allowlist(cls, v: Optional[int], info) -> Optional[int]:
        if v is not None and v not in ROLE_PROGRAMS[info.field_name]:
            raise ValueError(f"program {v} not allowed for role {info.field_name}")
        return v


class SongBrief(_Strict):
    genre: Optional[Genre] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    seed: int = Field(default=0, ge=0, le=2**31 - 1, description="Set by the server, never by the AI")
    mood: Optional[str] = Field(default=None, max_length=120,
                                description="Short description of this song's feel; steers instruments and tempo")
    key: Optional[Key] = None
    scale: Optional[Scale] = None
    tempo: Optional[float] = Field(default=None, ge=40, le=200, description="BPM; clamped into the genre range later")
    energy: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="0 = slow end of the genre tempo range, 1 = fast end")
    density: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Melody note density")
    instruments: Optional[BriefInstruments] = None
    bass_style: Optional[BassStyle] = None
    keys_style: Optional[KeysStyle] = None
    swing: Optional[float] = Field(default=None, ge=0.0, le=0.5, description="0 = straight, 0.5 = heavy swing")
    drums: Optional[bool] = Field(default=None, description="false = no drums for this song")

    @field_validator("name", "mood", mode="before")
    @classmethod
    def _name(cls, v):
        if isinstance(v, str):
            v = " ".join(v.split())
        return v


def ai_schema() -> dict:
    """The brief's JSON schema as shown to an AI: everything except the server-only seed."""
    schema = SongBrief.model_json_schema()
    schema["properties"].pop("seed", None)
    schema["required"] = [r for r in schema.get("required", []) if r != "seed"]
    return schema


_CLAMPS: dict[str, tuple[float, float]] = {
    "seed": (0, 2**31 - 1), "tempo": (40, 200), "energy": (0.0, 1.0), "density": (0.0, 1.0), "swing": (0.0, 0.5),
}


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def clamp_brief_dict(raw: dict) -> dict:
    """Clamp numeric scalars and truncate the name of an untrusted dict.

    Unknown top-level keys are dropped; allowlists (genre, key, scale,
    instrument programs) are still enforced by SongBrief validation.
    """
    out = {k: v for k, v in json.loads(json.dumps(raw)).items() if k in SongBrief.model_fields}
    for field, (lo, hi) in _CLAMPS.items():
        if _num(out.get(field)):
            val = min(max(out[field], lo), hi)
            out[field] = int(val) if field == "seed" else float(val)
    if isinstance(out.get("name"), str):
        out["name"] = " ".join(out["name"].split())[:60].strip() or None
    if isinstance(out.get("mood"), str):
        out["mood"] = " ".join(out["mood"].split())[:120].strip() or None
    return out
