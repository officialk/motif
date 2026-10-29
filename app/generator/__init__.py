"""Hybrid spec generation: procedural by default, Claude when enabled and asked for."""

from __future__ import annotations

import re
from typing import Optional

from app.schema import SongSpec

MOOD_MAX = 200


def clean_mood(mood: Optional[str]) -> Optional[str]:
    """User mood text is data: printable characters only, collapsed whitespace, max 200 chars."""
    if not mood:
        return None
    text = re.sub(r"[^\w\s,.'!?&-]", " ", mood, flags=re.UNICODE)
    text = " ".join(text.split())[:MOOD_MAX]
    return text or None


def make_spec(genre: Optional[str], mood: Optional[str], use_claude: bool) -> tuple[SongSpec, str]:
    """Returns (spec, source). AI is used only when asked for and a backend is configured."""
    from app.generator import ai

    return ai.make_spec(genre, mood, use_claude)
