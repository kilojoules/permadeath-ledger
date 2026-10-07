"""Gen 9 data for the harness: species, moves, the type chart, stat estimates and a damage heuristic.

Everything is read from ``harness/data/dex_gen9.json``, dumped from the pinned pokemon-showdown 0.11.11 by
``showdown/ps_tool.js dex``.  Lookups accept display names ("Dragon Claw") or Showdown ids ("dragonclaw").
Nothing here is random.
"""
from __future__ import annotations

import copy
import json
import os
import re

#: Path of the bundled Gen 9 data dump (relative to this package directory).
DEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "dex_gen9.json")

_NOT_ID_CHARS = re.compile(r"[^a-z0-9]")

#: Showdown's ``damageTaken`` encoding: 0 neutral, 1 super effective, 2 resisted, 3 immune.
DAMAGE_TAKEN_MULTIPLIER: dict[int, float] = {0: 1.0, 1: 2.0, 2: 0.5, 3: 0.0}

#: Damaging moves with base power 0 whose damage is fixed or level-based rather than power-based.
FIXED_DAMAGE_MOVES: frozenset[str] = frozenset({
    "seismictoss", "nightshade", "superfang", "naturesmadness", "ruination", "endeavor", "finalgambit",
    "dragonrage", "sonicboom", "psywave", "counter", "mirrorcoat", "metalburst", "comeuppance",
})

#: Score of a fixed-damage or one-hit-KO move: usable, but below any ordinary attack.
FIXED_DAMAGE_SCORE = 10.0

#: Showdown rolls 2/3/4/5 hits with 35/35/15/15 percent for ``multihit: [2, 5]`` moves.
TWO_TO_FIVE_AVERAGE_HITS = 3.1

#: Defender ability id -> {attacking type id: multiplier} for the immunities and resistances modelled.
ABILITY_TYPE_MULTIPLIERS: dict[str, dict[str, float]] = {
    "levitate": {"ground": 0.0},
    "eartheater": {"ground": 0.0},
    "flashfire": {"fire": 0.0},
    "wellbakedbody": {"fire": 0.0},
    "waterabsorb": {"water": 0.0},
    "stormdrain": {"water": 0.0},
    "dryskin": {"water": 0.0},
    "voltabsorb": {"electric": 0.0},
    "lightningrod": {"electric": 0.0},
    "motordrive": {"electric": 0.0},
    "sapsipper": {"grass": 0.0},
    "thickfat": {"fire": 0.5, "ice": 0.5},
}

_STAT_KEYS = ("hp", "atk", "def", "spa", "spd", "spe")


def to_id(s: str) -> str:
    """Showdown id of a name: lowercase with everything outside [a-z0-9] removed."""
    return _NOT_ID_CHARS.sub("", str(s).lower())


class Dex:
    """Read-only access to the Gen 9 data dump.  Returned dicts are copies; mutating them changes nothing."""

    def __init__(self, path: str = DEX_PATH) -> None:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        self.path = path
        self.package_version: str | None = raw.get("package_version")
        self.gen: int | None = raw.get("gen")
        self._species, self._species_aliases = self._index(raw.get("species", {}))
        self._moves, self._move_aliases = self._index(raw.get("moves", {}))
        self._types: dict[str, dict] = {}
        self._type_names: dict[str, str] = {}
        for key, entry in raw.get("types", {}).items():
            type_id = to_id(key)
            self._types[type_id] = entry
            self._type_names[type_id] = str(entry.get("name") or key)
        self._items: dict[str, dict] = dict(raw.get("items", {}))
        self._abilities: dict[str, dict] = dict(raw.get("abilities", {}))
        self._revival_moves: set[str] = {to_id(m) for m in raw.get("revival_moves", [])}
        self._revival_learners: set[str] = {to_id(s) for s in raw.get("revival_learners", [])}

    @staticmethod
    def _index(table: dict[str, dict]) -> tuple[dict[str, dict], dict[str, str]]:
        """Copy a table keyed by id, adding "id" to each entry; map to_id(name) -> id where the two differ."""
        entries: dict[str, dict] = {}
        aliases: dict[str, str] = {}
        for key, entry in table.items():
            copy = dict(entry)
            copy["id"] = key
            entries[key] = copy
            alias = to_id(copy.get("name", ""))
            if alias and alias != key:
                aliases.setdefault(alias, key)
        return entries, aliases

    @staticmethod
    def _find(entries: dict[str, dict], aliases: dict[str, str], name_or_id: str) -> dict | None:
        """The cached entry itself (not a copy) for a name or id, or None."""
        key = to_id(name_or_id)
        entry = entries.get(key)
        if entry is None and key in aliases:
            entry = entries.get(aliases[key])
        return entry

    def species(self, name_or_id: str) -> dict | None:
        """Species entry as in the JSON plus "id" (a deep copy), or None if unknown."""
        entry = self._find(self._species, self._species_aliases, name_or_id)
        return copy.deepcopy(entry) if entry is not None else None

    def move(self, name_or_id: str) -> dict | None:
        """Move entry as in the JSON plus "id" (a deep copy), or None if unknown."""
        entry = self._find(self._moves, self._move_aliases, name_or_id)
        return copy.deepcopy(entry) if entry is not None else None

    def ability(self, name_or_id: str) -> dict | None:
        """Ability entry as in the JSON plus "id", or None if unknown."""
        key = to_id(name_or_id)
        entry = self._abilities.get(key)
        return dict(copy.deepcopy(entry), id=key) if entry is not None else None

    def item(self, name_or_id: str) -> dict | None:
        """Item entry as in the JSON plus "id", or None if unknown."""
        key = to_id(name_or_id)
        entry = self._items.get(key)
        return dict(copy.deepcopy(entry), id=key) if entry is not None else None

    def type_names(self) -> list[str]:
        """Canonical type names ("Fire", ...) in the dump's order."""
        return [self._type_names[key] for key in self._types]

    def effectiveness(self, move_type: str, defender_types: list[str]) -> float:
        """Type-chart multiplier of ``move_type`` against a defender with ``defender_types`` (product over types).

        Uses ``types[defender].damageTaken[move_type]`` with Showdown's encoding (0 -> 1x, 1 -> 2x, 2 -> 0.5x,
        3 -> 0x).  Unknown types on either side count as neutral.
        """
        attack_name = self._type_names.get(to_id(move_type))
        if attack_name is None:
            return 1.0
        multiplier = 1.0
        for defender_type in defender_types:
            entry = self._types.get(to_id(defender_type))
            if entry is None:
                continue
            code = entry.get("damageTaken", {}).get(attack_name, 0)
            multiplier *= DAMAGE_TAKEN_MULTIPLIER.get(code, 1.0)
        return multiplier

    def _require_species(self, species: str) -> dict:
        """The cached species entry (read-only use), KeyError if unknown."""
        entry = self._find(self._species, self._species_aliases, species)
        if entry is None:
            raise KeyError(f"unknown species: {species!r}")
        return entry

    def bst(self, species: str) -> int:
        """Base stat total of a species (KeyError if unknown)."""
        return int(sum(self._require_species(species)["baseStats"].values()))

    def is_revival_move(self, move_id: str) -> bool:
        """True if the move revives a fainted Pokémon (Revival Blessing)."""
        return to_id(move_id) in self._revival_moves

    def revival_moves(self) -> set[str]:
        """Ids of the in-battle revival moves."""
        return set(self._revival_moves)

    def revival_learners(self) -> set[str]:
        """Ids of the species that can learn a revival move."""
        return set(self._revival_learners)

    def estimate_stats(self, species: str, level: int = 100) -> dict:
        """Stats at ``level`` with 31 IVs, 0 EVs and a neutral nature (KeyError if the species is unknown).

        hp = floor((2*base+31)*L/100) + L + 10 (Shedinja's 1 base HP stays 1); others floor((2*base+31)*L/100) + 5.
        """
        base = self._require_species(species)["baseStats"]
        stats: dict[str, int] = {}
        for key in _STAT_KEYS:
            value = int(base.get(key, 0))
            inner = ((2 * value + 31) * level) // 100
            if key == "hp":
                stats[key] = 1 if value == 1 else inner + level + 10
            else:
                stats[key] = inner + 5
        return stats


_DEX: Dex | None = None


def get_dex() -> Dex:
    """The process-wide Dex loaded from DEX_PATH (loaded on first call)."""
    global _DEX
    if _DEX is None:
        _DEX = Dex()
    return _DEX


def average_hits(multihit: int | list | None) -> float:
    """Expected number of hits for a move's ``multihit`` field (None -> 1, int -> int, [2, 5] -> 3.1)."""
    if multihit is None:
        return 1.0
    if isinstance(multihit, (int, float)):
        return float(multihit)
    if isinstance(multihit, (list, tuple)) and multihit:
        low, high = float(multihit[0]), float(multihit[-1])
        if (low, high) == (2.0, 5.0):
            return TWO_TO_FIVE_AVERAGE_HITS
        return (low + high) / 2.0
    return 1.0


def ability_multiplier(defender_ability: str | None, move_type: str) -> float:
    """Damage multiplier from the defender's ability against a move of ``move_type`` (1.0 when not modelled)."""
    if not defender_ability:
        return 1.0
    table = ABILITY_TYPE_MULTIPLIERS.get(to_id(defender_ability))
    if not table:
        return 1.0
    return table.get(to_id(move_type), 1.0)


def _stat(stats: dict, key: str) -> float:
    """A stat as a positive float, defaulting to 100 when absent."""
    try:
        value = float(stats.get(key, 100))
    except (TypeError, ValueError):
        value = 100.0
    return value if value > 0 else 1.0


def damage_score_for_move(
    dex: Dex,
    attacker_types: list[str],
    attacker_stats: dict,
    move: dict,
    defender_types: list[str],
    defender_stats: dict,
    defender_ability: str | None = None,
) -> float:
    """``damage_score`` for a move given as a dict (a dex entry, or a synthetic one with type/basePower/category/accuracy)."""
    category = move.get("category")
    if category not in ("Physical", "Special"):
        return 0.0
    move_type = str(move.get("type") or "")
    multiplier = ability_multiplier(defender_ability, move_type)
    if multiplier == 0.0:
        return 0.0
    effectiveness = dex.effectiveness(move_type, list(defender_types))
    if effectiveness == 0.0:
        return 0.0
    base_power = move.get("basePower") or 0
    if base_power <= 0:
        move_id = to_id(move.get("id") or move.get("name") or "")
        if move.get("ohko") or move_id in FIXED_DAMAGE_MOVES:
            return FIXED_DAMAGE_SCORE
        return 0.0
    accuracy = move.get("accuracy", True)
    hit_chance = 1.0 if isinstance(accuracy, bool) or accuracy is None else float(accuracy) / 100.0
    stab = 1.5 if to_id(move_type) in {to_id(t) for t in attacker_types} else 1.0
    if category == "Physical":
        ratio = _stat(attacker_stats, "atk") / _stat(defender_stats, "def")
    else:
        ratio = _stat(attacker_stats, "spa") / _stat(defender_stats, "spd")
    hits = average_hits(move.get("multihit"))
    return float(base_power) * hit_chance * stab * effectiveness * multiplier * ratio * hits


def damage_score(
    dex: Dex,
    attacker_types: list[str],
    attacker_stats: dict,
    move_id: str,
    defender_types: list[str],
    defender_stats: dict,
    defender_ability: str | None = None,
) -> float:
    """Relative damage estimate of one move: basePower * accuracy * STAB * effectiveness * ability * atk/def * hits.

    0 for status moves, unknown moves and 0-power moves, except that fixed-damage and OHKO moves score a low constant
    (still 0 when the defender's types or ability make it immune).  Physical moves use atk/def, special spa/spd.
    """
    move = dex.move(move_id)
    if move is None:
        return 0.0
    return damage_score_for_move(dex, attacker_types, attacker_stats, move, defender_types, defender_stats, defender_ability)
