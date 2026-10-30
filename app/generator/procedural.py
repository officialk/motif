"""Procedural SongSpec generator: per-genre rules, randomised within them by seed.

Same (genre, mood, seed) -> same SongSpec. All patterns here are generic genre
conventions (four-on-the-floor, backbeat, ii-V-I...), never quotes of songs.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from app.schema import GENRE_TEMPO, GENRES, KEYS, SongSpec

if TYPE_CHECKING:
    from app.generator.brief import SongBrief

SONG_SECONDS = (180.0, 300.0)  # target length range, 3-5 minutes

# Drum/bass grid notation: X accent, x normal, o ghost, . rest. 16 chars = one bar.
_VEL = {"X": 112, "x": 92, "o": 52, ".": 0}


def grid(s: str) -> list[int]:
    s = s.replace(" ", "")
    assert len(s) == 16, s
    return [_VEL[c] for c in s]


Prog = list[tuple[int, str, int]]  # (degree, ext, beats)
Form = tuple[tuple[str, int], ...]  # (section kind, relative weight)

# Song forms. Weights are relative; repeats are scaled to hit the target length.
STANDARD: Form = (("intro", 1), ("main", 2), ("variation", 2), ("breakdown", 1), ("build", 1), ("return", 2), ("outro", 1))
CLUB: Form = (("intro", 2), ("main", 2), ("breakdown", 1), ("build", 1), ("variation", 2), ("return", 2), ("outro", 2))
FLOWING: Form = (("intro", 1), ("main", 2), ("variation", 2), ("breakdown", 2), ("return", 2), ("outro", 1))
SONG: Form = (("intro", 1), ("main", 2), ("variation", 2), ("return", 2), ("outro", 1))


@dataclass(frozen=True)
class GenreRules:
    scales: tuple[str, ...]
    progressions: tuple[Prog, ...]
    drums: tuple[dict[str, str], ...]  # empty tuple = no drums
    bass_styles: tuple[str, ...]
    bass_grids: tuple[str, ...]
    keys_styles: tuple[str, ...]
    pad: tuple[Optional[int], ...]
    lead: tuple[Optional[int], ...]
    bass: tuple[Optional[int], ...]
    keys: tuple[Optional[int], ...]
    kits: tuple[int, ...]
    swing: tuple[float, float]
    swing_grid: int
    humanize: tuple[float, float]
    bars: tuple[int, ...]
    tail: tuple[int, ...]
    density: tuple[float, float]
    registers: tuple[str, ...]
    fill_chance: float = 0.4
    forms: tuple[Form, ...] = (STANDARD,)
    bass_octave: tuple[int, ...] = (2,)
    words: tuple[tuple[str, ...], tuple[str, ...]] = field(default=((), ()))


T, S7, S9, SUS2, SUS4 = "triad", "7", "9", "sus2", "sus4"

RULES: dict[str, GenreRules] = {
    "lofi": GenreRules(
        scales=("dorian", "minor", "major", "mixolydian"),
        progressions=(
            [(1, S7, 4), (4, S7, 4), (6, S7, 4), (5, S7, 4)],
            [(2, S7, 4), (5, S7, 4), (1, S7, 8)],
            [(1, S9, 8), (4, S9, 8)],
            [(6, S7, 4), (4, S7, 4), (1, S7, 4), (5, SUS4, 4)],
            [(1, S7, 4), (3, S7, 4), (4, S7, 4), (4, S9, 4)],
        ),
        drums=(
            {"kick": "X......x..x.....", "snare": "....X.......X...", "closed_hat": "x.x.x.x.x.xox.x."},
            {"kick": "X.....x...X..x..", "snare": "....X.......X..o", "closed_hat": "x.xox.x.x.x.x.xo"},
            {"kick": "X..x......X.....", "rim": "....x.......x...", "closed_hat": "x.x.x.x.x.x.x.x.", "shaker": "..o...o...o...o."},
        ),
        bass_styles=("root", "root_fifth"),
        bass_grids=("X......x..x.....", "X.....x...X.....", "X..x......x....."),
        keys_styles=("comp", "sustain", "broken"),
        pad=(89, 48, None), lead=(11, 73, 24, 4), bass=(33, 32, 35), keys=(4, 5, 0),
        kits=(0, 8), swing=(0.2, 0.35), swing_grid=16, humanize=(0.3, 0.5),
        bars=(4, 8), tail=(1, 2), density=(0.25, 0.45), registers=("mid", "high"),
        forms=(STANDARD, SONG,),
        words=(("Dusty", "Sleepy", "Rainy", "Late", "Faded", "Warm", "Quiet", "Paper"),
               ("Window", "Tape", "Notebook", "Tea", "Streetlight", "Bedroom", "Cassette", "Sunday")),
    ),
    "ambient": GenreRules(
        scales=("lydian", "major", "dorian", "minor"),
        progressions=(
            [(1, S9, 8), (4, S9, 8)],
            [(1, SUS2, 8), (6, S7, 8)],
            [(1, T, 16), (5, SUS4, 16)],
            [(4, S9, 8), (1, SUS2, 8), (6, S7, 8), (5, SUS2, 8)],
        ),
        drums=(),
        bass_styles=("drone",), bass_grids=("X...............",),
        keys_styles=("broken", "sustain"),
        pad=(88, 89, 91, 92, 94, 95, 49), lead=(73, 11, 8, 9, None), bass=(38, 35, None), keys=(46, 8, 11, None),
        kits=(0,), swing=(0.0, 0.0), swing_grid=16, humanize=(0.2, 0.4),
        bars=(8, 16), tail=(3, 4), density=(0.08, 0.2), registers=("high", "mid"),
        fill_chance=0.0, bass_octave=(1, 2),
        forms=(FLOWING, SONG,),
        words=(("Slow", "Pale", "Distant", "Drifting", "Glass", "Hollow", "Silver", "Still"),
               ("Horizon", "Tide", "Fog", "Aurora", "Field", "Orbit", "Snowfall", "Lake")),
    ),
    "synthwave": GenreRules(
        scales=("minor", "dorian", "minor", "major"),
        progressions=(
            [(1, T, 4), (6, T, 4), (3, T, 4), (7, T, 4)],
            [(1, T, 4), (7, T, 4), (6, T, 4), (7, T, 4)],
            [(1, T, 4), (4, T, 4), (6, T, 4), (5, T, 4)],
            [(6, T, 8), (7, T, 8)],
        ),
        drums=(
            {"kick": "X...X...X...X...", "snare": "....X.......X...", "closed_hat": "..x...x...x...x."},
            {"kick": "X.......X.x.....", "snare": "....X.......X...", "closed_hat": "x.x.x.x.x.x.x.x.", "tom_low": "..............o."},
        ),
        bass_styles=("octave", "root"),
        bass_grids=("x.x.x.x.x.x.x.x.", "xxxxxxxxxxxxxxxx", "X.x.X.x.X.x.X.x."),
        keys_styles=("arpeggio", "stabs", "sustain"),
        pad=(90, 94, 89), lead=(81, 80, 87, 84), bass=(38, 39), keys=(81, 4, 5),
        kits=(25, 24), swing=(0.0, 0.05), swing_grid=16, humanize=(0.05, 0.15),
        bars=(4, 8), tail=(1, 2), density=(0.3, 0.5), registers=("mid", "high"),
        forms=(STANDARD, CLUB,),
        words=(("Neon", "Chrome", "Midnight", "Electric", "Laser", "Violet", "Turbo", "Coastal"),
               ("Drive", "Skyline", "Arcade", "Boulevard", "Sunset", "Grid", "Highway", "Pulse")),
    ),
    "jazz": GenreRules(
        scales=("major", "dorian", "mixolydian"),
        progressions=(
            [(2, S7, 4), (5, S7, 4), (1, S7, 8)],
            [(1, S7, 4), (6, S7, 4), (2, S7, 4), (5, S7, 4)],
            [(3, S7, 4), (6, S7, 4), (2, S7, 4), (5, S7, 4)],
            [(1, S9, 4), (4, S9, 4), (3, S7, 4), (6, S7, 4)],
        ),
        drums=(
            {"ride": "x...x.x.x...x.x.", "closed_hat": "....x.......x...", "kick": "o.......o.......", "snare": "......o.......o."},
            {"ride": "x...x.x.x...x.x.", "closed_hat": "....x.......x...", "snare": "..........o....."},
        ),
        bass_styles=("walking",), bass_grids=("x...x...x...x...",),
        keys_styles=("comp",),
        pad=(None, None, 48), lead=(65, 66, 56, 26, 11, 73), bass=(32,), keys=(0, 4, 26),
        kits=(32, 40), swing=(0.25, 0.4), swing_grid=8, humanize=(0.25, 0.4),
        bars=(4, 8), tail=(1, 2), density=(0.35, 0.6), registers=("mid",),
        fill_chance=0.2, bass_octave=(2,),
        forms=(SONG, STANDARD,),
        words=(("Blue", "Smoky", "Velvet", "After-Hours", "Quiet", "Brass", "Corner", "Basement"),
               ("Lounge", "Avenue", "Room", "Hour", "Club", "Stroll", "Walk", "Waltz")),
    ),
    "techno": GenreRules(
        scales=("minor", "phrygian", "dorian"),
        progressions=(
            [(1, T, 16)],
            [(1, T, 8), (6, T, 8)],
            [(1, SUS2, 8), (7, T, 8)],
            [(1, T, 16), (2, T, 16)],
        ),
        drums=(
            {"kick": "X...X...X...X...", "clap": "....x.......x...", "closed_hat": "..x...x...x...x.", "open_hat": "..o.......o....."},
            {"kick": "X...X...X...X...", "rim": "...o..o....o..o.", "closed_hat": "xoxoxoxoxoxoxoxo", "open_hat": "..x...x...x...x."},
            {"kick": "X...X...X...X...", "clap": "....x.......x...", "shaker": "oxoxoxoxoxoxoxox", "rim": "..........o....."},
        ),
        bass_styles=("root", "octave"),
        bass_grids=("..x...x...x...x.", ".xx..xx..xx..xx.", "..xx..x...xx..x."),
        keys_styles=("stabs", "offbeat"),
        pad=(90, 95, None), lead=(81, 84, 87, None), bass=(38, 39), keys=(81, 80, None),
        kits=(24, 25), swing=(0.0, 0.08), swing_grid=16, humanize=(0.0, 0.1),
        bars=(8, 16), tail=(1,), density=(0.12, 0.3), registers=("high", "mid"),
        bass_octave=(1, 2),
        forms=(CLUB, STANDARD,),
        words=(("Concrete", "Steel", "Warehouse", "Night", "Static", "Iron", "Deep", "Cold"),
               ("Circuit", "Signal", "Tunnel", "Machine", "Engine", "Floor", "Current", "Relay")),
    ),
    "chiptune": GenreRules(
        scales=("major", "minor", "mixolydian", "dorian"),
        progressions=(
            [(1, T, 4), (5, T, 4), (6, T, 4), (4, T, 4)],
            [(1, T, 4), (4, T, 4), (5, T, 4), (1, T, 4)],
            [(6, T, 4), (4, T, 4), (1, T, 4), (5, T, 4)],
            [(1, T, 2), (6, T, 2), (4, T, 2), (5, T, 2)],
        ),
        drums=(
            {"kick": "X...X...X...X...", "snare": "....X.......X..x", "closed_hat": "x.x.x.x.x.x.x.x."},
            {"kick": "X.....X.X.......", "snare": "....X.......X...", "closed_hat": "xxxxxxxxxxxxxxxx"},
        ),
        bass_styles=("octave", "arp"),
        bass_grids=("x.x.x.x.x.x.x.x.", "x.xxx.xxx.xxx.xx"),
        keys_styles=("arpeggio",),
        pad=(None,), lead=(80, 81), bass=(80, 81), keys=(80, 81),
        kits=(24, 25), swing=(0.0, 0.0), swing_grid=16, humanize=(0.0, 0.0),
        bars=(4, 8), tail=(0, 1), density=(0.5, 0.75), registers=("high",),
        bass_octave=(2, 3),
        forms=(STANDARD, SONG,),
        words=(("Pixel", "Tiny", "Bit", "Turbo", "Super", "Jumping", "Lost", "Hyper"),
               ("Castle", "Quest", "Dungeon", "Rocket", "Hero", "Coin", "Level", "Forest")),
    ),
    "classical-piano": GenreRules(
        scales=("major", "minor", "harmonic_minor"),
        progressions=(
            [(1, T, 4), (4, T, 4), (5, S7, 4), (1, T, 4)],
            [(1, T, 4), (6, T, 4), (2, T, 4), (5, S7, 4)],
            [(1, T, 4), (5, T, 4), (6, T, 4), (3, T, 4), (4, T, 4), (1, T, 4), (4, T, 4), (5, S7, 4)],
            [(6, T, 4), (4, T, 4), (1, T, 4), (5, T, 4)],
        ),
        drums=(),
        bass_styles=("root",), bass_grids=("x...............",),
        keys_styles=("alberti", "broken", "arpeggio"),
        pad=(None, None, None, 48), lead=(0,), bass=(None,), keys=(0,),
        kits=(0,), swing=(0.0, 0.0), swing_grid=16, humanize=(0.25, 0.4),
        bars=(4, 8), tail=(1, 2), density=(0.35, 0.6), registers=("high", "mid"),
        fill_chance=0.0,
        forms=(SONG, FLOWING,),
        words=(("Little", "Morning", "Gentle", "Autumn", "Evening", "Simple", "Winter", "Lantern"),
               ("Prelude", "Study", "Etude", "Sonatina", "Nocturne", "Air", "Romance", "Song")),
    ),
    "house": GenreRules(
        scales=("minor", "dorian", "major", "mixolydian"),
        progressions=(
            [(1, S7, 4), (4, S7, 4)],
            [(2, S7, 4), (5, S7, 4), (1, S9, 8)],
            [(1, S9, 8), (6, S7, 8)],
            [(6, S7, 4), (5, T, 4), (4, S7, 4), (5, SUS4, 4)],
        ),
        drums=(
            {"kick": "X...X...X...X...", "clap": "....x.......x...", "open_hat": "..x...x...x...x.", "closed_hat": "o.o.o.o.o.o.o.o."},
            {"kick": "X...X...X...X...", "clap": "....x.......x...", "open_hat": "..x...x...x...x.", "shaker": "oxoxoxoxoxoxoxox"},
            {"kick": "X...X...X...X...", "snare": "....x.......x...", "closed_hat": "..x...x...x..xx.", "rim": "...o.....o....o."},
        ),
        bass_styles=("root", "octave", "root_fifth"),
        bass_grids=("..x..x....x..x..", "..x...x...x...x.", "x..x..x...x..x.."),
        keys_styles=("stabs", "offbeat"),
        pad=(89, 48, 50, None), lead=(81, 4, 11, 73, None), bass=(38, 39, 33), keys=(4, 0, 16, 17),
        kits=(25, 24, 16), swing=(0.05, 0.18), swing_grid=16, humanize=(0.05, 0.15),
        bars=(4, 8), tail=(1, 2), density=(0.2, 0.4), registers=("mid", "high"),
        bass_octave=(2,),
        forms=(CLUB, STANDARD,),
        words=(("Sunrise", "Rooftop", "Velvet", "Golden", "Late", "Open", "Warm", "Disco"),
               ("Groove", "Room", "Session", "Motion", "Heat", "Garden", "Party", "Feeling")),
    ),
    "trip-hop": GenreRules(
        scales=("minor", "harmonic_minor", "phrygian", "dorian"),
        progressions=(
            [(1, S7, 8), (6, S7, 8)],
            [(1, T, 4), (7, T, 4), (6, T, 4), (5, S7, 4)],
            [(1, S9, 8), (4, S7, 8)],
            [(1, T, 4), (2, T, 4), (6, S7, 4), (5, SUS4, 4)],
        ),
        drums=(
            {"kick": "X......X..X.....", "snare": "....X.......X...", "closed_hat": "x.xox.x.x.xox.x.", "rim": "...........o...."},
            {"kick": "X.........X..x..", "snare": "....X.......X...", "closed_hat": "x.x.x.x.x.x.x.x.", "shaker": "..o...o...o...o."},
        ),
        bass_styles=("root", "drone"),
        bass_grids=("X.......x.x.....", "X.........x....."),
        keys_styles=("sustain", "broken", "comp"),
        pad=(48, 49, 50, 95, 89), lead=(11, 73, 4, 71), bass=(33, 38, 35), keys=(4, 0, 11),
        kits=(8, 0, 40, 25), swing=(0.1, 0.25), swing_grid=16, humanize=(0.2, 0.4),
        bars=(4, 8), tail=(2, 3), density=(0.2, 0.4), registers=("mid",),
        forms=(STANDARD, FLOWING,),
        words=(("Grey", "Smoke", "Underwater", "Heavy", "Slow", "Shadow", "Velvet", "Hazy"),
               ("Harbour", "Signal", "Corridor", "Static", "Mirror", "Echo", "Ferry", "Weather")),
    ),
    "drum-and-bass": GenreRules(
        scales=("minor", "dorian", "phrygian"),
        progressions=(
            [(1, S9, 8), (6, S7, 8)],
            [(1, T, 16)],
            [(1, S7, 4), (4, S7, 4), (6, S7, 4), (5, SUS4, 4)],
            [(6, S7, 8), (7, T, 8)],
        ),
        drums=(
            {"kick": "X.........X.....", "snare": "....X.......X...", "closed_hat": "x.x.x.x.x.x.x.x.", "rim": "......o.......o."},
            {"kick": "X.........x..X..", "snare": "....X..o....X...", "closed_hat": "xoxoxoxoxoxoxoxo", "open_hat": "..............x."},
            {"kick": "X.....X...X.....", "snare": "....X.......X..o", "shaker": "oxoxoxoxoxoxoxox"},
        ),
        bass_styles=("drone", "root"),
        bass_grids=("X.........X.....", "X.....x...X.....", "X...............",),
        keys_styles=("sustain", "stabs", "arpeggio"),
        pad=(89, 90, 91, 94), lead=(81, 84, 11, None), bass=(38, 39), keys=(4, 81, 5, None),
        kits=(24, 25, 0), swing=(0.0, 0.05), swing_grid=16, humanize=(0.05, 0.15),
        bars=(8, 16), tail=(1, 2), density=(0.15, 0.35), registers=("high", "mid"),
        bass_octave=(1, 2),
        forms=(CLUB, STANDARD,),
        words=(("Liquid", "Midnight", "Rolling", "Neon", "Rain", "Rapid", "Static", "Deep"),
               ("Current", "Runner", "Tunnel", "Skyline", "Signal", "Pulse", "Overpass", "Motion")),
    ),
    "hip-hop": GenreRules(
        scales=("minor", "dorian", "harmonic_minor"),
        progressions=(
            [(1, S7, 8), (4, S7, 8)],
            [(1, S9, 4), (6, S7, 4), (4, S7, 4), (5, S7, 4)],
            [(6, S7, 8), (1, S7, 8)],
            [(1, T, 4), (7, T, 4), (6, S7, 8)],
        ),
        drums=(
            {"kick": "X......X..X.....", "snare": "....X.......X...", "closed_hat": "x.x.x.x.x.x.x.x."},
            {"kick": "X.....X...X..x..", "snare": "....X.......X...", "closed_hat": "x.xox.x.x.xox.x.", "open_hat": "..............x."},
            {"kick": "X..x......X.....", "clap": "....X.......X...", "closed_hat": "xxx.xxx.xxx.xxx."},
        ),
        bass_styles=("root", "root_fifth"),
        bass_grids=("X......x..X.....", "X.....x...X..x..", "X..x......X....."),
        keys_styles=("comp", "sustain", "stabs"),
        pad=(48, 89, None), lead=(11, 73, 65, 4, None), bass=(33, 38, 35), keys=(4, 0, 11),
        kits=(0, 8, 25), swing=(0.15, 0.3), swing_grid=16, humanize=(0.2, 0.4),
        bars=(4, 8), tail=(1, 2), density=(0.2, 0.4), registers=("mid",),
        forms=(STANDARD, SONG,),
        words=(("Concrete", "Corner", "Golden", "Late", "Basement", "Smooth", "Block", "Dusty"),
               ("Cypher", "Rooftop", "Crate", "Avenue", "Stoop", "Tape", "Session", "Borough")),
    ),
    "reggae": GenreRules(
        scales=("major", "mixolydian", "dorian", "minor"),
        progressions=(
            [(1, T, 4), (4, T, 4)],
            [(1, T, 4), (5, T, 4), (6, T, 4), (4, T, 4)],
            [(2, S7, 8), (1, T, 8)],
            [(1, T, 8), (7, T, 8)],
        ),
        drums=(
            {"kick": "........X.......", "rim": "........X.......", "closed_hat": "x.x.x.x.x.x.x.x."},
            {"kick": "........X.......", "snare": "........X.......", "closed_hat": "x.xxx.xxx.xxx.xx"},
            {"kick": "X.......X.......", "rim": "....x.......x...", "closed_hat": "..x...x...x...x."},
        ),
        bass_styles=("root_fifth", "root"),
        bass_grids=("X...x..xX..x.x..", "X..x..x.X.......", "..X.x...X.x.x..."),
        keys_styles=("offbeat",),
        pad=(None, 89), lead=(56, 65, 11, None), bass=(33, 34), keys=(27, 16, 0),
        kits=(0, 8), swing=(0.1, 0.2), swing_grid=16, humanize=(0.15, 0.3),
        bars=(4, 8), tail=(1, 2), density=(0.2, 0.4), registers=("mid",),
        forms=(SONG, STANDARD,),
        words=(("Island", "Sunny", "Easy", "Coastal", "Roots", "Harbour", "Palm", "Morning"),
               ("Skank", "Tide", "Yard", "Breeze", "Shore", "Riddim", "Market", "Shade")),
    ),
    "bossa-nova": GenreRules(
        scales=("major", "dorian", "mixolydian", "lydian"),
        progressions=(
            [(1, S7, 8), (2, S7, 4), (5, S7, 4)],
            [(1, S9, 4), (6, S7, 4), (2, S7, 4), (5, S7, 4)],
            [(2, S9, 8), (5, S7, 8)],
            [(4, S7, 4), (3, S7, 4), (2, S7, 4), (1, S9, 4)],
        ),
        drums=(
            {"kick": "X..X....X..X....", "rim": "..x..x....x..x..", "shaker": "xoxoxoxoxoxoxoxo"},
            {"kick": "X......xX......x", "rim": "...x..x...x..x..", "closed_hat": "x.x.x.x.x.x.x.x."},
        ),
        bass_styles=("root_fifth",),
        bass_grids=("X..x....X..x....", "X......xX......x"),
        keys_styles=("comp", "broken"),
        pad=(None, 48), lead=(73, 65, 66, 11, 24), bass=(32, 33), keys=(24, 0, 26),
        kits=(40, 32), swing=(0.05, 0.12), swing_grid=16, humanize=(0.2, 0.35),
        bars=(4, 8), tail=(1, 2), density=(0.3, 0.5), registers=("mid", "high"),
        forms=(SONG, FLOWING,),
        words=(("Ocean", "Lazy", "Sunlit", "Seaside", "Quiet", "Breezy", "Amber", "Velvet"),
               ("Terrace", "Promenade", "Afternoon", "Veranda", "Lagoon", "Cafe", "Sand", "Samba")),
    ),
    "funk": GenreRules(
        scales=("dorian", "mixolydian", "minor"),
        progressions=(
            [(1, S7, 16)],
            [(1, S9, 8), (4, S9, 8)],
            [(1, S7, 4), (4, S7, 4)],
            [(1, S7, 12), (7, T, 4)],
        ),
        drums=(
            {"kick": "X.....X...X..X..", "snare": "....X..o.o..X..o", "closed_hat": "xxxxxxxxxxxxxxxx"},
            {"kick": "X..X......X.X...", "snare": "....X.o.....X.o.", "closed_hat": "x.xxx.xxx.xxx.xx", "open_hat": "......x........."},
        ),
        bass_styles=("octave", "root_fifth"),
        bass_grids=("X..x..X.x.X..x..", "X.xX..x...X.x..x", "X..x.x..X..x.x.."),
        keys_styles=("stabs", "offbeat"),
        pad=(None,), lead=(61, 65, 81, 56), bass=(36, 37, 33), keys=(7, 27, 16),
        kits=(0, 8), swing=(0.05, 0.15), swing_grid=16, humanize=(0.1, 0.25),
        bars=(4, 8), tail=(1,), density=(0.3, 0.5), registers=("mid", "high"),
        forms=(STANDARD, CLUB,),
        words=(("Greasy", "Strut", "Hot", "Uptown", "Tight", "Brass", "Saturday", "Electric"),
               ("Strut", "Pocket", "Shuffle", "Bounce", "Groove", "Parade", "Hustle", "Swagger")),
    ),
    "cinematic": GenreRules(
        scales=("minor", "harmonic_minor", "dorian", "lydian"),
        progressions=(
            [(1, T, 8), (6, T, 8), (3, T, 8), (7, T, 8)],
            [(6, T, 4), (4, T, 4), (1, T, 4), (5, T, 4)],
            [(1, SUS2, 8), (4, T, 8)],
            [(1, T, 4), (7, T, 4), (6, T, 4), (5, SUS4, 4)],
        ),
        drums=(
            {"tom_low": "X.......X.......", "tom_high": "......x.......x.", "crash": "X..............."},
            {"tom_low": "X..X....X..X....", "tom_high": "..............xx"},
        ),
        bass_styles=("drone", "root"),
        bass_grids=("X...............", "X.......X......."),
        keys_styles=("arpeggio", "broken", "sustain"),
        pad=(48, 49, 50, 52), lead=(40, 60, 68, 73, 71), bass=(43, 32), keys=(46, 0, 48),
        kits=(48,), swing=(0.0, 0.0), swing_grid=16, humanize=(0.2, 0.35),
        bars=(8, 16), tail=(3, 4), density=(0.2, 0.4), registers=("mid", "high"),
        fill_chance=0.2, bass_octave=(1, 2),
        forms=(FLOWING, STANDARD,),
        words=(("Distant", "Iron", "Fallen", "Northern", "Silent", "Burning", "Last", "Golden"),
               ("Kingdom", "Horizon", "Voyage", "Citadel", "Frontier", "Ascent", "Empire", "Dawn")),
    ),
    "disco": GenreRules(
        scales=("minor", "dorian", "major"),
        progressions=(
            [(1, S7, 4), (4, S7, 4)],
            [(2, S7, 4), (5, S7, 4), (1, S7, 8)],
            [(6, S7, 4), (2, S7, 4), (5, S7, 4), (1, S9, 4)],
            [(1, S9, 8), (6, S7, 8)],
        ),
        drums=(
            {"kick": "X...X...X...X...", "clap": "....x.......x...", "open_hat": "..x...x...x...x.", "closed_hat": "o.o.o.o.o.o.o.o."},
            {"kick": "X...X...X...X...", "snare": "....x.......x...", "open_hat": "..x...x...x...x.", "shaker": "oxoxoxoxoxoxoxox"},
        ),
        bass_styles=("octave",),
        bass_grids=("x.x.x.x.x.x.x.x.", "X.xxX.xxX.xxX.xx"),
        keys_styles=("offbeat", "stabs"),
        pad=(48, 49), lead=(81, 61, 56, 65), bass=(33, 38), keys=(48, 61, 4),
        kits=(0, 8, 25), swing=(0.0, 0.05), swing_grid=16, humanize=(0.05, 0.15),
        bars=(4, 8), tail=(1, 2), density=(0.25, 0.45), registers=("high", "mid"),
        forms=(CLUB, STANDARD,),
        words=(("Mirror", "Glitter", "Velvet", "Starlight", "Roller", "Satin", "Silver", "Saturday"),
               ("Ballroom", "Boogie", "Fever", "Lights", "Rink", "Strut", "Heat", "Nights")),
    ),
    "city-pop": GenreRules(
        scales=("major", "lydian", "dorian"),
        progressions=(
            [(4, S7, 4), (5, S7, 4), (3, S7, 4), (6, S7, 4)],
            [(2, S9, 4), (5, S7, 4), (1, S7, 4), (6, S7, 4)],
            [(4, S9, 8), (5, S7, 4), (3, S7, 4)],
            [(1, S7, 4), (3, S7, 4), (4, S7, 4), (5, SUS4, 4)],
        ),
        drums=(
            {"kick": "X......X..X.....", "snare": "....X.......X...", "closed_hat": "x.x.x.x.x.x.x.x."},
            {"kick": "X.....X...X.....", "snare": "....X.......X...", "closed_hat": "xxxxxxxxxxxxxxxx", "open_hat": "......x........."},
        ),
        bass_styles=("root_fifth", "octave"),
        bass_grids=("X..x..X...x.x...", "X.x...x.X.x...x."),
        keys_styles=("comp", "stabs", "sustain"),
        pad=(89, 48, None), lead=(65, 81, 11, 56), bass=(36, 33), keys=(4, 5, 0),
        kits=(0, 8), swing=(0.05, 0.12), swing_grid=16, humanize=(0.1, 0.25),
        bars=(4, 8), tail=(1, 2), density=(0.3, 0.5), registers=("mid", "high"),
        forms=(SONG, STANDARD,),
        words=(("Plastic", "Midnight", "Summer", "Seaside", "Neon", "Pastel", "Weekend", "Highway"),
               ("Drive", "Romance", "Marina", "Boulevard", "Sunset", "Station", "Resort", "Cruise")),
    ),
    "blues": GenreRules(
        scales=("mixolydian", "dorian", "minor"),
        progressions=(
            [(1, S7, 16), (4, S7, 8), (1, S7, 8), (5, S7, 4), (4, S7, 4), (1, S7, 4), (5, S7, 4)],
            [(1, S7, 4), (4, S7, 4), (1, S7, 8), (4, S7, 8), (1, S7, 8), (5, S7, 4), (4, S7, 4), (1, S7, 4), (5, S7, 4)],
        ),
        drums=(
            {"ride": "x...x.x.x...x.x.", "closed_hat": "....x.......x...", "kick": "X.......X.......", "snare": "....x.......x..."},
            {"closed_hat": "x.xxx.xxx.xxx.xx", "kick": "X.......X.......", "snare": "....X.......X..."},
        ),
        bass_styles=("walking", "root_fifth"),
        bass_grids=("x...x...x...x...", "X..xX..xX..xX..x"),
        keys_styles=("comp", "stabs"),
        pad=(None,), lead=(22, 26, 24, 65, 66), bass=(32, 33), keys=(16, 0, 4),
        kits=(32, 0, 40), swing=(0.3, 0.4), swing_grid=8, humanize=(0.2, 0.35),
        bars=(12,), tail=(1, 2), density=(0.3, 0.5), registers=("mid",),
        fill_chance=0.3,
        forms=(SONG, STANDARD,),
        words=(("Muddy", "Crossroad", "Midnight", "Lonesome", "Dusty", "Smoky", "Rusty", "Southern"),
               ("Shuffle", "Station", "Porch", "Highway", "Juke", "Levee", "Railway", "Rambler")),
    ),
    "punjabi-beats": GenreRules(
        # Punjabi rap / hip-hop: trap-style drums and long 808s under a dark minor loop,
        # with a tumbi-like pluck, sitar or flute carrying the melody.
        scales=("minor", "phrygian", "harmonic_minor", "dorian"),
        progressions=(
            [(1, T, 8), (6, T, 8)],
            [(1, T, 4), (7, T, 4), (6, T, 4), (5, T, 4)],
            [(1, T, 8), (4, T, 8)],
            [(6, T, 4), (5, T, 4), (1, T, 8)],
        ),
        drums=(
            {"kick": "X......X..X.....", "clap": "....X.......X...", "closed_hat": "xxxxxxXxxxxxxXxx", "open_hat": "..............x."},
            {"kick": "X..X......X..X..", "snare": "....X.......X...", "closed_hat": "x.x.xxx.x.x.xxxx", "rim": "...........o...."},
            {"kick": "X.....X...X.....", "clap": "....X.......X..o", "closed_hat": "xxxXxxxxxxxXxxxx", "open_hat": "......x........."},
        ),
        bass_styles=("root",),
        bass_grids=("X......x..X.....", "X..x......X..x..", "X.....x...X....."),
        keys_styles=("sustain", "broken", "stabs"),
        pad=(48, 49, None), lead=(106, 104, 73, 81), bass=(38, 39), keys=(0, 48, 4),
        kits=(25, 24, 0), swing=(0.0, 0.08), swing_grid=16, humanize=(0.05, 0.15),
        bars=(4, 8), tail=(1, 2), density=(0.3, 0.5), registers=("mid", "high"),
        bass_octave=(1,), fill_chance=0.3,
        forms=(STANDARD, SONG,),
        words=(("Midnight", "Golden", "Royal", "Concrete", "Desert", "Smoke", "Street", "Velvet"),
               ("Lane", "Throne", "Highway", "Skyline", "Convoy", "Crown", "Hustle", "Signal")),
    ),
}

assert set(RULES) == set(GENRES)

# Mood words (data only) nudge choices inside the genre rules.
_MOODS = {
    "calm": ("calm", "chill", "relax", "sleep", "soft", "gentle", "peace", "slow", "study", "focus", "mellow"),
    "dark": ("dark", "sad", "moody", "night", "melanch", "lonely", "rain", "grim", "tense", "eerie"),
    "bright": ("happy", "bright", "sunny", "upbeat", "joy", "cheer", "summer", "hope", "warm", "morning"),
    "energy": ("energ", "fast", "hype", "dance", "party", "intense", "drive", "workout", "run", "power"),
}
_BRIGHT_SCALES = ("major", "lydian", "mixolydian")
_DARK_SCALES = ("minor", "phrygian", "harmonic_minor", "dorian")


# Prompt words -> radio (genre). Checked in order; first match wins.
_GENRE_WORDS: list[tuple[str, tuple[str, ...]]] = [
    ("punjabi-beats", ("punjabi", "punjab", "desi rap", "desi hip hop", "desi hip-hop", "tumbi", "desi")),
    ("drum-and-bass", ("drum and bass", "drum & bass", "drum n bass", "dnb", "d&b", "jungle", "liquid")),
    ("city-pop", ("city pop", "city-pop", "citypop", "tokyo", "japanese 80s", "80s japan")),
    ("chiptune", ("chiptune", "8-bit", "8 bit", "8bit", "retro game", "video game", "arcade", "pixel", "nes ", "gameboy")),
    ("synthwave", ("synthwave", "retrowave", "outrun", "80s", "eighties", "neon", "night drive", "vaporwave")),
    ("techno", ("techno", "warehouse", "rave", "industrial", "minimal techno")),
    ("disco", ("disco", "boogie", "70s", "seventies", "mirror ball")),
    ("funk", ("funk", "slap bass", "clavinet", "groove")),
    ("reggae", ("reggae", "dub", "ska", "island", "one drop")),
    ("bossa-nova", ("bossa", "brazil", "samba", "rio", "copacabana")),
    ("blues", ("blues", "bluesy", "delta", "harmonica", "12-bar", "twelve bar")),
    ("hip-hop", ("hip hop", "hip-hop", "hiphop", "boom bap", "boombap", "rap beat", "rap instrumental")),
    ("cinematic", ("cinematic", "epic", "film score", "soundtrack", "trailer", "orchestral", "orchestra")),
    ("house", ("house", "club", "dance", "deep house", "funky")),
    ("trip-hop", ("trip-hop", "trip hop", "triphop", "downtempo", "noir", "smoky beats", "dark beats")),
    ("jazz", ("jazz", "bebop", "swing", "sax", "lounge", "cocktail", "speakeasy")),
    ("classical-piano", ("classical", "piano", "sonata", "nocturne", "etude", "baroque", "chopin-like")),
    ("ambient", ("ambient", "drone", "meditat", "sleep", "space", "atmospher", "calm ocean", "spa")),
    ("lofi", ("lofi", "lo-fi", "lo fi", "study", "chillhop", "beats to", "homework", "coffee")),
]

# Prompt words -> instrument overrides (role, GM program). Applied only if the role is in use.
_INSTRUMENT_WORDS: list[tuple[tuple[str, ...], str, int]] = [
    (("tenor sax",), "lead", 66), (("sax", "saxophone"), "lead", 65), (("trumpet", "horn"), "lead", 56),
    (("flute",), "lead", 73), (("clarinet",), "lead", 71), (("vibraphone", "vibes"), "lead", 11),
    (("marimba",), "lead", 12), (("bells", "glockenspiel"), "lead", 9), (("guitar",), "lead", 24),
    (("piano",), "keys", 0), (("rhodes", "electric piano"), "keys", 4), (("organ",), "keys", 16),
    (("harp",), "keys", 46), (("strings", "orchestra", "cinematic"), "pad", 48), (("choir", "voices"), "pad", 52),
    (("synth", "analog"), "lead", 81),
]


def genre_from_prompt(prompt: str | None, default: str | None = None) -> str:
    """Pick a radio for free-text prompt words; falls back to `default` or a random genre."""
    text = f" {(prompt or '').lower()} "
    for genre, words in _GENRE_WORDS:
        if any(w in text for w in words):
            return genre
    return default or random.choice(GENRES)


def instrument_hints(prompt: str | None) -> dict[str, int]:
    text = (prompt or "").lower()
    hints: dict[str, int] = {}
    for words, role, prog in _INSTRUMENT_WORDS:
        if role not in hints and any(w in text for w in words):
            hints[role] = prog
    return hints


def mood_tags(mood: str | None) -> set[str]:
    text = (mood or "").lower()[:200]
    return {tag for tag, words in _MOODS.items() if any(w in text for w in words)}


def new_seed() -> int:
    return random.SystemRandom().randrange(2**31)


def generate(genre: str, mood: str | None = None, seed: int | None = None,
             brief: "SongBrief | None" = None) -> SongSpec:
    """Build a SongSpec. A brief (from an AI) overrides choices after the mood hints; still deterministic."""
    if genre not in RULES:
        raise ValueError(f"unknown genre {genre!r}")
    if seed is None:
        seed = brief.seed if brief is not None else new_seed()
    r = RULES[genre]
    rng = random.Random(f"{genre}:{seed}")
    tags = mood_tags(mood)

    lo, hi = GENRE_TEMPO[genre]
    t = rng.random()
    if "calm" in tags:
        t *= 0.4
    if "energy" in tags:
        t = 0.6 + 0.4 * t
    tempo = round(lo + (hi - lo) * t)
    if brief is not None:
        if brief.tempo is not None:
            tempo = round(min(max(brief.tempo, lo), hi))
        elif brief.energy is not None:
            tempo = round(lo + (hi - lo) * brief.energy)

    scales = list(r.scales)
    if "bright" in tags:
        scales = [s for s in scales if s in _BRIGHT_SCALES] or scales
    elif "dark" in tags:
        scales = [s for s in scales if s in _DARK_SCALES] or scales

    density = rng.uniform(*r.density)
    if "calm" in tags:
        density *= 0.7
    if "energy" in tags:
        density = min(1.0, density * 1.3)
    tail = rng.choice(r.tail)
    if "calm" in tags:
        tail = min(4, tail + 1)

    main_prog = rng.choice(r.progressions)
    others = [p for p in r.progressions if p != main_prog]
    alt_prog = rng.choice(others) if others else None
    form = rng.choice(r.forms)
    bars = rng.choice(r.bars)
    while bars > 4 and len(form) * bars * 4 * 60.0 / tempo > SONG_SECONDS[1]:
        bars //= 2  # phrases too long for every section to appear once: shorten them
    drums = rng.choice(r.drums) if r.drums else {}
    kit = rng.choice(r.kits) if drums else None
    adj, noun = r.words
    spec = {
        "genre": genre,
        "name": f"{rng.choice(adj)} {rng.choice(noun)}",
        "tempo": tempo,
        "key": rng.choice(KEYS),
        "scale": rng.choice(scales),
        "bars": bars,
        "chord_progression": [{"degree": d, "ext": e, "beats": b} for d, e, b in main_prog],
        "alt_progression": [{"degree": d, "ext": e, "beats": b} for d, e, b in alt_prog] if alt_prog else None,
        "structure": [{"kind": k, "repeats": w} for k, w in form],
        "drum_pattern": {piece: grid(g) for piece, g in drums.items()},
        "drum_fill": bool(drums) and rng.random() < r.fill_chance,
        "bass_pattern": {
            "style": rng.choice(r.bass_styles),
            "steps": grid(rng.choice(r.bass_grids)),
            "octave": rng.choice(r.bass_octave),
        },
        "keys_style": rng.choice(r.keys_styles),
        "melody": {
            "enabled": True,
            "density": round(density, 3),
            "register": rng.choice(r.registers),
            "rhythm_seed": rng.randrange(2**31),
            "motif_bars": rng.choice((1, 2)),
        },
        "instruments": {
            "pad": rng.choice(r.pad),
            "lead": rng.choice(r.lead),
            "bass": rng.choice(r.bass),
            "keys": rng.choice(r.keys),
            "drum_kit": kit,
        },
        "swing": round(rng.uniform(*r.swing), 3),
        "swing_grid": r.swing_grid,
        "humanize": round(rng.uniform(*r.humanize), 3),
        "reverb_tail_bars": tail,
        "seed": seed,
    }
    for role, prog in instrument_hints(mood).items():
        # Lead and keys are switched on if the genre left them out; a pad is only swapped, never added.
        if spec["instruments"][role] is not None or role in ("lead", "keys"):
            spec["instruments"][role] = prog
    if brief is not None:
        if brief.name:
            spec["name"] = brief.name
        if brief.key:
            spec["key"] = brief.key
        if brief.scale:
            spec["scale"] = brief.scale
        if brief.density is not None:
            spec["melody"]["density"] = round(brief.density, 3)
        if brief.instruments:
            for role in ("pad", "lead", "bass", "keys"):
                prog = getattr(brief.instruments, role)
                if prog is not None:
                    spec["instruments"][role] = prog
    # Validate with a provisional long-enough structure, then size it to the target length.
    phrase_s = spec["bars"] * 4 * 60.0 / tempo
    spec["structure"] = scale_structure(spec["structure"], phrase_s, rng.uniform(*SONG_SECONDS))
    return SongSpec.model_validate(spec)


def scale_structure(structure: list[dict], phrase_seconds: float, target: float,
                    lo: float = SONG_SECONDS[0], hi: float = SONG_SECONDS[1]) -> list[dict]:
    """Scale section repeats (keeping their proportions) so the song lasts about `target` seconds."""
    weights = [max(1, int(sec.get("repeats", 1))) for sec in structure]
    k = target / (sum(weights) * phrase_seconds)
    reps = [min(16, max(1, round(w * k))) for w in weights]
    # Nudge into [lo, hi]: grow/shrink the biggest body sections first, never intro/outro below 1.
    order = sorted(range(len(reps)), key=lambda i: -weights[i])
    for _ in range(64):
        total = sum(reps) * phrase_seconds
        if total < lo:
            i = next((i for i in order if reps[i] < 16), None)
            if i is None:
                break
            reps[i] += 1
        elif total > hi:
            i = next((i for i in order if reps[i] > 1), None)
            if i is None:
                break
            reps[i] -= 1
        else:
            break
        order = order[1:] + order[:1]  # spread changes over sections
    return [{**sec, "repeats": n} for sec, n in zip(structure, reps)]
