"""Scripted play for the harness: a deterministic heuristic battle policy, greedy team selection, a trivial agent.

The policy reads the simulator's request JSON directly (``sim/SIM-PROTOCOL.md``) and uses the per-player view only
for what the request does not contain: the opponent's active species and its revealed moves.  Choices are Showdown
choice strings without the ``>p1`` prefix: ``move 2``, ``switch 3``, ``team 123`` or ``default``.  Nothing here is
random, and nothing ever terastallizes.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

from harness.dex import Dex, damage_score, damage_score_for_move, get_dex, to_id

if TYPE_CHECKING:  # type names only: harness.showdown is written in parallel and is never imported at runtime
    from harness.showdown import OppMon, PlayerView

#: Weight of the opponent's best estimated hit when valuing a switch-in.
THREAT_WEIGHT = 0.5
#: Base power assumed for each of the opponent's types when none of its moves are known.
ASSUMED_STAB_POWER = 90
#: Base stat assumed for an opponent whose species is unknown.
_NEUTRAL_BASE_STAT = 80


@dataclass
class Combatant:
    """What the policy knows about one Pokémon, in the shape ``damage_score`` consumes."""

    species: str
    types: list[str] = field(default_factory=list)
    stats: dict[str, float] = field(default_factory=dict)
    ability: str | None = None
    moves: list[str] = field(default_factory=list)
    level: int = 100
    slot: int = 0
    active: bool = False
    fainted: bool = False


def parse_details(details: str) -> tuple[str, int]:
    """Species and level from a details string: "Pidgey, L50, F" -> ("Pidgey", 50); no "L" part means 100."""
    parts = [part.strip() for part in str(details or "").split(",")]
    species = parts[0] if parts else ""
    level = 100
    for part in parts[1:]:
        if len(part) >= 2 and part[0] == "L" and part[1:].isdigit():
            level = int(part[1:])
    return species, level


def parse_condition(condition: str) -> tuple[int, int | None, str | None]:
    """(hp, max hp or None, status or None) from "357/357", "120/357 par" or "0 fnt"."""
    text = str(condition or "").strip()
    status: str | None = None
    hp_part = text
    if " " in text:
        hp_part, status_part = text.split(" ", 1)
        status = status_part.strip() or None
    if "/" in hp_part:
        current, maximum = hp_part.split("/", 1)
        hp = int(current) if current.isdigit() else 0
        maxhp: int | None = int(maximum) if maximum.isdigit() else None
    else:
        hp = int(hp_part) if hp_part.isdigit() else 0
        maxhp = None
    return hp, maxhp, status


def is_fainted(entry: dict) -> bool:
    """True if a request's side.pokemon entry has fainted (condition ends in "fnt")."""
    return str(entry.get("condition", "")).strip().endswith("fnt")


def is_force_switch(request: dict) -> bool:
    """True if the request asks for a (forced) switch."""
    force = request.get("forceSwitch")
    if isinstance(force, list):
        return any(bool(flag) for flag in force)
    return bool(force)


def team_order(request: dict) -> str:
    """Team Preview answer keeping the given order: "team 123" for three Pokémon."""
    count = len(((request.get("side") or {}).get("pokemon")) or [])
    if count <= 0:
        return "default"
    return "team " + "".join(str(index) for index in range(1, min(count, 9) + 1))


def _clean_species(name: Any) -> str:
    """Species text from a species name or an ident like "p2a: Garchomp"."""
    text = str(name or "")
    if ":" in text:
        text = text.split(":", 1)[1]
    return text.strip()


def _neutral_stats(level: int) -> dict[str, float]:
    """Stats of a hypothetical Pokémon with every base stat equal to _NEUTRAL_BASE_STAT."""
    inner = ((2 * _NEUTRAL_BASE_STAT + 31) * level) // 100
    stats = {"hp": float(inner + level + 10)}
    for key in ("atk", "def", "spa", "spd", "spe"):
        stats[key] = float(inner + 5)
    return stats


def own_combatant(dex: Dex, entry: dict, slot: int) -> Combatant:
    """Combatant for one of our own side.pokemon request entries (real stats come from the request)."""
    species_name, level = parse_details(entry.get("details", ""))
    species = dex.species(species_name) if species_name else None
    stats = {key: float(value) for key, value in (entry.get("stats") or {}).items()}
    hp, maxhp, _status = parse_condition(entry.get("condition", ""))
    stats.setdefault("hp", float(maxhp if maxhp is not None else hp))
    if species is not None:
        for key, value in dex.estimate_stats(species_name, level).items():
            stats.setdefault(key, float(value))
    ability = entry.get("ability") or entry.get("baseAbility") or None
    return Combatant(
        species=species_name,
        types=list(species["types"]) if species is not None else [],
        stats=stats,
        ability=str(ability) if ability else None,
        moves=[to_id(move) for move in (entry.get("moves") or [])],
        level=level,
        slot=slot,
        active=bool(entry.get("active")),
        fainted=is_fainted(entry),
    )


def _opponent_level(opp: "OppMon | None") -> int:
    """Level of the opponent's active Pokémon from the view when it offers one, else 100."""
    if opp is None:
        return 100
    level = getattr(opp, "level", None)
    if isinstance(level, int) and not isinstance(level, bool) and level > 0:
        return level
    details = getattr(opp, "details", None)
    if isinstance(details, str) and details:
        return parse_details(details)[1]
    return 100


def _move_id_list(value: Any) -> list[str]:
    """Move ids from a list/tuple/set of move names or ids; anything else gives []."""
    if isinstance(value, (list, tuple)):
        return [to_id(move) for move in value if isinstance(move, str) and to_id(move)]
    if isinstance(value, (set, frozenset)):
        return sorted(to_id(move) for move in value if isinstance(move, str) and to_id(move))
    return []


def _record_moves(record: Any) -> list[str]:
    """Revealed move ids held by a record: a dict or object with "moves" or "revealed", or a bare collection."""
    if isinstance(record, dict):
        for key in ("moves", "revealed"):
            moves = _move_id_list(record.get(key))
            if moves:
                return moves
        return []
    if isinstance(record, (list, tuple, set, frozenset)):
        return _move_id_list(record)
    for key in ("moves", "revealed"):
        moves = _move_id_list(getattr(record, key, None))
        if moves:
            return moves
    return []


def _record_species(record: Any) -> str | None:
    """Species text stored on a record, if any."""
    if isinstance(record, dict):
        value = record.get("species")
    else:
        value = getattr(record, "species", None)
    return _clean_species(value) if isinstance(value, str) and value else None


def revealed_moves(view: "PlayerView | None", opp: "OppMon | None", species_name: str) -> list[str]:
    """Opponent move ids revealed so far, from view.opp_seen (keyed by species or ident) or the active OppMon."""
    target = to_id(species_name)
    records: list[Any] = []
    seen = getattr(view, "opp_seen", None) if view is not None else None
    if isinstance(seen, dict):
        for key, record in seen.items():
            key_matches = to_id(_clean_species(key)) == target
            record_species = _record_species(record)
            species_matches = record_species is not None and to_id(record_species) == target
            if target and (key_matches or species_matches):
                records.append(record)
    if opp is not None:
        records.append(opp)
    for record in records:
        moves = _record_moves(record)
        if moves:
            return moves
    return []


def opponent_combatant(dex: Dex, view: "PlayerView | None") -> Combatant:
    """Combatant for the opponent's active Pokémon: estimated stats; revealed moves if known, else none."""
    opp = getattr(view, "opp_active", None) if view is not None else None
    species_name = _clean_species(getattr(opp, "species", "")) if opp is not None else ""
    level = _opponent_level(opp)
    species = dex.species(species_name) if species_name else None
    if species is not None:
        types = list(species["types"])
        stats = {key: float(value) for key, value in dex.estimate_stats(species_name, level).items()}
    else:
        types = []
        stats = _neutral_stats(level)
    ability = getattr(opp, "ability", None) if opp is not None else None
    return Combatant(
        species=species_name,
        types=types,
        stats=stats,
        ability=str(ability) if isinstance(ability, str) and ability else None,
        moves=revealed_moves(view, opp, species_name),
        level=level,
        active=True,
        fainted=bool(getattr(opp, "fainted", False)) if opp is not None else False,
    )


def best_damage(dex: Dex, attacker: Combatant, move_ids: Iterable[str], defender: Combatant) -> float:
    """Highest damage_score among ``move_ids`` for attacker against defender (0 if none damages)."""
    best = 0.0
    for move_id in move_ids:
        score = damage_score(dex, attacker.types, attacker.stats, move_id, defender.types, defender.stats, defender.ability)
        if score > best:
            best = score
    return best


def threat(dex: Dex, opponent: Combatant, target: Combatant) -> float:
    """Opponent's best estimated hit on target: its revealed moves, else a STAB 90-power move of each of its types."""
    if opponent.moves:
        return best_damage(dex, opponent, opponent.moves, target)
    category = "Special" if opponent.stats.get("spa", 0.0) > opponent.stats.get("atk", 0.0) else "Physical"
    best = 0.0
    for type_name in opponent.types:
        move = {"name": f"assumed {type_name}", "type": type_name, "basePower": ASSUMED_STAB_POWER,
                "category": category, "accuracy": True}
        score = damage_score_for_move(dex, opponent.types, opponent.stats, move, target.types, target.stats, target.ability)
        if score > best:
            best = score
    return best


def switch_value(dex: Dex, candidate: Combatant, opponent: Combatant) -> tuple[float, float]:
    """(net value, own best damage) of switching ``candidate`` in: best damage minus THREAT_WEIGHT * threat."""
    own = best_damage(dex, candidate, candidate.moves, opponent)
    return own - THREAT_WEIGHT * threat(dex, opponent, candidate), own


class HeuristicPolicy:
    """Deterministic in-battle policy for the opponent bot and the scripted subjects (implements Agent.choose).

    Move requests: the non-disabled move with the highest damage_score against the opponent's active Pokémon; with no
    damaging move, a switch to the best bench Pokémon that has one (unless trapped), else the first non-disabled move.
    Forced switches: the bench Pokémon maximizing best damage minus half the opponent's best estimated hit, ties to
    the lowest slot.  Team Preview keeps the given order; "wait" requests answer "default".  After an invalid-choice
    error the policy flips: a rejected switch becomes the best move and a rejected move becomes the best switch.
    """

    def __init__(self, dex: Dex | None = None) -> None:
        self.dex = dex if dex is not None else get_dex()
        self.last_choice: str | None = None

    def choose(self, request: dict, view: "PlayerView | None", error: str | None = None) -> str:
        """Choice string for ``request`` ("move N", "switch N", "team 123" or "default")."""
        choice = self._decide(request or {}, view, error)
        self.last_choice = choice
        return choice

    def _last_kind(self) -> str:
        """"move", "switch" or "" for the previous answer."""
        if not self.last_choice:
            return ""
        return self.last_choice.split(" ", 1)[0]

    def _last_slot(self) -> int | None:
        """Slot number of the previous answer when it had one."""
        if not self.last_choice:
            return None
        parts = self.last_choice.split()
        if len(parts) == 2 and parts[1].isdigit():
            return int(parts[1])
        return None

    def _decide(self, request: dict, view: Any, error: str | None) -> str:
        if request.get("wait"):
            return "default"
        if request.get("teamPreview"):
            return team_order(request) if not error else "default"
        side = request.get("side") or {}
        own = [own_combatant(self.dex, entry, slot) for slot, entry in enumerate(side.get("pokemon") or [], start=1)]
        bench = [mon for mon in own if not mon.active and not mon.fainted]
        opponent = opponent_combatant(self.dex, view)

        if is_force_switch(request):
            exclude: set[int] = set()
            if error and self._last_kind() == "switch" and self._last_slot() is not None:
                exclude.add(self._last_slot())  # type: ignore[arg-type]
            best = self._best_switch(bench, opponent, exclude=exclude, require_damage=False)
            return f"switch {best.slot}" if best is not None else "default"

        active_data = (request.get("active") or [{}])[0] or {}
        trapped = bool(active_data.get("trapped"))
        attacker = next((mon for mon in own if mon.active), own[0] if own else None)
        usable = [(index, entry) for index, entry in enumerate(active_data.get("moves") or [], start=1)
                  if not entry.get("disabled")]
        ranked = self._rank_moves(attacker, usable, opponent)

        if error and self._last_kind() == "switch":
            return f"move {ranked[0][1]}" if ranked else "default"
        if error and self._last_kind() == "move":
            if not trapped:
                best = self._best_switch(bench, opponent, exclude=set(), require_damage=False)
                if best is not None:
                    return f"switch {best.slot}"
            for _score, index in ranked:
                if f"move {index}" != self.last_choice:
                    return f"move {index}"
            return "default"

        if ranked and ranked[0][0] > 0.0:
            return f"move {ranked[0][1]}"
        if not trapped:
            best = self._best_switch(bench, opponent, exclude=set(), require_damage=True)
            if best is not None:
                return f"switch {best.slot}"
        if ranked:
            return f"move {ranked[0][1]}"
        return "default"

    def _rank_moves(self, attacker: Combatant | None, usable: list[tuple[int, dict]],
                    opponent: Combatant) -> list[tuple[float, int]]:
        """(score, 1-based index) of the usable moves, best first, ties by lowest index."""
        ranked: list[tuple[float, int]] = []
        for index, entry in usable:
            move_id = to_id(entry.get("id") or entry.get("move") or "")
            score = 0.0
            if attacker is not None and move_id:
                score = damage_score(self.dex, attacker.types, attacker.stats, move_id,
                                     opponent.types, opponent.stats, opponent.ability)
            ranked.append((score, index))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return ranked

    def _best_switch(self, bench: list[Combatant], opponent: Combatant, exclude: set[int],
                     require_damage: bool) -> Combatant | None:
        """Bench Pokémon with the highest switch_value (ties to the lowest slot), optionally only those that can damage."""
        best: Combatant | None = None
        best_value = 0.0
        for candidate in bench:
            if candidate.slot in exclude:
                continue
            value, own = switch_value(self.dex, candidate, opponent)
            if require_damage and own <= 0.0:
                continue
            if best is None or value > best_value:
                best, best_value = candidate, value
        return best


def greedy_select(roster: list[dict], available: set[str], k: int = 3, dex: Dex | None = None) -> list[str]:
    """Names of the ``k`` available roster entries with the highest BST (aces first on ties, then by name).

    Roster entries have "name", "species" and "ace"; ``available`` holds names (matched case/space-insensitively).
    With fewer than ``k`` available, all of them are returned in that order.
    """
    dex = dex if dex is not None else get_dex()
    wanted = {to_id(name) for name in available}
    candidates = [entry for entry in roster if to_id(entry.get("name", "")) in wanted]
    ranked = sorted(
        candidates,
        key=lambda entry: (-dex.bst(entry["species"]), not bool(entry.get("ace")), str(entry.get("name", ""))),
    )
    return [str(entry["name"]) for entry in ranked[:k]]


def first_legal(request: dict) -> str:
    """Trivial agent: the first non-disabled move, else the first legal switch, else "default"."""
    request = request or {}
    if request.get("wait"):
        return "default"
    if request.get("teamPreview"):
        return team_order(request)
    pokemon = (request.get("side") or {}).get("pokemon") or []

    def first_switch() -> str | None:
        for index, entry in enumerate(pokemon, start=1):
            if not entry.get("active") and not is_fainted(entry):
                return f"switch {index}"
        return None

    if is_force_switch(request):
        return first_switch() or "default"
    active_data = (request.get("active") or [{}])[0] or {}
    for index, entry in enumerate(active_data.get("moves") or [], start=1):
        if not entry.get("disabled"):
            return f"move {index}"
    if not active_data.get("trapped"):
        switch = first_switch()
        if switch is not None:
            return switch
    return "default"
