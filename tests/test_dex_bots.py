"""Tests for harness.dex and harness.bots: no simulator process and no harness.showdown import."""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness import bots  # noqa: E402
from harness.bots import HeuristicPolicy, first_legal, greedy_select  # noqa: E402
from harness.dex import FIXED_DAMAGE_SCORE, Dex, average_hits, damage_score, get_dex, to_id  # noqa: E402


@pytest.fixture(scope="module")
def dex() -> Dex:
    return get_dex()


# --- tiny stand-ins for the PlayerView / OppMon contract (showdown.py is not imported) ---------------------------


class StubOpp:
    def __init__(self, species: str, revealed=None, level: int | None = None) -> None:
        self.species = species
        self.hp_pct = 100.0
        self.status = None
        self.fainted = False
        self.revealed = list(revealed) if revealed is not None else []
        if level is not None:
            self.level = level


class StubView:
    def __init__(self, opp_species: str | None = None, opp_seen: dict | None = None, revealed=None) -> None:
        self.own = []
        self.opp_active = StubOpp(opp_species, revealed) if opp_species else None
        self.opp_seen = opp_seen if opp_seen is not None else {}
        self.turn = 1

    def legal_choices(self, request: dict) -> dict:
        return {"moves": [], "switches": [], "force_switch": False, "trapped": False}


def mon(dex: Dex, species: str, moves: list[str], active: bool = False, fainted: bool = False,
        level: int = 100, ability: str | None = None) -> dict:
    """A side.pokemon entry shaped like the simulator's request JSON."""
    est = dex.estimate_stats(species, level)
    entry = dex.species(species)
    ability_name = ability or entry["abilities"]["0"]
    return {
        "ident": f"p1: {species}",
        "details": species if level == 100 else f"{species}, L{level}",
        "condition": "0 fnt" if fainted else f"{est['hp']}/{est['hp']}",
        "active": active,
        "stats": {key: est[key] for key in ("atk", "def", "spa", "spd", "spe")},
        "moves": [to_id(move) for move in moves],
        "baseAbility": to_id(ability_name),
        "item": "leftovers",
        "pokeball": "pokeball",
        "ability": to_id(ability_name),
        "commanding": False,
        "reviving": False,
        "teraType": entry["types"][0],
        "terastallized": "",
    }


def side(team: list[dict]) -> dict:
    return {"name": "Subject", "id": "p1", "pokemon": team}


def move_request(dex: Dex, team: list[dict], disabled: tuple[str, ...] = (), trapped: bool = False) -> dict:
    active = next(entry for entry in team if entry["active"])
    moves = []
    for move_id in active["moves"]:
        moves.append({"move": dex.move(move_id)["name"], "id": move_id, "pp": 10, "maxpp": 10,
                      "target": "normal", "disabled": move_id in disabled})
    active_data: dict = {"moves": moves, "canTerastallize": dex.species(active["details"].split(",")[0])["types"][0]}
    if trapped:
        active_data["trapped"] = True
    return {"active": [active_data], "side": side(team)}


def force_switch_request(team: list[dict]) -> dict:
    return {"forceSwitch": [True], "side": side(team)}


def team_preview_request(team: list[dict]) -> dict:
    return {"teamPreview": True, "side": side(team)}


def garchomp_team(dex: Dex, bench_a: str = "Floatzel", bench_b: str = "Luxray") -> list[dict]:
    return [
        mon(dex, "Garchomp", ["earthquake", "dragonclaw", "stoneedge", "firefang"], active=True),
        mon(dex, bench_a, ["surf", "icebeam"]),
        mon(dex, bench_b, ["thunderbolt", "crunch"]),
    ]


# --- dex -----------------------------------------------------------------------------------------------------------


def test_to_id() -> None:
    assert to_id("Dragon Claw") == "dragonclaw"
    assert to_id("Hidden Power [Fire]") == "hiddenpowerfire"
    assert to_id("Farfetch'd") == "farfetchd"
    assert to_id("  Mr. Mime ") == "mrmime"


def test_species_and_move_lookup(dex: Dex) -> None:
    garchomp = dex.species("Garchomp")
    assert garchomp is not None and garchomp["id"] == "garchomp" and garchomp["name"] == "Garchomp"
    assert garchomp["types"] == ["Dragon", "Ground"]
    assert dex.species("garchomp") == garchomp
    assert dex.species("Arceus-*")["id"] == "arceus"
    assert dex.species("Not A Mon") is None
    earthquake = dex.move("Earthquake")
    assert earthquake is not None and earthquake["id"] == "earthquake" and earthquake["basePower"] == 100
    assert dex.move("earthquake") == earthquake
    assert dex.move("no such move") is None
    garchomp["types"].append("Fairy")  # copies: the cache is untouched
    assert dex.species("Garchomp")["types"] == ["Dragon", "Ground"]
    assert get_dex() is get_dex()


def test_effectiveness_spot_checks(dex: Dex) -> None:
    assert dex.effectiveness("Electric", ["Water", "Flying"]) == 4.0
    assert dex.effectiveness("Ground", ["Flying"]) == 0.0
    assert dex.effectiveness("Normal", ["Ghost"]) == 0.0
    assert dex.effectiveness("Fire", ["Water"]) == 0.5
    assert dex.effectiveness("Water", ["Fire"]) == 2.0
    assert dex.effectiveness("Ground", ["Steel", "Rock"]) == 4.0
    assert dex.effectiveness("Fire", ["Steel", "Rock"]) == 1.0
    assert dex.effectiveness("electric", ["water", "flying"]) == 4.0  # ids are accepted too
    assert dex.effectiveness("Dragon", []) == 1.0
    assert dex.effectiveness("???", ["Water"]) == 1.0


def test_estimate_stats_garchomp(dex: Dex) -> None:
    stats = dex.estimate_stats("Garchomp")
    assert stats["hp"] == 357
    assert stats["atk"] == 296
    assert stats == {"hp": 357, "atk": 296, "def": 226, "spa": 196, "spd": 206, "spe": 240}
    assert dex.estimate_stats("Garchomp", 50) == {"hp": 183, "atk": 150, "def": 115, "spa": 100, "spd": 105, "spe": 122}
    assert dex.estimate_stats("Shedinja")["hp"] == 1
    with pytest.raises(KeyError):
        dex.estimate_stats("Not A Mon")


def test_bst_and_revival(dex: Dex) -> None:
    assert dex.bst("Garchomp") == 600
    assert dex.bst("Dragonite") == 600
    assert dex.bst("Luxray") == 523
    with pytest.raises(KeyError):
        dex.bst("Not A Mon")
    assert dex.is_revival_move("revivalblessing")
    assert dex.is_revival_move("Revival Blessing")
    assert not dex.is_revival_move("recover")
    assert dex.revival_learners() == {"rabsca", "pawmot"}


def test_damage_score_prefers_earthquake_vs_steel_rock(dex: Dex) -> None:
    garchomp, aggron = dex.estimate_stats("Garchomp"), dex.estimate_stats("Aggron")
    types = ["Dragon", "Ground"]
    earthquake = damage_score(dex, types, garchomp, "earthquake", ["Steel", "Rock"], aggron)
    fire_fang = damage_score(dex, types, garchomp, "firefang", ["Steel", "Rock"], aggron)
    assert earthquake > fire_fang > 0
    assert earthquake == pytest.approx(100 * 1.0 * 1.5 * 4.0 * (296 / aggron["def"]))
    assert fire_fang == pytest.approx(65 * 0.95 * 1.0 * 1.0 * (296 / aggron["def"]))


def test_damage_score_prefers_dragon_claw_vs_dragon(dex: Dex) -> None:
    garchomp, haxorus = dex.estimate_stats("Garchomp"), dex.estimate_stats("Haxorus")
    types = ["Dragon", "Ground"]
    dragon_claw = damage_score(dex, types, garchomp, "dragonclaw", ["Dragon"], haxorus)
    earthquake = damage_score(dex, types, garchomp, "earthquake", ["Dragon"], haxorus)
    assert dragon_claw > earthquake > 0
    assert dragon_claw == pytest.approx(80 * 1.5 * 2.0 * (296 / haxorus["def"]))


def test_damage_score_edge_cases(dex: Dex) -> None:
    garchomp, target = dex.estimate_stats("Garchomp"), dex.estimate_stats("Bronzong")
    types = ["Dragon", "Ground"]
    assert damage_score(dex, types, garchomp, "swordsdance", ["Steel", "Psychic"], target) == 0.0
    assert damage_score(dex, types, garchomp, "earthquake", ["Steel", "Psychic"], target) > 0
    assert damage_score(dex, types, garchomp, "earthquake", ["Steel", "Psychic"], target, "Levitate") == 0.0
    assert damage_score(dex, types, garchomp, "earthquake", ["Steel", "Psychic"], target, "levitate") == 0.0
    plain = damage_score(dex, types, garchomp, "firefang", ["Steel", "Psychic"], target)
    assert damage_score(dex, types, garchomp, "firefang", ["Steel", "Psychic"], target, "Thick Fat") == pytest.approx(plain / 2)
    assert damage_score(dex, types, garchomp, "no such move", ["Steel", "Psychic"], target) == 0.0
    # fixed damage / OHKO: a low constant, still 0 when immune
    assert damage_score(dex, types, garchomp, "fissure", ["Normal"], target) == FIXED_DAMAGE_SCORE
    assert damage_score(dex, types, garchomp, "fissure", ["Flying"], target) == 0.0
    assert damage_score(dex, types, garchomp, "seismictoss", ["Normal"], target) == FIXED_DAMAGE_SCORE
    assert damage_score(dex, types, garchomp, "seismictoss", ["Ghost"], target) == 0.0
    assert FIXED_DAMAGE_SCORE < damage_score(dex, types, garchomp, "dragonclaw", ["Normal"], target)
    # accuracy True counts as 100%, special moves use spa/spd, multi-hit uses the average hit count
    assert damage_score(dex, types, garchomp, "swift", ["Normal"], target) == pytest.approx(60 * (196 / target["spd"]))
    assert damage_score(dex, types, garchomp, "bulletseed", ["Normal"], target) == pytest.approx(25 * 3.1 * (296 / target["def"]))
    assert average_hits(None) == 1.0 and average_hits(2) == 2.0 and average_hits([2, 5]) == 3.1
    # immunity by ability on the opponent's water move
    assert damage_score(dex, ["Water"], target, "surf", ["Fire"], garchomp, "Water Absorb") == 0.0
    assert damage_score(dex, ["Water"], target, "surf", ["Fire"], garchomp) > 0


# --- greedy selection ---------------------------------------------------------------------------------------------


ROSTER = [
    {"name": "Luxray", "species": "Luxray", "ace": False},
    {"name": "Garchomp", "species": "Garchomp", "ace": True},
    {"name": "Floatzel", "species": "Floatzel", "ace": False},
    {"name": "Machamp", "species": "Machamp", "ace": False},
    {"name": "Dragonite", "species": "Dragonite", "ace": True},
    {"name": "Gardevoir", "species": "Gardevoir", "ace": False},
    {"name": "Roserade", "species": "Roserade", "ace": False},
    {"name": "Donphan", "species": "Donphan", "ace": False},
]


def test_greedy_select_aces_first(dex: Dex) -> None:
    names = {entry["name"] for entry in ROSTER}
    assert greedy_select(ROSTER, names) == ["Dragonite", "Garchomp", "Luxray"]
    assert greedy_select(ROSTER, names, k=3, dex=dex) == ["Dragonite", "Garchomp", "Luxray"]
    # an ace beats a non-ace of equal BST (Archaludon is also 600) and the name breaks the remaining tie
    roster = ROSTER + [{"name": "Archaludon", "species": "Archaludon", "ace": False}]
    assert greedy_select(roster, names | {"Archaludon"}) == ["Dragonite", "Garchomp", "Archaludon"]
    assert greedy_select(roster, names | {"Archaludon"}, k=4) == ["Dragonite", "Garchomp", "Archaludon", "Luxray"]


def test_greedy_select_respects_availability(dex: Dex) -> None:
    survivors = {"Luxray", "Floatzel", "Machamp", "Gardevoir", "Roserade", "Donphan"}
    assert greedy_select(ROSTER, survivors) == ["Luxray", "Gardevoir", "Roserade"]
    assert greedy_select(ROSTER, {"Donphan", "Floatzel"}) == ["Donphan", "Floatzel"]
    assert greedy_select(ROSTER, {"luxray"}) == ["Luxray"]  # names match case-insensitively
    assert greedy_select(ROSTER, set()) == []


# --- heuristic policy -----------------------------------------------------------------------------------------------


def test_policy_team_preview_and_wait(dex: Dex) -> None:
    policy = HeuristicPolicy(dex)
    team = garchomp_team(dex)
    assert policy.choose(team_preview_request(team), StubView()) == "team 123"
    assert policy.choose(team_preview_request(team[:2]), StubView()) == "team 12"
    assert policy.choose({"wait": True, "side": side(team)}, StubView()) == "default"


def test_policy_picks_best_move(dex: Dex) -> None:
    policy = HeuristicPolicy(dex)
    request = move_request(dex, garchomp_team(dex))
    # Tyranitar is Rock/Dark: STAB Earthquake is 2x, Dragon Claw neutral, Stone Edge and Fire Fang resisted
    assert policy.choose(request, StubView("Tyranitar")) == "move 1"
    # Dragonite is Dragon/Flying: Earthquake is immune, Dragon Claw is STAB 2x
    assert policy.choose(request, StubView("Dragonite")) == "move 2"
    # without any view the policy still answers a legal move
    assert policy.choose(request, None) == "move 1"


def test_policy_skips_disabled_move(dex: Dex) -> None:
    policy = HeuristicPolicy(dex)
    request = move_request(dex, garchomp_team(dex), disabled=("earthquake",))
    assert policy.choose(request, StubView("Tyranitar")) == "move 2"
    request = move_request(dex, garchomp_team(dex), disabled=("earthquake", "dragonclaw"))
    assert policy.choose(request, StubView("Tyranitar")) == "move 3"


def test_policy_force_switch_uses_damage_and_threat(dex: Dex) -> None:
    team = [
        mon(dex, "Luxray", ["thunderbolt"], active=True, fainted=True),
        mon(dex, "Empoleon", ["icebeam"]),
        mon(dex, "Dragonite", ["dragonclaw"]),
    ]
    request = force_switch_request(team)
    # Opposing Garchomp with no revealed moves: assumed STAB Dragon/Ground 90s. Empoleon's 4x Ice Beam outweighs the
    # assumed Ground hit, so Empoleon (slot 2) is the better switch.
    assert HeuristicPolicy(dex).choose(request, StubView("Garchomp")) == "switch 2"
    # With Earthquake revealed in opp_seen, Empoleon's threat doubles while Dragonite is immune: slot 3 wins.
    seen = {"Garchomp": {"species": "Garchomp", "moves": ["earthquake"]}}
    assert HeuristicPolicy(dex).choose(request, StubView("Garchomp", opp_seen=seen)) == "switch 3"
    # the same when the revealed moves sit on the OppMon or under an ident key
    assert HeuristicPolicy(dex).choose(request, StubView("Garchomp", revealed=["earthquake"])) == "switch 3"
    seen = {"p2: Garchomp": StubOpp("Garchomp", revealed=["earthquake"])}
    assert HeuristicPolicy(dex).choose(request, StubView("Garchomp", opp_seen=seen)) == "switch 3"


def test_policy_force_switch_ties_to_lowest_slot_and_skips_fainted(dex: Dex) -> None:
    team = [
        mon(dex, "Luxray", ["thunderbolt"], active=True, fainted=True),
        mon(dex, "Floatzel", ["surf"], fainted=True),
        mon(dex, "Machamp", ["closecombat"]),
        mon(dex, "Machamp", ["closecombat"]),
    ]
    assert HeuristicPolicy(dex).choose(force_switch_request(team), StubView("Tyranitar")) == "switch 3"
    everyone_down = [mon(dex, "Luxray", ["thunderbolt"], active=True, fainted=True)]
    assert HeuristicPolicy(dex).choose(force_switch_request(everyone_down), StubView("Tyranitar")) == "default"


def test_policy_switches_when_no_damaging_move(dex: Dex) -> None:
    team = [
        mon(dex, "Toxapex", ["recover", "toxic", "haze"], active=True),
        mon(dex, "Blissey", ["softboiled"]),
        mon(dex, "Raichu", ["thunderbolt"]),
    ]
    policy = HeuristicPolicy(dex)
    assert policy.choose(move_request(dex, team), StubView("Gyarados")) == "switch 3"
    # trapped: the first non-disabled move instead
    assert policy.choose(move_request(dex, team, trapped=True), StubView("Gyarados")) == "move 1"
    # no bench Pokémon with a damaging move: the first non-disabled move
    team[2] = mon(dex, "Raichu", ["nastyplot"])
    assert policy.choose(move_request(dex, team), StubView("Gyarados")) == "move 1"
    assert policy.choose(move_request(dex, team, disabled=("recover",)), StubView("Gyarados")) == "move 2"


def test_policy_error_flips_between_move_and_switch(dex: Dex) -> None:
    policy = HeuristicPolicy(dex)
    request = move_request(dex, garchomp_team(dex))
    view = StubView("Tyranitar")
    assert policy.choose(request, view) == "move 1"
    flipped = policy.choose(request, view, error="[Invalid choice] Can't move: Earthquake is disabled")
    assert flipped.startswith("switch ")
    assert policy.choose(request, view, error="[Invalid choice] Can't switch: You are trapped") == "move 1"
    # a rejected move while trapped: a different move rather than a switch
    policy = HeuristicPolicy(dex)
    trapped = move_request(dex, garchomp_team(dex), trapped=True)
    assert policy.choose(trapped, view) == "move 1"
    assert policy.choose(trapped, view, error="[Invalid choice] Can't move: Earthquake is disabled") == "move 2"
    # a rejected forced switch: the next-best bench slot
    policy = HeuristicPolicy(dex)
    team = [
        mon(dex, "Luxray", ["thunderbolt"], active=True, fainted=True),
        mon(dex, "Empoleon", ["icebeam"]),
        mon(dex, "Dragonite", ["dragonclaw"]),
    ]
    assert policy.choose(force_switch_request(team), StubView("Garchomp")) == "switch 2"
    assert policy.choose(force_switch_request(team), StubView("Garchomp"), error="[Invalid choice] nope") == "switch 3"


def test_policy_never_terastallizes_and_is_deterministic(dex: Dex) -> None:
    team = garchomp_team(dex)
    requests = [team_preview_request(team), move_request(dex, team), force_switch_request(
        [mon(dex, "Garchomp", ["earthquake"], active=True, fainted=True)] + team[1:])]
    answers = []
    for request in requests:
        policy = HeuristicPolicy(dex)
        answers.append(policy.choose(request, StubView("Tyranitar")))
    assert all("tera" not in answer for answer in answers)
    assert answers == [HeuristicPolicy(dex).choose(request, StubView("Tyranitar")) for request in requests]
    assert answers[0] == "team 123" and answers[1].startswith("move ") and answers[2].startswith("switch ")


def test_policy_reads_level_from_details(dex: Dex) -> None:
    assert bots.parse_details("Pidgey, L50, F") == ("Pidgey", 50)
    assert bots.parse_details("Garchomp, M") == ("Garchomp", 100)
    assert bots.parse_details("Sawsbuck, L50, F, shiny") == ("Sawsbuck", 50)
    assert bots.parse_condition("357/357") == (357, 357, None)
    assert bots.parse_condition("120/357 par") == (120, 357, "par")
    assert bots.parse_condition("0 fnt") == (0, None, "fnt")
    combatant = bots.own_combatant(dex, mon(dex, "Pidgey", ["gust"], level=50), slot=2)
    assert combatant.level == 50 and combatant.slot == 2 and combatant.types == ["Normal", "Flying"]
    assert combatant.stats["atk"] == dex.estimate_stats("Pidgey", 50)["atk"]
    opponent = bots.opponent_combatant(dex, StubView("Pidgey"))
    assert opponent.level == 100 and opponent.stats["atk"] == dex.estimate_stats("Pidgey", 100)["atk"]
    view = StubView("Pidgey")
    view.opp_active.level = 50
    assert bots.opponent_combatant(dex, view).stats["atk"] == dex.estimate_stats("Pidgey", 50)["atk"]


# --- first_legal ---------------------------------------------------------------------------------------------------


def test_first_legal(dex: Dex) -> None:
    team = garchomp_team(dex)
    assert first_legal(team_preview_request(team)) == "team 123"
    assert first_legal({"wait": True, "side": side(team)}) == "default"
    assert first_legal(move_request(dex, team)) == "move 1"
    assert first_legal(move_request(dex, team, disabled=("earthquake", "dragonclaw"))) == "move 3"
    assert first_legal(move_request(dex, team, disabled=("earthquake", "dragonclaw", "stoneedge", "firefang"))) == "switch 2"
    assert first_legal(move_request(dex, team, disabled=("earthquake", "dragonclaw", "stoneedge", "firefang"),
                                    trapped=True)) == "default"
    fainted_lead = [mon(dex, "Garchomp", ["earthquake"], active=True, fainted=True),
                    mon(dex, "Floatzel", ["surf"], fainted=True), mon(dex, "Luxray", ["thunderbolt"])]
    assert first_legal(force_switch_request(fainted_lead)) == "switch 3"
    assert first_legal(force_switch_request(fainted_lead[:2])) == "default"


# --- import hygiene -------------------------------------------------------------------------------------------------


def test_bots_imports_without_showdown_module() -> None:
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "sys.modules['harness.showdown'] = None\n"  # any runtime import of it would now raise ImportError
        "import harness.bots\n"
        "import harness.dex\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], cwd=str(ROOT), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
