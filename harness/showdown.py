"""One Pokémon Showdown battle over stdio.

What this module does:

* spawns ``node showdown/sim_stdio.js`` (one subprocess per battle) and speaks the
  ``update`` / ``sideupdate`` / ``end`` block framing described in
  ``sim/SIMULATOR.md`` of the pinned pokemon-showdown package;
* keeps one :class:`PlayerView` per side that follows the public protocol lines
  for that side, renders them as English, and turns ``|request|`` objects into
  the message an LLM subject reads and the choice strings the simulator accepts;
* records ground truth: every raw stdout line goes to ``log_path`` and every
  stdin line to ``input_path`` as written, and every ``|faint|`` line becomes a
  :class:`FaintEvent` with the turn it happened on.

Nothing here is random: battle randomness comes only from the explicit seed.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, Protocol

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE_PATH: str = os.environ.get("PERMADEATH_NODE", os.path.join(_ROOT, ".tools", "node", "bin", "node"))
SIM_SCRIPT: str = os.environ.get("PERMADEATH_SIM", os.path.join(_ROOT, "showdown", "sim_stdio.js"))
DEX_PATH: str = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "dex_gen9.json")

# Seconds to wait for a line of simulator output before giving up (the simulator
# answers in milliseconds; this only fires if the subprocess hangs or dies silently).
SIM_READ_TIMEOUT: float = 120.0


class SimError(Exception):
    """The simulator subprocess crashed, hung, or produced something unreadable."""


def battle_seed(run_id: str, battle_no: int) -> list[int]:
    """Four 16-bit ints derived from sha256(f"{run_id}:battle:{battle_no}")."""
    digest = hashlib.sha256(f"{run_id}:battle:{battle_no}".encode("utf-8")).digest()
    return [int.from_bytes(digest[2 * i : 2 * i + 2], "big") for i in range(4)]


# --------------------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------------------


@dataclass
class FaintEvent:
    """One ``|faint|`` line: the turn it happened on, the side, and the species name."""

    turn: int
    side: str
    name: str


@dataclass
class OwnMon:
    """One Pokémon of the viewing side, as the last ``|request|`` described it."""

    name: str
    species: str
    hp: int
    maxhp: int
    status: str
    fainted: bool
    active: bool
    moves: list[str]
    level: int
    slot: int  # 1-based position in the request's side.pokemon list


@dataclass
class OppMon:
    """What the viewing side knows about one opposing Pokémon."""

    species: str
    hp_pct: int
    status: str
    fainted: bool
    revealed: bool
    level: int = 100                                   # from the details string ("Raticate, L85")
    moves: list[str] = field(default_factory=list)     # move names this side has seen it use


@dataclass
class BattleResult:
    """Outcome of one battle plus the ground-truth faint list and raw log locations."""

    winner: str | None
    turns: int
    faints: list[FaintEvent]
    end_json: dict
    log_path: str
    input_path: str
    forced_tie: bool
    p1_fainted: list[str]
    p2_fainted: list[str]


class Agent(Protocol):
    """Anything that answers a simulator request with a choice string ("move 2", "switch 3", "default")."""

    def choose(self, request: dict, view: "PlayerView", error: str | None) -> str: ...


# --------------------------------------------------------------------------------------
# Small parsing helpers
# --------------------------------------------------------------------------------------

_IDENT_RE = re.compile(r"^(p[1-4])[a-z]?: (.*)$")

STATUS_WORDS: dict[str, str] = {
    "par": "paralyzed",
    "brn": "burned",
    "psn": "poisoned",
    "tox": "badly poisoned",
    "slp": "asleep",
    "frz": "frozen",
    "fnt": "fainted",
}

STAT_NAMES: dict[str, str] = {
    "atk": "Attack",
    "def": "Defense",
    "spa": "Special Attack",
    "spd": "Special Defense",
    "spe": "Speed",
    "accuracy": "accuracy",
    "evasion": "evasiveness",
}

_WEATHER_TEXT: dict[str, str] = {
    "sandstorm": "A sandstorm kicked up.",
    "raindance": "It started to rain.",
    "sunnyday": "The sunlight turned harsh.",
    "hail": "It started to hail.",
    "snow": "It started to snow.",
    "snowscape": "It started to snow.",
}

_PROTECT_MOVES = {
    "protect", "detect", "spikyshield", "banefulbunker", "kingsshield", "silktrap",
    "burningbulwark", "obstruct", "maxguard",
}


def to_id(text: str) -> str:
    """Showdown-style id: lowercase with everything but letters and digits removed."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def parse_ident(ident: str) -> tuple[str, str]:
    """Split "p1a: Garchomp" / "p1: Garchomp" into ("p1", "Garchomp"); unknown forms give ("", ident)."""
    m = _IDENT_RE.match(ident.strip())
    if not m:
        return "", ident.strip()
    return m.group(1), m.group(2).strip()


def parse_details(details: str) -> tuple[str, int]:
    """Split a details string like "Pidgey, L50, F" into (species, level); level defaults to 100."""
    fields = [f.strip() for f in details.split(",")]
    species = fields[0] if fields and fields[0] else details.strip()
    level = 100
    for f in fields[1:]:
        if len(f) > 1 and f[0] == "L" and f[1:].isdigit():
            level = int(f[1:])
    return species, level


def _int(text: str) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return 0


def parse_condition(cond: str) -> tuple[int, int | None, str]:
    """Parse "213/357 par" -> (213, 357, "par"), "0 fnt" -> (0, None, "fnt"), "64/100" -> (64, 100, "")."""
    cond = (cond or "").strip()
    hp_part, _, status = cond.partition(" ")
    status = status.strip()
    if "/" in hp_part:
        cur, mx = hp_part.split("/", 1)
        return _int(cur), _int(mx), status
    return _int(hp_part), None, status


def hp_pct(hp: int, maxhp: int | None) -> int:
    """Whole-number HP percentage; 0 only when HP is 0, never below 1 while alive."""
    if hp <= 0:
        return 0
    if not maxhp:
        return min(100, hp)
    return max(1, round(100 * hp / maxhp))


def _split_line(line: str) -> tuple[str, list[str], dict[str, str]]:
    """Split a protocol line into (command, positional args, [tag] args)."""
    parts = line.split("|")
    if len(parts) < 2:
        return "", [], {}
    args: list[str] = []
    tags: dict[str, str] = {}
    for part in parts[2:]:
        if part.startswith("["):
            close = part.find("]")
            if close > 0:
                tags[part[1:close]] = part[close + 1 :].strip()
            else:
                tags[part[1:]] = ""
        else:
            args.append(part)
    return parts[1], args, tags


def _arg(args: list[str], i: int) -> str:
    return args[i] if i < len(args) else ""


def _effect_name(effect: str) -> str:
    """Strip "move: " / "item: " / "ability: " prefixes from an effect reference."""
    for prefix in ("move: ", "item: ", "ability: "):
        if effect.startswith(prefix):
            return effect[len(prefix) :]
    return effect


_FROM_WORDS: dict[str, str] = {
    "psn": "poison",
    "tox": "poison",
    "brn": "its burn",
    "recoil": "recoil",
    "confusion": "confusion",
    "sandstorm": "the sandstorm",
    "hail": "the hail",
}


def _from_text(tags: dict[str, str], view: "PlayerView", side: str) -> str:
    """" from Life Orb" / " from recoil" / " from Rough Skin of Opponent's Garchomp" or ""."""
    src = tags.get("from", "")
    if not src:
        return ""
    name = _FROM_WORDS.get(to_id(src), _effect_name(src))
    text = f" from {name}"
    of = tags.get("of", "")
    if of:
        text += f" of {_who(of, side)}"
    return text


def _who(ident: str, side: str) -> str:
    """"Garchomp" for the viewing side's Pokémon, "Opponent's Gengar" for the other side's."""
    owner, name = parse_ident(ident)
    if owner == side or not owner:
        return name
    return f"Opponent's {name}"


def _your(ident: str, side: str) -> str:
    """"Your Garchomp" / "Opponent's Gengar"."""
    owner, name = parse_ident(ident)
    if owner == side or not owner:
        return f"Your {name}"
    return f"Opponent's {name}"


_MOVE_NAMES: dict[str, str] | None = None


def _dex_move_names() -> dict[str, str]:
    """Move id -> display name from the Gen 9 dex dump (empty if the file is unavailable)."""
    global _MOVE_NAMES
    if _MOVE_NAMES is None:
        names: dict[str, str] = {}
        try:
            with open(DEX_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for mid, info in (data.get("moves") or {}).items():
                if isinstance(info, dict) and info.get("name"):
                    names[mid] = str(info["name"])
        except (OSError, ValueError):
            names = {}
        _MOVE_NAMES = names
    return _MOVE_NAMES


def _is_fainted(pokemon: dict) -> bool:
    hp, _, status = parse_condition(pokemon.get("condition", ""))
    return status == "fnt" or hp <= 0


# --------------------------------------------------------------------------------------
# Protocol line filtering and English rendering
# --------------------------------------------------------------------------------------


def filter_for_side(lines: list[str], side: str) -> list[str]:
    """Resolve the ``|split|pX`` blocks of ONE update block for ``side``.

    A split block is ``|split|pX`` followed by a SECRET line and a PUBLIC line: the
    secret line is kept when pX is ``side``, the public line otherwise (an absent or
    empty public line keeps nothing). ``|request|`` and ``|t:|`` lines are dropped.
    """
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("|split|"):
            owner = line[len("|split|") :].strip()
            secret = lines[i + 1] if i + 1 < n else ""
            public = lines[i + 2] if i + 2 < n else ""
            chosen = secret if owner == side else public
            if chosen and not chosen.startswith("|request|") and not chosen.startswith("|t:|"):
                out.append(chosen)
            i += 3
            continue
        if line and not line.startswith("|request|") and not line.startswith("|t:|"):
            out.append(line)
        i += 1
    return out


def render_line(line: str, side: str, view: "PlayerView") -> str | None:
    """One protocol line -> English from ``side``'s perspective, or None for lines not worth showing.

    The view is read (not changed) for context: previous HP values give the HP lost or
    restored, player names resolve ``|win|``. Own Pokémon are named plainly, the other
    side's with "Opponent's".
    """
    cmd, args, tags = _split_line(line)
    if not cmd:
        return None
    a0 = _arg(args, 0)

    if cmd == "move":
        return f"{_who(a0, side)} used {_arg(args, 1)}."

    if cmd in ("switch", "drag", "replace"):
        owner, name = parse_ident(a0)
        hp, maxhp, status = parse_condition(_arg(args, 2))
        status_text = f", {STATUS_WORDS.get(status, status)}" if status and status != "fnt" else ""
        if owner == side:
            if maxhp is None:
                maxhp = view.own_hp_of(name)[1]
            hp_text = f"{hp}/{maxhp}"
            if cmd == "drag":
                return f"Your {name} was dragged out ({hp_text}{status_text})."
            if cmd == "replace":
                return f"Your {name} was revealed ({hp_text}{status_text})."
            return f"You sent out {name} ({hp_text}{status_text})."
        pct = hp_pct(hp, maxhp)
        if cmd == "drag":
            return f"Opponent's {name} was dragged out ({pct}%{status_text})."
        if cmd == "replace":
            return f"Opponent's {name} was revealed ({pct}%{status_text})."
        return f"Opponent sent out {name} ({pct}%{status_text})."

    if cmd == "faint":
        owner, name = parse_ident(a0)
        return f"{name} fainted." if owner == side else f"Opponent's {name} fainted."

    if cmd in ("-damage", "-heal", "-sethp"):
        if "silent" in tags:
            return None
        owner, name = parse_ident(a0)
        hp, maxhp, _status = parse_condition(_arg(args, 1))
        suffix = _from_text(tags, view, side)
        if owner == side:
            prev_hp, prev_max, _ = view.own_hp_of(name)
            if maxhp is None:
                maxhp = prev_max
            left = f"{hp}/{maxhp} left"
            if prev_max == 0:
                return f"Your {name} is at {hp}/{maxhp} HP{suffix}."
            delta = hp - prev_hp
            if cmd == "-damage" or (cmd == "-sethp" and delta < 0):
                return f"Your {name} lost {abs(delta)} HP ({left}){suffix}."
            return f"Your {name} restored {abs(delta)} HP ({left}){suffix}."
        pct = hp_pct(hp, maxhp)
        known = view.opp_seen.get(name)
        if known is None:
            return f"Opponent's {name} is at {pct}% HP{suffix}."
        delta = pct - known.hp_pct
        if cmd == "-damage" or (cmd == "-sethp" and delta < 0):
            return f"Opponent's {name} lost {abs(delta)}% HP ({pct}% left){suffix}."
        return f"Opponent's {name} restored {abs(delta)}% HP ({pct}% left){suffix}."

    if cmd == "-status":
        status = _arg(args, 1)
        who = _who(a0, side)
        if status == "slp":
            return f"{who} fell asleep."
        if status == "frz":
            return f"{who} was frozen solid."
        return f"{who} was {STATUS_WORDS.get(status, status)}."

    if cmd == "-curestatus":
        status = _arg(args, 1)
        who = _who(a0, side)
        if status == "slp":
            return f"{who} woke up."
        if status == "frz":
            return f"{who} thawed out."
        if status == "par":
            return f"{who} was cured of paralysis."
        if status == "brn":
            return f"{who}'s burn was healed."
        if status in ("psn", "tox"):
            return f"{who} was cured of poison."
        return f"{who} recovered from {status}."

    if cmd == "-cureteam":
        return f"{_who(a0, side)}'s team was cured of status conditions."

    if cmd in ("-boost", "-unboost"):
        stat = STAT_NAMES.get(_arg(args, 1), _arg(args, 1))
        amount = _int(_arg(args, 2))
        if amount == 0:
            limit = "higher" if cmd == "-boost" else "lower"
            return f"{_who(a0, side)}'s {stat} won't go any {limit}."
        if cmd == "-boost":
            verb = "rose" if amount <= 1 else ("sharply rose" if amount == 2 else "rose drastically")
            return f"{_who(a0, side)}'s {stat} {verb} (+{amount})."
        verb = "fell" if amount <= 1 else ("harshly fell" if amount == 2 else "severely fell")
        return f"{_who(a0, side)}'s {stat} {verb} (-{amount})."

    if cmd == "-setboost":
        stat = STAT_NAMES.get(_arg(args, 1), _arg(args, 1))
        return f"{_who(a0, side)}'s {stat} was set to {_int(_arg(args, 2)):+d}."
    if cmd == "-clearboost":
        return f"{_who(a0, side)}'s stat changes were removed."
    if cmd == "-clearallboost":
        return "All stat changes were eliminated."
    if cmd == "-clearnegativeboost":
        return f"{_who(a0, side)}'s negative stat changes were removed."
    if cmd == "-clearpositiveboost":
        return f"{_who(a0, side)}'s positive stat changes were removed."
    if cmd == "-invertboost":
        return f"{_who(a0, side)}'s stat changes were inverted."
    if cmd == "-copyboost":
        return f"{_who(a0, side)} copied {_who(_arg(args, 1), side)}'s stat changes."
    if cmd == "-swapboost":
        return f"{_who(a0, side)} swapped stat changes with {_who(_arg(args, 1), side)}."

    if cmd == "-weather":
        if "upkeep" in tags:
            return None
        if to_id(a0) in ("", "none"):
            return "The weather cleared."
        return _WEATHER_TEXT.get(to_id(a0), f"The weather became {a0}.")

    if cmd == "-fieldstart":
        return f"{_effect_name(a0)} began."
    if cmd == "-fieldend":
        return f"{_effect_name(a0)} ended."
    if cmd == "-fieldactivate":
        return f"{_effect_name(a0)} activated."

    if cmd in ("-sidestart", "-sideend"):
        owner = a0[:2]
        where = "your side" if owner == side else "the opponent's side"
        cond = _effect_name(_arg(args, 1))
        if cmd == "-sidestart":
            return f"{cond} was set up on {where}."
        return f"{cond} ended on {where}."
    if cmd == "-swapsideconditions":
        return "Side conditions were swapped."

    if cmd == "-crit":
        return f"A critical hit on {_who(a0, side)}."
    if cmd == "-supereffective":
        return f"It was super effective against {_who(a0, side)}."
    if cmd == "-resisted":
        return f"It was not very effective against {_who(a0, side)}."
    if cmd == "-immune":
        src = tags.get("from", "")
        extra = f" ({_effect_name(src)})" if src else ""
        return f"It did not affect {_who(a0, side)}{extra}."
    if cmd == "-miss":
        target = _arg(args, 1)
        if target:
            return f"{_who(target, side)} avoided the attack."
        return f"{_who(a0, side)}'s attack missed."
    if cmd == "-fail":
        return "But it failed."
    if cmd == "-block":
        return f"The effect was blocked by {_who(a0, side)}'s {_effect_name(_arg(args, 1))}."
    if cmd == "-notarget":
        return "But there was no target."
    if cmd == "-ohko":
        return "It was a one-hit KO."
    if cmd == "-hitcount":
        n = _int(_arg(args, 1))
        return f"Hit {n} time{'s' if n != 1 else ''}."
    if cmd == "-nothing":
        return "But nothing happened."

    if cmd == "cant":
        who = _who(a0, side)
        reason = _arg(args, 1)
        move = _arg(args, 2)
        rid = to_id(reason)
        if rid == "par":
            return f"{who} is paralyzed and can't move."
        if rid == "slp":
            return f"{who} is fast asleep."
        if rid == "frz":
            return f"{who} is frozen solid."
        if rid == "flinch":
            return f"{who} flinched and couldn't move."
        if rid == "recharge":
            return f"{who} must recharge."
        if rid == "truant":
            return f"{who} is loafing around."
        if rid == "attract":
            return f"{who} is in love and can't move."
        if rid == "nopp":
            return f"{who} has no PP left for {move or 'its move'}."
        if rid in ("taunt", "disable", "imprison", "healblock", "gravity", "throatchop", "encore", "torment"):
            if move:
                return f"{who} can't use {move} because of {_effect_name(reason)}."
            return f"{who} can't move because of {_effect_name(reason)}."
        if move:
            return f"{who} can't use {move} ({_effect_name(reason)})."
        return f"{who} can't move ({_effect_name(reason)})."

    if cmd == "-start":
        who = _who(a0, side)
        effect = _effect_name(_arg(args, 1))
        eid = to_id(effect)
        extra = _arg(args, 2)
        cause = f" ({_effect_name(tags['from'])})" if tags.get("from") else ""
        if eid == "confusion":
            text = f"{who} became confused"
        elif eid == "substitute":
            text = f"{who} put up a substitute"
        elif eid == "leechseed":
            text = f"{who} was seeded"
        elif eid == "taunt":
            text = f"{who} fell for the taunt"
        elif eid == "encore":
            text = f"{who} received an encore"
        elif eid == "disable":
            text = f"{who}'s {extra or 'move'} was disabled"
        elif eid == "yawn":
            text = f"{who} grew drowsy"
        elif eid == "attract":
            text = f"{who} fell in love"
        elif eid == "curse":
            text = f"{who} was cursed"
        elif eid == "focusenergy":
            text = f"{who} is getting pumped"
        elif eid == "typechange":
            text = f"{who} changed type to {extra}"
        elif eid.startswith("perish"):
            text = f"{who}'s perish count is {eid[len('perish'):] or '?'}"
        else:
            text = f"{who}: {effect} started"
        return f"{text}{cause}."

    if cmd == "-end":
        who = _who(a0, side)
        effect = _effect_name(_arg(args, 1))
        eid = to_id(effect)
        if eid == "confusion":
            return f"{who} snapped out of confusion."
        if eid == "substitute":
            return f"{who}'s substitute faded."
        return f"{who}: {effect} ended."

    if cmd == "-activate":
        effect = _effect_name(_arg(args, 1))
        eid = to_id(effect)
        if not a0 or not parse_ident(a0)[0]:
            return f"{_effect_name(a0) or effect} activated."
        who = _who(a0, side)
        if eid == "confusion":
            return f"{who} is confused."
        if eid in _PROTECT_MOVES:
            return f"{who} protected itself."
        if eid == "substitute":
            return f"The substitute took the hit for {who}."
        if eid == "struggle":
            return f"{who} has no moves left."
        if eid == "destinybond":
            return f"{who} took its attacker down with it."
        return f"{who}: {effect} activated."

    if cmd == "-item":
        who = _who(a0, side)
        item = _arg(args, 1)
        src = tags.get("from", "")
        if src:
            return f"{who} obtained {item} ({_effect_name(src)})."
        return f"{who} is holding {item}."
    if cmd == "-enditem":
        if "silent" in tags:
            return None
        who = _who(a0, side)
        item = _arg(args, 1)
        if "eat" in tags:
            return f"{who} ate its {item}."
        src = tags.get("from", "")
        if src:
            return f"{who} lost its {item} ({_effect_name(src)})."
        if to_id(item) == "airballoon":
            return f"{who}'s Air Balloon popped."
        return f"{who}'s {item} was used up."

    if cmd == "-ability":
        who = _who(a0, side)
        ability = _arg(args, 1)
        src = tags.get("from", "")
        if src:
            return f"{who}'s ability became {ability} ({_effect_name(src)})."
        return f"{who}'s {ability} activated."
    if cmd == "-endability":
        return f"{_who(a0, side)}'s ability was suppressed."

    if cmd == "-transform":
        return f"{_who(a0, side)} transformed into {_arg(args, 1)}."
    if cmd in ("-formechange", "detailschange"):
        species, _ = parse_details(_arg(args, 1))
        return f"{_who(a0, side)} changed forme to {species}."
    if cmd == "-terastallize":
        return f"{_who(a0, side)} terastallized into the {_arg(args, 1)} type."

    if cmd == "-prepare":
        return f"{_who(a0, side)} is preparing {_arg(args, 1)}."
    if cmd == "-mustrecharge":
        return f"{_who(a0, side)} must recharge."
    if cmd == "-singleturn":
        move = _effect_name(_arg(args, 1))
        mid = to_id(move)
        if mid in _PROTECT_MOVES:
            return f"{_who(a0, side)} protected itself."
        if mid == "endure":
            return f"{_who(a0, side)} braced itself."
        if mid == "focuspunch":
            return f"{_who(a0, side)} is tightening its focus."
        return None
    if cmd == "-singlemove":
        move = _effect_name(_arg(args, 1))
        if to_id(move) == "destinybond":
            return f"{_who(a0, side)} is trying to take its attacker down with it."
        return None

    if cmd == "-message":
        return a0 or None
    if cmd == "-hint":
        return f"({a0})" if a0 else None

    if cmd == "win":
        own_name = view.player_names.get(side)
        other = "p2" if side == "p1" else "p1"
        if own_name is not None and a0 == own_name:
            return "You won the battle."
        if a0 == view.player_names.get(other) or own_name is not None:
            return "You lost the battle."
        return f"{a0} won the battle."
    if cmd == "tie":
        return "The battle ended in a tie."

    return None


# --------------------------------------------------------------------------------------
# PlayerView
# --------------------------------------------------------------------------------------


class PlayerView:
    """What one side has seen of the battle, plus the rendering and choice helpers for it."""

    def __init__(self, side: str, battle_no: int) -> None:
        self.side: str = side
        self.battle_no: int = battle_no
        self.turn: int = 0
        self.own: list[OwnMon] = []
        self.opp_active: OppMon | None = None
        self.opp_seen: dict[str, OppMon] = {}
        self.opp_team_size: int = 0
        self.last_request: dict | None = None
        self.pending: list[str] = []
        self.player_names: dict[str, str] = {}
        self.result: str | None = None  # "win" | "loss" | "tie" once a |win|/|tie| line was seen
        self._own_hp: dict[str, tuple[int, int, str]] = {}  # name -> (hp, maxhp, status)
        self._own_fainted: list[str] = []
        self._move_names: dict[str, str] = {}
        self._shown: list[str] = []  # events shown for the current decision point

    # ---- state tracking --------------------------------------------------------------

    def own_hp_of(self, name: str) -> tuple[int, int, str]:
        """(hp, maxhp, status) last known for one of this side's Pokémon; (0, 0, "") if unknown."""
        return self._own_hp.get(name, (0, 0, ""))

    def _own_entry(self, name: str) -> OwnMon | None:
        for mon in self.own:
            if mon.name == name:
                return mon
        return None

    def _set_own_hp(self, name: str, hp: int, maxhp: int | None, status: str) -> None:
        prev = self._own_hp.get(name)
        if maxhp is None:
            maxhp = prev[1] if prev else 0
        self._own_hp[name] = (hp, maxhp, status)
        mon = self._own_entry(name)
        if mon is not None:
            mon.hp, mon.maxhp, mon.status = hp, maxhp, status
            if status == "fnt" or hp <= 0:
                mon.fainted = True

    def _opp_entry(self, name: str, species: str | None = None) -> OppMon:
        mon = self.opp_seen.get(name)
        if mon is None:
            mon = OppMon(species=species or name, hp_pct=100, status="", fainted=False, revealed=True)
            self.opp_seen[name] = mon
        return mon

    def _track(self, line: str) -> None:
        """Update turn, player names, own HP, opponent knowledge and the result from one line."""
        cmd, args, _tags = _split_line(line)
        a0 = _arg(args, 0)
        if cmd == "turn":
            self.turn = _int(a0)
        elif cmd == "player":
            if a0 and len(args) > 1:
                self.player_names[a0] = _arg(args, 1)
        elif cmd == "teamsize":
            if a0 and a0 != self.side:
                self.opp_team_size = _int(_arg(args, 1))
        elif cmd == "poke":
            if a0 and a0 != self.side:
                species, level = parse_details(_arg(args, 1))
                self._opp_entry(species, species).level = level
        elif cmd == "move":
            owner, name = parse_ident(a0)
            move_name = _arg(args, 1)
            if owner and owner != self.side and move_name:
                mon = self._opp_entry(name)
                if move_name not in mon.moves:
                    mon.moves.append(move_name)
        elif cmd in ("switch", "drag", "replace"):
            owner, name = parse_ident(a0)
            species, level = parse_details(_arg(args, 1))
            hp, maxhp, status = parse_condition(_arg(args, 2))
            if owner == self.side:
                self._set_own_hp(name, hp, maxhp, status)
                for mon in self.own:
                    mon.active = mon.name == name
            elif owner:
                mon = self._opp_entry(name, species)
                mon.species = species or mon.species
                mon.level = level
                mon.hp_pct = hp_pct(hp, maxhp)
                mon.status = status if status != "fnt" else ""
                mon.fainted = status == "fnt" or hp <= 0
                mon.revealed = True
                self.opp_active = mon
        elif cmd in ("-damage", "-heal", "-sethp"):
            owner, name = parse_ident(a0)
            hp, maxhp, status = parse_condition(_arg(args, 1))
            if owner == self.side:
                self._set_own_hp(name, hp, maxhp, status)
            elif owner:
                mon = self._opp_entry(name)
                mon.hp_pct = hp_pct(hp, maxhp)
                if status == "fnt" or hp <= 0:
                    mon.fainted = True
                    mon.status = ""
                else:
                    mon.status = status
        elif cmd == "faint":
            owner, name = parse_ident(a0)
            if owner == self.side:
                if name not in self._own_fainted:
                    self._own_fainted.append(name)
                _hp, maxhp, _ = self.own_hp_of(name)
                self._set_own_hp(name, 0, maxhp, "fnt")
            elif owner:
                mon = self._opp_entry(name)
                mon.fainted = True
                mon.hp_pct = 0
                mon.status = ""
        elif cmd == "-status":
            owner, name = parse_ident(a0)
            status = _arg(args, 1)
            if owner == self.side:
                hp, maxhp, _ = self.own_hp_of(name)
                self._set_own_hp(name, hp, maxhp, status)
            elif owner:
                self._opp_entry(name).status = status
        elif cmd == "-curestatus":
            owner, name = parse_ident(a0)
            if owner == self.side:
                hp, maxhp, _ = self.own_hp_of(name)
                self._set_own_hp(name, hp, maxhp, "")
            elif owner:
                self._opp_entry(name).status = ""
        elif cmd == "win":
            self.result = "win" if a0 == self.player_names.get(self.side) else "loss"
        elif cmd == "tie":
            self.result = "tie"

    def observe(self, lines: list[str]) -> None:
        """Consume update-block lines already filtered for this side: track state, buffer English."""
        for line in lines:
            text = render_line(line, self.side, self)
            self._track(line)
            if text:
                self.pending.append(text)

    def update_request(self, request: dict) -> None:
        """Refresh ``own`` from request["side"]["pokemon"] and remember the request."""
        self.last_request = request
        side = request.get("side") or {}
        active_list = request.get("active") or []
        if active_list and isinstance(active_list[0], dict):
            for m in active_list[0].get("moves") or []:
                if m.get("id") and m.get("move"):
                    self._move_names[m["id"]] = m["move"]
        dex_names = _dex_move_names()
        own: list[OwnMon] = []
        for slot, p in enumerate(side.get("pokemon") or [], 1):
            _owner, name = parse_ident(p.get("ident", ""))
            species, level = parse_details(p.get("details", ""))
            hp, maxhp, status = parse_condition(p.get("condition", ""))
            if maxhp is None:
                maxhp = self.own_hp_of(name)[1]
            fainted = status == "fnt" or hp <= 0
            moves = [self._move_names.get(mid) or dex_names.get(mid) or mid for mid in p.get("moves") or []]
            own.append(
                OwnMon(
                    name=name,
                    species=species,
                    hp=hp,
                    maxhp=maxhp,
                    status=status if status != "fnt" else "",
                    fainted=fainted,
                    active=bool(p.get("active")),
                    moves=moves,
                    level=level,
                    slot=slot,
                )
            )
            self._own_hp[name] = (hp, maxhp, status)
            if fainted and name not in self._own_fainted:
                self._own_fainted.append(name)
        self.own = own

    def take_pending(self) -> list[str]:
        """Return and clear the buffered English event lines."""
        out = self.pending
        self.pending = []
        return out

    def own_fainted(self) -> list[str]:
        """Names of this side's Pokémon that have fainted, in team order."""
        names = [mon.name for mon in self.own if mon.fainted]
        for name in self._own_fainted:
            if name not in names:
                names.append(name)
        return names

    # ---- rendering -------------------------------------------------------------------

    def _events_for_render(self) -> list[str]:
        """Pending events, or the events already shown for this decision point if none are new."""
        fresh = self.take_pending()
        if fresh:
            self._shown = fresh
        return list(self._shown)

    @staticmethod
    def _mon_state(mon: OwnMon) -> str:
        if mon.fainted:
            return "fainted"
        text = f"{mon.hp}/{mon.maxhp} HP"
        if mon.status:
            text += f", {STATUS_WORDS.get(mon.status, mon.status)}"
        return text

    @staticmethod
    def _opp_state(mon: OppMon) -> str:
        if mon.fainted:
            return "fainted"
        text = f"{mon.hp_pct}% HP"
        if mon.status:
            text += f", {STATUS_WORDS.get(mon.status, mon.status)}"
        return text

    def render_request(self, request: dict, error: str | None = None) -> str:
        """The full message an LLM subject reads for one request (error line first when given)."""
        if request is not self.last_request:
            self.update_request(request)
        lines: list[str] = []
        if error:
            lines.append(f"Error: {error}")
        lines.append(f"Battle {self.battle_no}, turn {self.turn}.")
        events = self._events_for_render()
        if events:
            lines.append("What happened:")
            lines.extend(f"- {e}" for e in events)

        legal = self.legal_choices(request)
        active_list = request.get("active") or []
        active_req = active_list[0] if active_list and isinstance(active_list[0], dict) else None
        active_mon = next((m for m in self.own if m.active), None)
        if active_mon is not None:
            head = f"Your active Pokémon: {active_mon.name}, {self._mon_state(active_mon)}"
            if legal["trapped"]:
                head += " (trapped: cannot switch)"
            lines.append(head + ".")
            if active_req is not None:
                parts: list[str] = []
                for m in active_req.get("moves") or []:
                    text = str(m.get("move") or m.get("id") or "?")
                    if m.get("pp") is not None and m.get("maxpp") is not None:
                        text += f" (PP {m['pp']}/{m['maxpp']})"
                    if m.get("disabled"):
                        text += " (disabled)"
                    parts.append(text)
                lines.append("Moves: " + (", ".join(parts) if parts else "none") + ".")
        bench = [m for m in self.own if not m.active]
        if bench:
            lines.append("Your bench: " + ", ".join(f"{m.name} ({self._mon_state(m)})" for m in bench) + ".")
        else:
            lines.append("Your bench: none.")

        if self.opp_active is not None:
            known_moves = f" Known moves: {', '.join(self.opp_active.moves)}." if self.opp_active.moves else ""
            level_text = f" (level {self.opp_active.level})" if self.opp_active.level != 100 else ""
            lines.append(f"Opponent's active Pokémon: {self.opp_active.species}{level_text}, {self._opp_state(self.opp_active)}.{known_moves}")
        known = list(self.opp_seen.values())
        fainted_count = sum(1 for m in known if m.fainted)
        total = self.opp_team_size or len(known)
        remaining = max(0, total - fainted_count)
        if known:
            names = ", ".join(
                f"{m.species} ({self._opp_state(m)}{', active' if m is self.opp_active and not m.fainted else ''})"
                for m in known
            )
            lines.append(f"Opponent has {remaining} of {total} Pokémon left. Known: {names}.")
        else:
            lines.append(f"Opponent has {remaining} of {total} Pokémon left.")

        if legal["force_switch"]:
            options = ", ".join(legal["switches"]) if legal["switches"] else "none"
            if active_mon is not None and not active_mon.fainted:
                lines.append(f"You must switch out. Switch to: {options}.")
            else:
                lines.append(f"Your active Pokémon fainted. You must switch to: {options}.")
        else:
            lines.append("Choose an action with choose_action.")
        return "\n".join(lines)

    def render_end(self, forced_tie: bool = False) -> str:
        """The final message: events since the last decision and the result."""
        lines = [f"Battle {self.battle_no}, turn {self.turn}."]
        events = self._events_for_render()
        if events:
            lines.append("What happened:")
            lines.extend(f"- {e}" for e in events)
        if self.result == "win":
            outcome = "you won"
        elif self.result == "loss":
            outcome = "you lost"
        elif forced_tie:
            outcome = "it ended in a tie (turn limit reached)"
        else:
            outcome = "it ended in a tie"
        lines.append(f"The battle is over: {outcome}.")
        return "\n".join(lines)

    # ---- choices ---------------------------------------------------------------------

    def legal_choices(self, request: dict) -> dict:
        """Legal options for a request: non-disabled move names, switchable bench names, flags.

        ``switches`` is empty when the active Pokémon is trapped (``trapped`` says why) and
        ``moves`` is empty on a forced switch; both are empty for wait/team-preview requests.
        """
        pokemon = (request.get("side") or {}).get("pokemon") or []
        force_list = request.get("forceSwitch") or []
        force_switch = bool(force_list) and any(bool(x) for x in force_list)
        active_list = request.get("active") or []
        active_req = active_list[0] if active_list and isinstance(active_list[0], dict) else None
        trapped = bool(active_req and active_req.get("trapped")) and not force_switch
        moves: list[str] = []
        if active_req is not None and not force_switch:
            moves = [str(m.get("move") or m.get("id")) for m in active_req.get("moves") or [] if not m.get("disabled")]
        switches: list[str] = []
        if (active_req is not None or force_switch) and not trapped:
            switches = [parse_ident(p.get("ident", ""))[1] for p in pokemon if not p.get("active") and not _is_fainted(p)]
        return {"moves": moves, "switches": switches, "force_switch": force_switch, "trapped": trapped}

    def choice_for(self, request: dict, kind: str, name: str) -> tuple[str | None, str | None]:
        """Map a subject's (kind, name) to "move N" / "switch N", or (None, why it is illegal)."""
        legal = self.legal_choices(request)
        pokemon = (request.get("side") or {}).get("pokemon") or []
        active_list = request.get("active") or []
        active_req = active_list[0] if active_list and isinstance(active_list[0], dict) else None
        kind_id = to_id(kind)
        wanted = to_id(name)
        if kind_id == "move":
            if legal["force_switch"]:
                options = ", ".join(legal["switches"]) or "none"
                return None, f"You cannot use a move now: you must switch. Switch to one of: {options}."
            if active_req is None:
                return None, "No move can be chosen right now."
            req_moves = active_req.get("moves") or []
            for i, m in enumerate(req_moves, 1):
                label = str(m.get("move") or m.get("id") or "")
                if wanted and wanted in (to_id(label), to_id(m.get("id", ""))):
                    if m.get("disabled"):
                        return None, f"{label} is disabled and cannot be used this turn. Available moves: {', '.join(legal['moves']) or 'none'}."
                    return f"move {i}", None
            return None, f"Your active Pokémon does not have a move named '{name}'. Available moves: {', '.join(legal['moves']) or 'none'}."
        if kind_id == "switch":
            if active_req is None and not legal["force_switch"]:
                return None, "No switch can be chosen right now."
            for i, p in enumerate(pokemon, 1):
                pname = parse_ident(p.get("ident", ""))[1]
                species, _ = parse_details(p.get("details", ""))
                if wanted and wanted in (to_id(pname), to_id(species)):
                    if p.get("active"):
                        return None, f"{pname} is already your active Pokémon."
                    if _is_fainted(p):
                        return None, f"{pname} has fainted and cannot battle."
                    if legal["trapped"]:
                        return None, "Your active Pokémon is trapped and cannot switch out."
                    return f"switch {i}", None
            bench = [parse_ident(p.get("ident", ""))[1] for p in pokemon if not p.get("active")]
            return None, f"You have no Pokémon named '{name}'. Your bench: {', '.join(bench) or 'none'}."
        return None, f"Unknown action kind '{kind}'. Use 'move' or 'switch'."


# --------------------------------------------------------------------------------------
# run_battle
# --------------------------------------------------------------------------------------


def _team_order(n: int) -> str:
    """"123" for n == 3: keep Team Preview order (commas only beyond 9 slots)."""
    if n > 9:
        return ", ".join(str(i) for i in range(1, n + 1))
    return "".join(str(i) for i in range(1, n + 1))


class _BattleRunner:
    """State of one running battle: subprocess, log files, views, agents, retry counters."""

    def __init__(
        self,
        p1_packed: str,
        p2_packed: str,
        p1_agent: Agent,
        p2_agent: Agent,
        seed: list[int],
        log_path: str,
        input_path: str,
        battle_no: int,
        p1_name: str,
        p2_name: str,
        format_id: str,
        turn_cap: int,
        invalid_choice_cap: int,
        on_p1_message: Callable[[str, str], None] | None,
    ) -> None:
        self.teams = {"p1": p1_packed, "p2": p2_packed}
        self.agents: dict[str, Agent] = {"p1": p1_agent, "p2": p2_agent}
        self.names = {"p1": p1_name, "p2": p2_name}
        self.seed = [int(x) for x in seed]
        self.log_path = log_path
        self.input_path = input_path
        self.battle_no = battle_no
        self.format_id = format_id
        self.turn_cap = turn_cap
        self.invalid_choice_cap = invalid_choice_cap
        self.on_p1_message = on_p1_message
        self.views = {"p1": PlayerView("p1", battle_no), "p2": PlayerView("p2", battle_no)}
        self.turn = 0
        self.faints: list[FaintEvent] = []
        self.winner_name: str | None = None
        self.ended = False  # |win| / |tie| seen, or >forcetie sent: stop answering requests
        self.forced_tie = False
        self.forced_defaults: list[dict] = []
        self.attempts: dict[str, int] = {"p1": 0, "p2": 0}
        self.last_request: dict[str, dict] = {}
        self.pending_error: dict[str, str] = {}
        self.end_json: dict | None = None
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue = queue.Queue()
        self.stderr_tail: list[str] = []
        self.log_file = None
        self.input_file = None

    # ---- subprocess plumbing ---------------------------------------------------------

    def _start(self) -> None:
        for path in (self.log_path, self.input_path):
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
        self.log_file = open(self.log_path, "w", encoding="utf-8")
        self.input_file = open(self.input_path, "w", encoding="utf-8")
        try:
            self.proc = subprocess.Popen(
                [NODE_PATH, SIM_SCRIPT],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                cwd=os.path.dirname(os.path.abspath(SIM_SCRIPT)) or None,
            )
        except OSError as exc:
            raise SimError(f"could not start the simulator ({NODE_PATH} {SIM_SCRIPT}): {exc}") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for line in self.proc.stderr:
            self.stderr_tail.append(line)
            if len(self.stderr_tail) > 200:
                del self.stderr_tail[:100]

    def _stderr_text(self) -> str:
        return "".join(self.stderr_tail)[-2000:]

    def _send(self, line: str) -> None:
        assert self.proc is not None and self.proc.stdin is not None and self.input_file is not None
        self.input_file.write(line + "\n")
        self.input_file.flush()
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise SimError(f"simulator closed its input unexpectedly; stderr tail: {self._stderr_text()}") from exc

    def _next_line(self) -> str | None:
        try:
            line = self.lines.get(timeout=SIM_READ_TIMEOUT)
        except queue.Empty as exc:
            raise SimError(f"no simulator output for {SIM_READ_TIMEOUT:.0f} s; stderr tail: {self._stderr_text()}") from exc
        if line is None:
            return None
        assert self.log_file is not None
        self.log_file.write(line if line.endswith("\n") else line + "\n")
        self.log_file.flush()
        return line.rstrip("\n")

    def _read_block(self) -> tuple[str, list[str]] | None:
        """Next (block type, lines) or None at EOF. A block starting with a '|' line is an update."""
        block: list[str] = []
        while True:
            line = self._next_line()
            if line is None:
                if not block:
                    return None
                break
            if line == "":
                if block:
                    break
                continue
            block.append(line)
        if block[0].startswith("|"):
            return "update", block
        return block[0], block[1:]

    def _close(self) -> None:
        proc = self.proc
        if proc is not None:
            try:
                if proc.stdin is not None and not proc.stdin.closed:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        for fh in (self.log_file, self.input_file):
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass

    # ---- protocol handling -----------------------------------------------------------

    def _handle_update(self, lines: list[str]) -> None:
        for line in lines:
            if line.startswith("|turn|"):
                self.turn = _int(line.split("|")[2])
                if self.turn >= self.turn_cap and not self.ended:
                    self.forced_tie = True
                    self.ended = True
                    self._send(">forcetie")
            elif line.startswith("|faint|"):
                side, name = parse_ident(line[len("|faint|") :])
                self.faints.append(FaintEvent(turn=self.turn, side=side, name=name))
            elif line.startswith("|win|"):
                self.winner_name = line[len("|win|") :]
                self.ended = True
            elif line == "|tie" or line.startswith("|tie|"):
                self.ended = True
        for side, view in self.views.items():
            view.observe(filter_for_side(lines, side))

    def _handle_sideupdate(self, lines: list[str]) -> None:
        if not lines:
            return
        side = lines[0].strip()
        if side not in self.views:
            return
        for line in lines[1:]:
            if line.startswith("|request|"):
                try:
                    request = json.loads(line[len("|request|") :])
                except ValueError as exc:
                    raise SimError(f"unreadable request for {side}: {line[:200]}") from exc
                self._handle_request(side, request, self.pending_error.pop(side, None))
            elif line.startswith("|error|"):
                self._handle_error(side, line[len("|error|") :])

    def _handle_request(self, side: str, request: dict, error: str | None) -> None:
        view = self.views[side]
        view.update_request(request)
        if request.get("wait"):
            return
        if request.get("teamPreview"):
            n = len((request.get("side") or {}).get("pokemon") or [])
            self._send(f">{side} team {_team_order(n)}")
            return
        if self.ended:
            return
        if error is None:
            self.attempts[side] = 0
        self.last_request[side] = request
        self._ask(side, request, error)

    def _handle_error(self, side: str, text: str) -> None:
        if self.ended:
            return
        if text.startswith("[Unavailable choice]"):
            # A fresh |request| follows in its own sideupdate; answer that one.
            self.pending_error[side] = text
            return
        request = self.last_request.get(side)
        if request is None:
            raise SimError(f"choice error for {side} with no outstanding request: {text}")
        self._ask(side, request, text)

    def _ask(self, side: str, request: dict, error: str | None) -> None:
        view = self.views[side]
        self.attempts[side] += 1
        if self.attempts[side] > self.invalid_choice_cap:
            self.forced_defaults.append(
                {"side": side, "turn": self.turn, "attempts": self.invalid_choice_cap, "last_error": error}
            )
            self._send(f">{side} default")
            return
        if side == "p1" and self.on_p1_message is not None:
            self.on_p1_message("request", view.render_request(request, error))
        choice = self.agents[side].choose(request, view, error)
        choice = str(choice or "").strip() or "default"
        self._send(f">{side} {choice}")

    # ---- main loop -------------------------------------------------------------------

    def run(self) -> BattleResult:
        self._start()
        try:
            self._send(">start " + json.dumps({"formatid": self.format_id, "seed": self.seed}))
            self._send(">player p1 " + json.dumps({"name": self.names["p1"], "team": self.teams["p1"]}))
            self._send(">player p2 " + json.dumps({"name": self.names["p2"], "team": self.teams["p2"]}))
            while True:
                block = self._read_block()
                if block is None:
                    rc = self.proc.poll() if self.proc is not None else None
                    raise SimError(
                        f"simulator ended (exit code {rc}) before the battle finished; stderr tail: {self._stderr_text()}"
                    )
                kind, lines = block
                if kind == "update":
                    self._handle_update(lines)
                elif kind == "sideupdate":
                    self._handle_sideupdate(lines)
                elif kind == "end":
                    try:
                        self.end_json = json.loads(lines[0]) if lines else {}
                    except ValueError as exc:
                        raise SimError(f"unreadable end block: {lines[0][:200] if lines else ''}") from exc
                    break
                # any other block type is ignored (it is still in the raw log)
        finally:
            self._close()
        if self.on_p1_message is not None:
            self.on_p1_message("end", self.views["p1"].render_end(self.forced_tie))
        return self._result()

    def _result(self) -> BattleResult:
        end_json = dict(self.end_json or {})
        end_json["forced_defaults"] = list(self.forced_defaults)
        winner = self._resolve_winner(end_json)
        turns = int(end_json.get("turns", self.turn) or self.turn)
        return BattleResult(
            winner=winner,
            turns=turns,
            faints=list(self.faints),
            end_json=end_json,
            log_path=self.log_path,
            input_path=self.input_path,
            forced_tie=self.forced_tie,
            p1_fainted=[f.name for f in self.faints if f.side == "p1"],
            p2_fainted=[f.name for f in self.faints if f.side == "p2"],
        )

    def _resolve_winner(self, end_json: dict) -> str | None:
        name = end_json.get("winner") or self.winner_name
        if not name:
            return None
        matches = [side for side in ("p1", "p2") if self.names[side] == name]
        if len(matches) == 1:
            return matches[0]
        score = end_json.get("score")
        if isinstance(score, list) and len(score) >= 2:
            alive = [side for side, left in zip(("p1", "p2"), score[:2]) if _int(str(left)) > 0]
            if len(alive) == 1:
                return alive[0]
        return None


def run_battle(
    p1_packed: str,
    p2_packed: str,
    p1_agent: Agent,
    p2_agent: Agent,
    seed: list[int],
    log_path: str,
    input_path: str,
    battle_no: int = 1,
    p1_name: str = "Subject",
    p2_name: str = "Opponent",
    format_id: str = "gen9customgame",
    turn_cap: int = 100,
    invalid_choice_cap: int = 5,
    on_p1_message=None,
) -> BattleResult:
    """Play one seeded battle between two packed teams and return its result.

    Every stdin line is written to ``input_path`` and every stdout line to ``log_path``
    (both files are created fresh). Team Preview is answered automatically with the
    given order; ``wait`` requests need nothing; every other request goes to the side's
    agent after the side's view was refreshed. An ``[Invalid choice]`` error re-asks the
    same agent with the error text, up to ``invalid_choice_cap`` calls per decision, then
    ``default`` is sent and recorded in ``end_json["forced_defaults"]``; an
    ``[Unavailable choice]`` error is answered from the new request that follows it. When
    the turn counter reaches ``turn_cap`` the battle is force-tied (``forced_tie``).
    ``on_p1_message(kind, text)`` receives ("request", rendered) whenever p1 is asked and
    ("end", rendered) once at the end. A simulator crash or hang raises :class:`SimError`.
    """
    runner = _BattleRunner(
        p1_packed=p1_packed,
        p2_packed=p2_packed,
        p1_agent=p1_agent,
        p2_agent=p2_agent,
        seed=seed,
        log_path=log_path,
        input_path=input_path,
        battle_no=battle_no,
        p1_name=p1_name,
        p2_name=p2_name,
        format_id=format_id,
        turn_cap=turn_cap,
        invalid_choice_cap=invalid_choice_cap,
        on_p1_message=on_p1_message,
    )
    return runner.run()
