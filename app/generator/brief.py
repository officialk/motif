"""SongBrief: the only thing an AI backend may return.

A brief is a handful of optional choices; the procedural generator turns it
into a full SongSpec deterministically. Allowlists are enforced here.
"""

from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schema import ROLE_PROGRAMS, Genre, Key, Scale


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
    seed: int = Field(ge=0, le=2**31 - 1)
    key: Optional[Key] = None
    scale: Optional[Scale] = None
    tempo: Optional[float] = Field(default=None, ge=40, le=200, description="BPM; clamped into the genre range later")
    energy: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="0 = slow end of the genre tempo range, 1 = fast end")
    density: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Melody note density")
    instruments: Optional[BriefInstruments] = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        if isinstance(v, str):
            v = " ".join(v.split())
        return v


_CLAMPS: dict[str, tuple[float, float]] = {
    "seed": (0, 2**31 - 1), "tempo": (40, 200), "energy": (0.0, 1.0), "density": (0.0, 1.0),
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
    return out
