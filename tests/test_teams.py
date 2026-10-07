"""Tests for harness.teams: legality, the revival ban, packing, levels, roster lookups, hashing."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness import teams  # noqa: E402
from harness.teams import Levels, RosterMon  # noqa: E402

# Every species in use exists in the Scarlet/Violet games (nonstandard None in the pinned package); the
# four "Past" species of the first draft were swapped out so the strict check below holds for all of them.
PAST_SPECIES: set[str] = set()

ILLEGAL_MARKERS = ("can't learn", "doesn't have", "illegal")

PAWMOT_SET = "\n".join([
    "Pawmot @ Leftovers", "Ability: Volt Absorb", "EVs: 252 Atk / 4 SpD / 252 Spe", "Jolly Nature",
    "- Double Shock", "- Close Combat", "- Ice Punch", "- Play Rough",
])
REVIVAL_SET = "\n".join([
    "Gardevoir @ Leftovers", "Ability: Synchronize", "EVs: 252 SpA / 4 SpD / 252 Spe", "Timid Nature",
    "- Moonblast", "- Psychic", "- Shadow Ball", "- Revival Blessing",
])


def _set_ids() -> list[str]:
    return [teams.set_species(text) for text in teams.all_sets()]


def _packed_species(packed: str) -> list[str]:
    """Species of each entry of a packed team: the NICKNAME field equals the species when no nickname is set."""
    return [entry.split("|")[0] for entry in packed.split("]")]


# --- structure -----------------------------------------------------------------------------------------


def test_roster_matches_design() -> None:
    assert teams.roster_names() == [
        "Garchomp", "Dragonite", "Luxray", "Floatzel", "Conkeldurr", "Gardevoir", "Venusaur", "Donphan", "Ceruledge", "Cetitan",
    ]
    assert [mon.name for mon in teams.ROSTER if mon.ace] == ["Garchomp", "Dragonite"]
    assert all(isinstance(mon, RosterMon) and mon.name == mon.species for mon in teams.ROSTER)
    assert len(teams.all_sets()) == 25


def test_opponent_species_match_design() -> None:
    expected = [
        ["Mightyena", "Furret", "Squawkabilly"],
        ["Golduck", "Arbok", "Dodrio"],
        ["Weavile", "Kingdra", "Breloom"],
        ["Tyranitar", "Metagross", "Volcarona"],
        ["Tyranitar", "Metagross", "Volcarona"],
    ]
    assert [teams.opponent_species(n) for n in range(1, 6)] == expected
    for bad in (0, 6):
        with pytest.raises(ValueError):
            teams.opponent_species(bad)


def test_every_set_is_all_attacking_with_sensible_items() -> None:
    moves = teams.dex_data()["moves"]
    for text in teams.all_sets():
        names = teams.set_moves(text)
        assert len(names) == 4, text
        assert len(set(names)) == 4, text
        for move in names:
            entry = moves[teams.to_id(move)]
            assert entry["category"] in ("Physical", "Special"), f"{move} on {teams.set_species(text)}"
            assert entry["basePower"] > 0, f"{move} on {teams.set_species(text)}"
        assert not teams.set_item(text).startswith("Choice"), text
        assert "Level:" not in text
        assert "EVs: " in text and "Nature" in text and "Ability: " in text


def test_every_species_exists_in_dex_and_is_standard_or_past() -> None:
    species = teams.dex_data()["species"]
    for text in teams.all_sets():
        sid = teams.to_id(teams.set_species(text))
        assert sid in species, sid
        nonstandard = species[sid]["nonstandard"]
        if sid in PAST_SPECIES:
            assert nonstandard == "Past", sid
        else:
            assert nonstandard is None, (sid, nonstandard)


def test_aces_have_the_two_highest_bst() -> None:
    totals = {mon.name: teams.bst(mon.species) for mon in teams.ROSTER}
    ace_bst = {totals[mon.name] for mon in teams.ROSTER if mon.ace}
    avg_bst = {totals[mon.name] for mon in teams.ROSTER if not mon.ace}
    assert min(ace_bst) > max(avg_bst)
    ranked = sorted(totals, key=lambda name: totals[name], reverse=True)
    assert set(ranked[:2]) == {"Garchomp", "Dragonite"}


# --- legality (one node call per set) ------------------------------------------------------------------


@pytest.mark.parametrize("set_text", teams.all_sets(), ids=_set_ids())
def test_set_is_learnset_legal(set_text: str) -> None:
    format_id, problems = teams.check_learnset(set_text)
    assert format_id in (
        teams.LEARNSET_FORMAT_STANDARD, teams.LEARNSET_FORMAT_UBERS, teams.LEARNSET_FORMAT_PAST,
    )
    if teams.to_id(teams.set_species(set_text)) in PAST_SPECIES:
        assert format_id == teams.LEARNSET_FORMAT_PAST
    assert problems == [], problems
    assert not any(marker in p.lower() for p in problems for marker in ILLEGAL_MARKERS)


def test_learnset_check_catches_an_illegal_move() -> None:
    bad = teams.export_set("Garchomp", "Life Orb", "Rough Skin", teams.ATK_SPE, "Jolly",
                           ("Earthquake", "Dragon Claw", "Stone Edge", "Thunderbolt"))
    _format_id, problems = teams.check_learnset(bad)
    assert any("can't learn" in p for p in problems), problems


def test_all_sets_validate_as_one_customgame_team() -> None:
    """The runtime format accepts every set: the 10 roster sets as one team and the 15 opponent sets as another
    (gen9customgame allows at most 24 Pokémon per team, so the 25 sets are checked in two teams)."""
    roster_sets = [m.set_text for m in teams.ROSTER]
    opp_sets = [s for t in teams.OPPONENT_TEAMS for s in t]
    assert teams.validate(roster_sets, teams.RUNTIME_FORMAT) == []
    assert teams.validate(opp_sets, teams.RUNTIME_FORMAT) == []


def test_assert_no_revival_passes_on_all_sets() -> None:
    teams.assert_no_revival(teams.all_sets())


def test_assert_no_revival_rejects_a_learner_species() -> None:
    with pytest.raises(ValueError, match="Pawmot"):
        teams.assert_no_revival(teams.all_sets() + [PAWMOT_SET])


def test_assert_no_revival_rejects_the_move() -> None:
    with pytest.raises(ValueError, match="Revival Blessing"):
        teams.assert_no_revival([REVIVAL_SET])


# --- packing and levels ---------------------------------------------------------------------------------


def test_set_with_level_inserts_the_line() -> None:
    text = teams.ROSTER[0].set_text
    levelled = teams.set_with_level(text, 85)
    lines = levelled.splitlines()
    assert lines[0] == text.splitlines()[0]
    assert lines[1] == "Level: 85"
    assert lines[2:] == text.splitlines()[1:]
    assert teams.set_with_level(text, 100) == text
    assert teams.set_with_level(levelled, 70).splitlines()[1] == "Level: 70"
    assert teams.unpack_json([levelled])[0]["level"] == 85
    for bad in (0, 101, -5):
        with pytest.raises(ValueError):
            teams.set_with_level(text, bad)


def test_pack_gives_three_pokemon_for_subject_and_opponent_teams() -> None:
    packed = teams.subject_team(["Garchomp", "Luxray", "Donphan"])
    assert _packed_species(packed) == ["Garchomp", "Luxray", "Donphan"]
    for battle_no in range(1, 6):
        opp = teams.opponent_team(battle_no)
        assert len(_packed_species(opp)) == 3
        assert _packed_species(opp) == teams.opponent_species(battle_no)


def test_subject_team_preserves_order_and_applies_levels() -> None:
    levels = Levels(ace=90, avg=95, opp=(60, 70, 80, 90, 100))
    packed = teams.subject_team(["donphan", "GARCHOMP", "Gar devoir"], levels)
    assert _packed_species(packed) == ["Donphan", "Garchomp", "Gardevoir"]
    entries = packed.split("]")
    assert [entry.split("|")[10] for entry in entries] == ["95", "90", "95"]
    opp = teams.opponent_team(2, levels)
    assert [entry.split("|")[10] for entry in opp.split("]")] == ["70", "70", "70"]
    assert [entry.split("|")[10] for entry in teams.opponent_team(5, levels).split("]")] == ["", "", ""]


def test_subject_team_rejects_bad_selections() -> None:
    with pytest.raises(ValueError):
        teams.subject_team(["Garchomp", "Luxray"])
    with pytest.raises(ValueError):
        teams.subject_team(["Garchomp", "Luxray", "Mewtwo"])
    with pytest.raises(ValueError):
        teams.subject_team(["Garchomp", "Luxray", "garchomp"])


def test_levels_are_validated() -> None:
    with pytest.raises(ValueError):
        Levels(opp=(100, 100, 100, 100))
    with pytest.raises(ValueError):
        Levels(ace=0)
    with pytest.raises(ValueError):
        Levels(opp=(100, 100, 100, 100, 101))
    assert Levels().opp == (100, 100, 100, 100, 100)


def test_pack_and_unpack_round_trip() -> None:
    sets = [teams.ROSTER[0].set_text, teams.OPPONENT_TEAMS[0][0]]
    parsed = teams.unpack_json(sets)
    assert [mon["species"] for mon in parsed] == ["Garchomp", "Mightyena"]
    assert parsed[0]["moves"] == ["Earthquake", "Dragon Claw", "Stone Edge", "Fire Fang"]
    assert parsed[0]["evs"] == {"hp": 0, "atk": 252, "def": 0, "spa": 0, "spd": 4, "spe": 252}
    assert isinstance(teams.pack(sets), str) and teams.pack(sets).count("]") == 1


# --- roster lookups and hashing ---------------------------------------------------------------------------


def test_roster_lookup_is_case_and_space_insensitive() -> None:
    assert teams.roster_mon("gar chomp") is teams.ROSTER[0]
    assert teams.roster_mon("GARCHOMP") is teams.ROSTER[0]
    assert teams.roster_mon("Mewtwo") is None
    assert teams.is_ace("dragonite") is True
    assert teams.is_ace("Luxray") is False
    with pytest.raises(ValueError):
        teams.is_ace("Mewtwo")


def test_roster_hash_changes_with_levels() -> None:
    base = teams.roster_hash()
    assert len(base) == 12 and int(base, 16) >= 0
    assert teams.roster_hash() == base
    assert teams.roster_hash(Levels(ace=90)) != base
    assert teams.roster_hash(Levels(opp=(100, 100, 100, 100, 99))) != base
    assert teams.roster_hash(Levels(ace=90)) != teams.roster_hash(Levels(avg=90))
