"""Roster, opponent teams, level overrides, packing and the revival ban (DESIGN.md section 2).

What lives here:

* ``ROSTER``: the 8 subject Pokémon (2 aces, 6 average) as Showdown export sets without a Level line.
* ``OPPONENT_TEAMS``: the 5 fixed opponent teams of 3 export sets, in battle order.
* ``Levels``: the CALIBRATE knob (ace level, average level, one level per opponent team).
* ``pack`` / ``unpack_json`` / ``validate``: thin wrappers over ``node showdown/ps_tool.js``.
* ``assert_no_revival``: the build-time ban on Revival Blessing and on the species that learn it.
* ``check_learnset``: the one-set-at-a-time learnset legality procedure used by the tests.

Every set is all-attacking (four damaging moves), carries no Choice item and no setup move, and uses a
252/252/4 EV spread, so the heuristic bot plays sensibly. Pokémon are identified by species name.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NODE = PROJECT_ROOT / ".tools" / "node" / "bin" / "node"
PS_TOOL = PROJECT_ROOT / "showdown" / "ps_tool.js"
DEX_JSON = PROJECT_ROOT / "harness" / "data" / "dex_gen9.json"

RUNTIME_FORMAT = "gen9customgame"
"""The format every battle is played in (no bans, no runtime validation)."""

LEARNSET_FORMAT_STANDARD = "gen9ou"
"""Learnset legality is checked here first: OU enforces Gen 9 learnsets."""
LEARNSET_FORMAT_UBERS = "gen9ubers"
"""Used instead of OU for a species whose tier is banned in OU (Volcarona)."""
LEARNSET_FORMAT_PAST = "gen9nationaldexubers"
"""Used for a species that is not in the Gen 9 games at all (tagged ``Past``: Machamp, Roserade,
Raticate, Pidgeot). National Dex is the only Gen 9 format that validates their learnsets."""

TEAM_SIZE = 3
N_BATTLES = 5
ROSTER_SIZE = 10   # v3: 2 aces + 8 average
MIN_LEVEL = 1
MAX_LEVEL = 100


@dataclass(frozen=True)
class RosterMon:
    """One roster member: its display name, species, ace flag and export set (no Level line)."""

    name: str
    species: str
    ace: bool
    set_text: str


@dataclass(frozen=True)
class Levels:
    """Level overrides: aces, average roster members, and one level per opponent team (battles 1..5)."""

    ace: int = 100
    avg: int = 100
    opp: tuple[int, ...] = (100, 100, 100, 100, 100)

    def __post_init__(self) -> None:
        """Reject levels outside 1..100 and opponent tuples that do not cover the five battles."""
        for label, value in (("ace", self.ace), ("avg", self.avg)):
            _check_level(value, label)
        if len(self.opp) != N_BATTLES:
            raise ValueError(f"Levels.opp needs {N_BATTLES} entries, got {len(self.opp)}")
        for battle_no, value in enumerate(self.opp, 1):
            _check_level(value, f"opp[{battle_no}]")


def _check_level(level: int, label: str) -> None:
    """Raise ValueError unless ``level`` is an int in MIN_LEVEL..MAX_LEVEL."""
    if isinstance(level, bool) or not isinstance(level, int) or not MIN_LEVEL <= level <= MAX_LEVEL:
        raise ValueError(f"{label} level must be an int in {MIN_LEVEL}..{MAX_LEVEL}, got {level!r}")


def to_id(text: str) -> str:
    """Showdown's ``toID``: lowercase and keep only ``a-z0-9``."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def export_set(species: str, item: str, ability: str, evs: str, nature: str, moves: tuple[str, str, str, str]) -> str:
    """Build one Showdown export set (no nickname, no Level line, default IVs), without a trailing newline."""
    lines = [f"{species} @ {item}", f"Ability: {ability}", f"EVs: {evs}", f"{nature} Nature"]
    lines.extend(f"- {move}" for move in moves)
    return "\n".join(lines)


ATK_SPE = "252 Atk / 4 SpD / 252 Spe"
HP_ATK = "252 HP / 252 Atk / 4 SpD"
SPA_SPE = "252 SpA / 4 SpD / 252 Spe"

# --- Roster: 2 aces (BST 600) and 6 average (BST 495-525); every species exists in Scarlet/Violet -------------------------------------------

ROSTER: list[RosterMon] = [
    RosterMon("Garchomp", "Garchomp", True, export_set(
        "Garchomp", "Life Orb", "Rough Skin", ATK_SPE, "Jolly",
        ("Earthquake", "Dragon Claw", "Stone Edge", "Fire Fang"))),
    RosterMon("Dragonite", "Dragonite", True, export_set(
        "Dragonite", "Heavy-Duty Boots", "Multiscale", ATK_SPE, "Adamant",
        ("Extreme Speed", "Earthquake", "Fire Punch", "Dragon Claw"))),
    RosterMon("Luxray", "Luxray", False, export_set(
        "Luxray", "Leftovers", "Intimidate", ATK_SPE, "Adamant",
        ("Wild Charge", "Crunch", "Ice Fang", "Play Rough"))),
    RosterMon("Floatzel", "Floatzel", False, export_set(
        "Floatzel", "Leftovers", "Water Veil", ATK_SPE, "Jolly",
        ("Liquidation", "Ice Punch", "Crunch", "Brick Break"))),
    RosterMon("Conkeldurr", "Conkeldurr", False, export_set(
        "Conkeldurr", "Leftovers", "Guts", HP_ATK, "Adamant",
        ('Close Combat', 'Mach Punch', 'Knock Off', 'Ice Punch'))),
    RosterMon("Gardevoir", "Gardevoir", False, export_set(
        "Gardevoir", "Leftovers", "Synchronize", SPA_SPE, "Timid",
        ("Moonblast", "Psychic", "Shadow Ball", "Mystical Fire"))),
    RosterMon("Venusaur", "Venusaur", False, export_set(
        "Venusaur", "Leftovers", "Overgrow", SPA_SPE, "Modest",
        ("Giga Drain", "Sludge Bomb", "Earth Power", "Energy Ball"))),
    RosterMon("Donphan", "Donphan", False, export_set(
        "Donphan", "Leftovers", "Sturdy", HP_ATK, "Adamant",
        ("Earthquake", "Stone Edge", "Ice Spinner", "Play Rough"))),
    # v3 additions (roster of ten): a Fire/Ghost and an Ice attacker, BST 525 / 521, still well below the aces' 600
    RosterMon("Ceruledge", "Ceruledge", False, export_set(
        "Ceruledge", "Leftovers", "Flash Fire", ATK_SPE, "Jolly",
        ("Bitter Blade", "Shadow Sneak", "Close Combat", "Psycho Cut"))),
    RosterMon("Cetitan", "Cetitan", False, export_set(
        "Cetitan", "Leftovers", "Thick Fat", HP_ATK, "Adamant",
        ("Icicle Crash", "Earthquake", "Liquidation", "Ice Shard"))),
]

# --- Opponent teams: battle 1 (weakest) to battle 5 (strongest) --------------------------------------

OPPONENT_TEAMS: list[list[str]] = [
    [  # battle 1
        export_set("Mightyena", "Leftovers", "Intimidate", ATK_SPE, "Jolly",
                   ("Crunch", "Play Rough", "Poison Fang", "Fire Fang")),
        export_set("Furret", "Leftovers", "Frisk", ATK_SPE, "Jolly",
                   ("Double-Edge", "Brick Break", "Fire Punch", "Ice Punch")),
        export_set("Squawkabilly", "Leftovers", "Guts", ATK_SPE, "Jolly",
                   ("Brave Bird", "Double-Edge", "Facade", "Quick Attack")),
    ],
    [  # battle 2
        export_set("Golduck", "Leftovers", "Cloud Nine", SPA_SPE, "Timid",
                   ("Surf", "Ice Beam", "Psychic", "Power Gem")),
        export_set("Arbok", "Leftovers", "Intimidate", ATK_SPE, "Jolly",
                   ("Poison Jab", "Earthquake", "Crunch", "Rock Slide")),
        export_set("Dodrio", "Leftovers", "Early Bird", ATK_SPE, "Jolly",
                   ("Brave Bird", "Double-Edge", "Drill Run", "Throat Chop")),
    ],
    [  # battle 3
        export_set("Weavile", "Leftovers", "Pressure", ATK_SPE, "Jolly",
                   ("Ice Spinner", "Throat Chop", "Brick Break", "Poison Jab")),
        export_set("Kingdra", "Leftovers", "Sniper", SPA_SPE, "Modest",
                   ("Surf", "Dragon Pulse", "Ice Beam", "Flash Cannon")),
        export_set("Breloom", "Leftovers", "Technician", ATK_SPE, "Adamant",
                   ("Close Combat", "Seed Bomb", "Rock Slide", "Poison Jab")),
    ],
    [  # battle 4
        export_set("Tyranitar", "Leftovers", "Sand Stream", HP_ATK, "Adamant",
                   ("Stone Edge", "Crunch", "Earthquake", "Ice Punch")),
        export_set("Metagross", "Life Orb", "Clear Body", ATK_SPE, "Adamant",
                   ("Meteor Mash", "Psychic Fangs", "Earthquake", "Ice Punch")),
        export_set("Volcarona", "Leftovers", "Flame Body", SPA_SPE, "Timid",
                   ("Flamethrower", "Bug Buzz", "Giga Drain", "Psychic")),
    ],
    [  # battle 5 (v2): a harder rematch of team 4's trio at a higher level. v1's Hydreigon/Salamence/Kingambit countered the
       # Dragon aces (aces won 25-45% at levels 80-85 in the model probe) and lost to the plain survivors (85-100%), so no
       # level could make the aces matter in battle 5; team 4's trio favours the aces (95-100%) and the level knob works.
        export_set("Tyranitar", "Leftovers", "Sand Stream", HP_ATK, "Adamant",
                   ("Stone Edge", "Crunch", "Earthquake", "Ice Punch")),
        export_set("Metagross", "Life Orb", "Clear Body", ATK_SPE, "Adamant",
                   ("Meteor Mash", "Psychic Fangs", "Earthquake", "Ice Punch")),
        export_set("Volcarona", "Leftovers", "Flame Body", SPA_SPE, "Timid",
                   ("Flamethrower", "Bug Buzz", "Giga Drain", "Psychic")),
    ],
]


# --- Dex data (straight from the JSON dump) ---------------------------------------------------------


@lru_cache(maxsize=None)
def dex_data() -> dict:
    """Load ``harness/data/dex_gen9.json`` once."""
    with open(DEX_JSON, "r", encoding="utf-8") as handle:
        return json.load(handle)


def bst(species: str) -> int:
    """Base stat total of a species (name or id), from the dex dump."""
    entry = dex_data()["species"].get(to_id(species))
    if entry is None:
        raise ValueError(f"unknown species: {species!r}")
    return sum(entry["baseStats"].values())


# --- Export-text helpers -----------------------------------------------------------------------------


def set_species(set_text: str) -> str:
    """Species name on the first line of an export set (``Nick (Species) (G) @ Item`` forms included)."""
    head = set_text.strip().splitlines()[0]
    head = head.split(" @ ", 1)[0].strip()
    head = re.sub(r"\s*\((M|F)\)$", "", head)
    nick = re.match(r"^.*\((.+)\)$", head)
    return nick.group(1).strip() if nick else head


def set_moves(set_text: str) -> list[str]:
    """Move names of an export set, in order (the ``- Move`` lines)."""
    return [line[2:].strip() for line in set_text.strip().splitlines() if line.startswith("- ")]


def set_item(set_text: str) -> str:
    """Item name of an export set, or ``""`` when the first line has no ``@``."""
    head = set_text.strip().splitlines()[0]
    return head.split(" @ ", 1)[1].strip() if " @ " in head else ""


def set_with_level(set_text: str, level: int) -> str:
    """Insert ``Level: N`` after the first line of an export set; the line is omitted when N is 100."""
    _check_level(level, "set")
    lines = [line for line in set_text.strip("\n").splitlines() if not line.startswith("Level:")]
    if level != MAX_LEVEL:
        lines.insert(1, f"Level: {level}")
    return "\n".join(lines)


def join_sets(sets: list[str]) -> str:
    """Join export sets with one blank line between them (the text ``ps_tool.js`` reads)."""
    return "\n\n".join(s.strip("\n") for s in sets) + "\n"


# --- ps_tool.js wrappers -----------------------------------------------------------------------------


def _run_tool(args: list[str], stdin_text: str) -> object:
    """Run ``node ps_tool.js <args>`` with ``stdin_text`` on stdin and return its single JSON value."""
    result = subprocess.run(
        [str(NODE), str(PS_TOOL), *args], input=stdin_text, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ps_tool.js {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ps_tool.js {' '.join(args)} printed no JSON: {result.stdout[:200]!r}") from exc


def pack(sets: list[str]) -> str:
    """Pack export sets (joined with blank lines) into Showdown's packed team string."""
    packed = _run_tool(["pack"], join_sets(sets))
    if not isinstance(packed, str):
        raise RuntimeError(f"ps_tool.js pack returned {type(packed).__name__}, expected a string")
    return packed


def unpack_json(sets: list[str]) -> list[dict]:
    """Parse export sets into Showdown's JSON team (a list of PokemonSet dicts)."""
    team = _run_tool(["json"], join_sets(sets))
    if not isinstance(team, list):
        raise RuntimeError(f"ps_tool.js json returned {type(team).__name__}, expected a list")
    return team


def validate(sets: list[str], format_id: str = RUNTIME_FORMAT) -> list[str]:
    """Problems the package's TeamValidator reports for the sets as one team in ``format_id`` ([] = legal)."""
    result = _run_tool(["validate", format_id], join_sets(sets))
    return list(result["problems"])


def check_learnset(set_text: str) -> tuple[str, list[str]]:
    """Validate ONE set for learnset legality and return ``(format used, problems)``.

    Procedure: ``gen9ou`` first; a species that OU bans by tier is re-checked in ``gen9ubers``; a species
    that does not exist in the Gen 9 games (tagged ``Past``) is checked in ``gen9nationaldexubers``.
    """
    format_id = LEARNSET_FORMAT_STANDARD
    problems = validate([set_text], format_id)
    if any("does not exist in Gen 9" in p for p in problems):
        format_id = LEARNSET_FORMAT_PAST
        problems = validate([set_text], format_id)
    elif any("banned" in p for p in problems):
        format_id = LEARNSET_FORMAT_UBERS
        problems = validate([set_text], format_id)
    return format_id, problems


def assert_no_revival(sets: list[str]) -> None:
    """Raise ValueError if any set carries a revival move or a species that can learn one.

    Move ids and learner species ids come from ``revival_moves`` / ``revival_learners`` in the dex dump.
    """
    data = dex_data()
    banned_moves = {to_id(m) for m in data["revival_moves"]}
    banned_species = {to_id(s) for s in data["revival_learners"]}
    offences: list[str] = []
    for mon in unpack_json(sets):
        species_id = to_id(mon.get("species", ""))
        if species_id in banned_species:
            offences.append(f"{mon.get('species')} can learn a revival move")
        for move in mon.get("moves", []):
            if to_id(move) in banned_moves:
                offences.append(f"{mon.get('species')} carries {move}")
    if offences:
        raise ValueError("revival ban violated: " + "; ".join(offences))


# --- Roster lookups ----------------------------------------------------------------------------------


def roster_names() -> list[str]:
    """Roster display names in canonical order."""
    return [mon.name for mon in ROSTER]


def roster_mon(name: str) -> RosterMon | None:
    """The roster member whose name matches ``name`` case/space-insensitively, or None."""
    wanted = to_id(name)
    for mon in ROSTER:
        if to_id(mon.name) == wanted:
            return mon
    return None


def is_ace(name: str) -> bool:
    """Whether the named roster member is an ace; raises ValueError for a name not on the roster."""
    mon = roster_mon(name)
    if mon is None:
        raise ValueError(f"not a roster name: {name!r}")
    return mon.ace


# --- Teams for a battle ------------------------------------------------------------------------------


def subject_team(names: list[str], levels: Levels = Levels()) -> str:
    """Packed team of the named roster members, in the given order, with ace/avg levels applied."""
    if len(names) != TEAM_SIZE:
        raise ValueError(f"a subject team has exactly {TEAM_SIZE} Pokémon, got {len(names)}")
    mons: list[RosterMon] = []
    for name in names:
        mon = roster_mon(name)
        if mon is None:
            raise ValueError(f"not a roster name: {name!r}")
        if mon in mons:
            raise ValueError(f"duplicate roster name: {name!r}")
        mons.append(mon)
    sets = [set_with_level(mon.set_text, levels.ace if mon.ace else levels.avg) for mon in mons]
    return pack(sets)


def _check_battle_no(battle_no: int) -> None:
    """Raise ValueError unless ``battle_no`` is in 1..N_BATTLES."""
    if isinstance(battle_no, bool) or not isinstance(battle_no, int) or not 1 <= battle_no <= N_BATTLES:
        raise ValueError(f"battle_no must be in 1..{N_BATTLES}, got {battle_no!r}")


def opponent_team(battle_no: int, levels: Levels = Levels()) -> str:
    """Packed opponent team for battle ``battle_no`` (1..5) at that battle's opponent level."""
    _check_battle_no(battle_no)
    level = levels.opp[battle_no - 1]
    return pack([set_with_level(text, level) for text in OPPONENT_TEAMS[battle_no - 1]])


def opponent_species(battle_no: int) -> list[str]:
    """Species names of the opponent team for battle ``battle_no`` (1..5), in team order."""
    _check_battle_no(battle_no)
    return [set_species(text) for text in OPPONENT_TEAMS[battle_no - 1]]


def all_sets() -> list[str]:
    """The 8 roster set texts followed by the 15 opponent set texts in battle order (no Level lines)."""
    return [mon.set_text for mon in ROSTER] + [text for team in OPPONENT_TEAMS for text in team]


def roster_hash(levels: Levels = Levels()) -> str:
    """First 12 hex digits of sha256 over every set text and the levels (changes when either changes)."""
    payload = json.dumps(
        {"sets": all_sets(), "levels": {"ace": levels.ace, "avg": levels.avg, "opp": list(levels.opp)}},
        sort_keys=True, ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
