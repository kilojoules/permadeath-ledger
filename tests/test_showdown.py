"""Tests for harness/showdown.py: real simulator subprocess, fixed 3-mon teams, scripted agents."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.showdown import (  # noqa: E402
    NODE_PATH,
    PlayerView,
    battle_seed,
    filter_for_side,
    render_line,
    run_battle,
)

PS_TOOL = ROOT / "showdown" / "ps_tool.js"

TEAM_A = """Garchomp @ Choice Scarf
Ability: Rough Skin
EVs: 252 Atk / 4 SpD / 252 Spe
Jolly Nature
- Earthquake
- Dragon Claw
- Stone Edge
- Fire Fang

Toxapex @ Leftovers
Ability: Regenerator
EVs: 252 HP / 252 Def / 4 SpD
Bold Nature
- Scald
- Recover
- Toxic
- Haze

Raichu @ Life Orb
Ability: Static
EVs: 252 SpA / 4 SpD / 252 Spe
Timid Nature
- Thunderbolt
- Surf
- Focus Blast
- Nasty Plot
"""

TEAM_B = """Dragonite @ Heavy-Duty Boots
Ability: Multiscale
EVs: 252 Atk / 4 SpD / 252 Spe
Adamant Nature
- Dragon Dance
- Extreme Speed
- Earthquake
- Fire Punch

Gengar @ Choice Specs
Ability: Cursed Body
EVs: 252 SpA / 4 SpD / 252 Spe
Timid Nature
- Shadow Ball
- Sludge Wave
- Focus Blast
- Thunderbolt

Pidgey @ Oran Berry
Ability: Keen Eye
Level: 50
- Tackle
- Gust
"""

SEED = [1, 2, 3, 4]


def pack(export_text: str) -> str:
    r = subprocess.run([NODE_PATH, str(PS_TOOL), "pack"], input=export_text, capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


@pytest.fixture(scope="module")
def teams() -> tuple[str, str]:
    return pack(TEAM_A), pack(TEAM_B)


class FirstLegalAgent:
    """Picks the first legal switch on a forced switch, else the first legal move; records what it saw."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def choose(self, request: dict, view: PlayerView, error: str | None) -> str:
        legal = view.legal_choices(request)
        rendered = view.render_request(request, error)
        self.calls.append({"force": legal["force_switch"], "error": error, "turn": view.turn, "rendered": rendered})
        if legal["force_switch"] and legal["switches"]:
            choice, err = view.choice_for(request, "switch", legal["switches"][0])
            assert err is None, err
            return choice
        if legal["moves"]:
            choice, err = view.choice_for(request, "move", legal["moves"][0])
            assert err is None, err
            return choice
        if legal["switches"]:
            choice, err = view.choice_for(request, "switch", legal["switches"][0])
            assert err is None, err
            return choice
        return "default"


class BadOnceAgent(FirstLegalAgent):
    """Sends one impossible choice on its first decision, then plays like FirstLegalAgent."""

    def __init__(self) -> None:
        super().__init__()
        self.sent_bad = False

    def choose(self, request: dict, view: PlayerView, error: str | None) -> str:
        if not self.sent_bad:
            self.sent_bad = True
            view.render_request(request, error)
            self.calls.append({"force": False, "error": error, "turn": view.turn, "rendered": ""})
            return "move 9"
        return super().choose(request, view, error)


class AlwaysBadAgent:
    """Never sends a legal choice."""

    def __init__(self) -> None:
        self.errors: list[str | None] = []

    def choose(self, request: dict, view: PlayerView, error: str | None) -> str:
        self.errors.append(error)
        return "move 9"


def faint_lines_from_log(log_path: str) -> list[tuple[str, str]]:
    out = []
    for line in Path(log_path).read_text(encoding="utf-8").splitlines():
        if line.startswith("|faint|"):
            ident = line[len("|faint|") :]
            out.append((ident[:2], ident.split(": ", 1)[1]))
    return out


def log_without_timestamps(path: str) -> str:
    return "\n".join(l for l in Path(path).read_text(encoding="utf-8").splitlines() if not l.startswith("|t:|"))


# --------------------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------------------


def test_battle_seed_is_four_16bit_ints_and_deterministic():
    s1 = battle_seed("run-1", 1)
    s2 = battle_seed("run-1", 1)
    s3 = battle_seed("run-1", 2)
    assert s1 == s2
    assert s1 != s3
    assert len(s1) == 4 and all(isinstance(x, int) and 0 <= x <= 65535 for x in s1)


def test_filter_for_side_resolves_split_blocks():
    lines = [
        "|t:|1791396767",
        "|move|p1a: Garchomp|Earthquake|p2a: Dragonite",
        "|split|p2",
        "|-damage|p2a: Dragonite|292/323",
        "|-damage|p2a: Dragonite|90/100",
        "|split|p1",
        "|-damage|p1a: Garchomp|268/357|[from] recoil",
        "|-damage|p1a: Garchomp|75/100|[from] recoil",
        '|request|{"wait":true}',
        "|upkeep",
    ]
    assert filter_for_side(lines, "p1") == [
        "|move|p1a: Garchomp|Earthquake|p2a: Dragonite",
        "|-damage|p2a: Dragonite|90/100",
        "|-damage|p1a: Garchomp|268/357|[from] recoil",
        "|upkeep",
    ]
    assert filter_for_side(lines, "p2") == [
        "|move|p1a: Garchomp|Earthquake|p2a: Dragonite",
        "|-damage|p2a: Dragonite|292/323",
        "|-damage|p1a: Garchomp|75/100|[from] recoil",
        "|upkeep",
    ]
    # a split whose public line is missing (the simulator pushes an empty line there) keeps nothing for the other side
    tail = ["|split|p1", "|-hint|only for p1"]
    assert filter_for_side(tail, "p1") == ["|-hint|only for p1"]
    assert filter_for_side(tail, "p2") == []


def make_view() -> PlayerView:
    view = PlayerView("p1", 1)
    view.observe(
        [
            "|player|p1|Subject||",
            "|player|p2|Opponent||",
            "|teamsize|p1|3",
            "|teamsize|p2|3",
            "|poke|p2|Dragonite, M|",
            "|poke|p2|Gengar, F|",
            "|poke|p2|Pidgey, L50, F|",
            "|start",
            "|switch|p1a: Garchomp|Garchomp, M|357/357",
            "|switch|p2a: Gengar|Gengar, F|261/261",
            "|turn|1",
        ]
    )
    return view


REQUEST = {
    "active": [
        {
            "moves": [
                {"move": "Earthquake", "id": "earthquake", "pp": 16, "maxpp": 16, "target": "allAdjacent", "disabled": False},
                {"move": "Dragon Claw", "id": "dragonclaw", "pp": 24, "maxpp": 24, "target": "normal", "disabled": True},
                {"move": "Stone Edge", "id": "stoneedge", "pp": 8, "maxpp": 8, "target": "normal", "disabled": False},
                {"move": "Fire Fang", "id": "firefang", "pp": 24, "maxpp": 24, "target": "normal", "disabled": False},
            ]
        }
    ],
    "side": {
        "name": "Subject",
        "id": "p1",
        "pokemon": [
            {"ident": "p1: Garchomp", "details": "Garchomp, M", "condition": "213/357 par", "active": True,
             "moves": ["earthquake", "dragonclaw", "stoneedge", "firefang"]},
            {"ident": "p1: Toxapex", "details": "Toxapex, F", "condition": "304/304", "active": False,
             "moves": ["scald", "recover", "toxic", "haze"]},
            {"ident": "p1: Raichu", "details": "Raichu, F", "condition": "0 fnt", "active": False,
             "moves": ["thunderbolt", "surf", "focusblast", "nastyplot"]},
        ],
    },
}


def test_render_line_perspectives():
    view = make_view()
    assert render_line("|move|p1a: Garchomp|Earthquake|p2a: Gengar", "p1", view) == "Garchomp used Earthquake."
    assert render_line("|move|p2a: Gengar|Shadow Ball|p1a: Garchomp", "p1", view) == "Opponent's Gengar used Shadow Ball."
    assert render_line("|-damage|p2a: Gengar|167/261", "p1", view) == "Opponent's Gengar lost 36% HP (64% left)."
    assert render_line("|-damage|p1a: Garchomp|213/357", "p1", view) == "Your Garchomp lost 144 HP (213/357 left)."
    assert render_line("|faint|p1a: Garchomp", "p1", view) == "Garchomp fainted."
    assert render_line("|faint|p2a: Gengar", "p1", view) == "Opponent's Gengar fainted."
    assert render_line("|switch|p2a: Dragonite|Dragonite, M|323/323", "p1", view) == "Opponent sent out Dragonite (100%)."
    assert render_line("|switch|p1a: Toxapex|Toxapex, F|304/304", "p1", view) == "You sent out Toxapex (304/304)."
    assert render_line("|-status|p1a: Garchomp|par", "p1", view) == "Garchomp was paralyzed."
    assert render_line("|-boost|p2a: Gengar|spa|2", "p1", view) == "Opponent's Gengar's Special Attack sharply rose (+2)."
    assert render_line("|-supereffective|p2a: Gengar", "p1", view) == "It was super effective against Opponent's Gengar."
    assert render_line("|-immune|p2a: Gengar", "p1", view) == "It did not affect Opponent's Gengar."
    assert render_line("|cant|p1a: Garchomp|par", "p1", view) == "Garchomp is paralyzed and can't move."
    assert render_line("|win|Subject", "p1", view) == "You won the battle."
    assert render_line("|win|Opponent", "p1", view) == "You lost the battle."
    assert render_line("|upkeep", "p1", view) is None
    assert render_line("|turn|2", "p1", view) is None
    assert render_line("|-weather|Sandstorm|[upkeep]", "p1", view) is None
    # the same lines from p2's perspective flip the ownership
    view2 = PlayerView("p2", 1)
    view2.observe(["|player|p1|Subject||", "|player|p2|Opponent||", "|switch|p2a: Gengar|Gengar, F|261/261"])
    assert render_line("|move|p2a: Gengar|Shadow Ball|p1a: Garchomp", "p2", view2) == "Gengar used Shadow Ball."
    assert render_line("|-damage|p2a: Gengar|167/261", "p2", view2) == "Your Gengar lost 94 HP (167/261 left)."
    assert render_line("|win|Subject", "p2", view2) == "You lost the battle."


def test_observe_tracks_opponent_and_buffers_events():
    view = make_view()
    assert view.turn == 1
    assert view.opp_team_size == 3
    assert set(view.opp_seen) == {"Dragonite", "Gengar", "Pidgey"}
    assert view.opp_active is not None and view.opp_active.species == "Gengar"
    view.observe(["|-damage|p2a: Gengar|167/261 brn", "|faint|p2a: Gengar"])
    assert view.opp_seen["Gengar"].fainted is True
    assert view.opp_seen["Gengar"].hp_pct == 0
    events = view.take_pending()
    assert "You sent out Garchomp (357/357)." in events
    assert "Opponent sent out Gengar (100%)." in events
    assert "Opponent's Gengar lost 36% HP (64% left)." in events
    assert "Opponent's Gengar fainted." in events
    assert view.take_pending() == []


def test_update_request_parses_conditions_and_own_fainted():
    view = make_view()
    view.update_request(REQUEST)
    by_name = {m.name: m for m in view.own}
    assert by_name["Garchomp"].hp == 213 and by_name["Garchomp"].maxhp == 357 and by_name["Garchomp"].status == "par"
    assert by_name["Garchomp"].active is True and by_name["Garchomp"].slot == 1
    assert by_name["Toxapex"].fainted is False and by_name["Toxapex"].moves == ["Scald", "Recover", "Toxic", "Haze"]
    assert by_name["Raichu"].fainted is True and by_name["Raichu"].hp == 0
    assert view.own_fainted() == ["Raichu"]
    assert view.last_request is REQUEST


def test_render_request_contains_state_and_choices():
    view = make_view()
    view.update_request(REQUEST)
    text = view.render_request(REQUEST)
    assert text.startswith("Battle 1, turn 1.")
    assert "What happened:" in text and "- You sent out Garchomp (357/357)." in text
    assert "Your active Pokémon: Garchomp, 213/357 HP, paralyzed." in text
    assert "Earthquake (PP 16/16)" in text
    assert "Dragon Claw (PP 24/24) (disabled)" in text
    assert "Your bench: Toxapex (304/304 HP), Raichu (fainted)." in text
    assert "Opponent's active Pokémon: Gengar, 100% HP." in text
    assert "Opponent has 3 of 3 Pokémon left." in text and "Dragonite" in text and "Pidgey" in text
    assert text.endswith("Choose an action with choose_action.")
    # a re-ask after an error shows the error first and the same events again
    again = view.render_request(REQUEST, error="Dragon Claw is disabled")
    assert again.startswith("Error: Dragon Claw is disabled\nBattle 1, turn 1.")
    assert "- You sent out Garchomp (357/357)." in again


def test_render_request_force_switch():
    view = make_view()
    view.observe(["|-damage|p1a: Garchomp|0 fnt", "|faint|p1a: Garchomp"])
    request = {
        "forceSwitch": [True],
        "side": {
            "name": "Subject",
            "id": "p1",
            "pokemon": [
                {"ident": "p1: Garchomp", "details": "Garchomp, M", "condition": "0 fnt", "active": True, "moves": ["earthquake"]},
                {"ident": "p1: Toxapex", "details": "Toxapex, F", "condition": "304/304", "active": False, "moves": ["scald"]},
                {"ident": "p1: Raichu", "details": "Raichu, F", "condition": "200/261 psn", "active": False, "moves": ["surf"]},
            ],
        },
    }
    view.update_request(request)
    legal = view.legal_choices(request)
    assert legal == {"moves": [], "switches": ["Toxapex", "Raichu"], "force_switch": True, "trapped": False}
    text = view.render_request(request)
    assert "- Garchomp fainted." in text
    assert "Your active Pokémon fainted. You must switch to: Toxapex, Raichu." in text
    assert "Raichu (200/261 HP, poisoned)" in text
    assert view.choice_for(request, "switch", "toxapex") == ("switch 2", None)
    assert view.choice_for(request, "switch", "RAICHU") == ("switch 3", None)
    choice, err = view.choice_for(request, "move", "Earthquake")
    assert choice is None and "must switch" in err


def test_legal_choices_and_choice_for_mapping():
    view = make_view()
    view.update_request(REQUEST)
    legal = view.legal_choices(REQUEST)
    assert legal == {
        "moves": ["Earthquake", "Stone Edge", "Fire Fang"],
        "switches": ["Toxapex"],
        "force_switch": False,
        "trapped": False,
    }
    assert view.choice_for(REQUEST, "move", "earthquake") == ("move 1", None)
    assert view.choice_for(REQUEST, "Move", "stone edge") == ("move 3", None)
    assert view.choice_for(REQUEST, "move", "FIRE-FANG") == ("move 4", None)
    assert view.choice_for(REQUEST, "switch", "toxapex") == ("switch 2", None)
    choice, err = view.choice_for(REQUEST, "move", "Dragon Claw")
    assert choice is None and "disabled" in err
    choice, err = view.choice_for(REQUEST, "switch", "Raichu")
    assert choice is None and "fainted" in err
    choice, err = view.choice_for(REQUEST, "switch", "Garchomp")
    assert choice is None and "already" in err
    choice, err = view.choice_for(REQUEST, "move", "Hyper Beam")
    assert choice is None and "Hyper Beam" in err and "Earthquake" in err
    choice, err = view.choice_for(REQUEST, "switch", "Mewtwo")
    assert choice is None and "Mewtwo" in err
    choice, err = view.choice_for(REQUEST, "attack", "Earthquake")
    assert choice is None and "Unknown action kind" in err
    trapped = json.loads(json.dumps(REQUEST))
    trapped["active"][0]["trapped"] = True
    assert view.legal_choices(trapped)["trapped"] is True
    assert view.legal_choices(trapped)["switches"] == []
    choice, err = view.choice_for(trapped, "switch", "Toxapex")
    assert choice is None and "trapped" in err


# --------------------------------------------------------------------------------------
# Real battles
# --------------------------------------------------------------------------------------


def test_battle_completes_with_faints_winner_and_logs(tmp_path, teams):
    p1, p2 = teams
    a1, a2 = FirstLegalAgent(), FirstLegalAgent()
    messages: list[tuple[str, str]] = []
    log, inp = str(tmp_path / "battle_1.log"), str(tmp_path / "battle_1.in")
    result = run_battle(p1, p2, a1, a2, SEED, log, inp, battle_no=1, on_p1_message=lambda k, t: messages.append((k, t)))

    assert result.winner in ("p1", "p2")
    assert result.forced_tie is False
    assert result.turns >= 1
    assert result.log_path == log and result.input_path == inp
    assert result.end_json["winner"] in ("Subject", "Opponent")
    assert result.end_json["forced_defaults"] == []
    assert result.end_json["seed"] == "1,2,3,4"

    # ground truth: the faint list is exactly the |faint| lines of the raw log, with turns
    assert [(f.side, f.name) for f in result.faints] == faint_lines_from_log(log)
    assert len(result.faints) >= 3
    assert all(f.turn >= 1 for f in result.faints)
    assert all(f.side in ("p1", "p2") for f in result.faints)
    assert result.p1_fainted == [f.name for f in result.faints if f.side == "p1"]
    assert result.p2_fainted == [f.name for f in result.faints if f.side == "p2"]
    loser = "p2" if result.winner == "p1" else "p1"
    assert len(getattr(result, f"{loser}_fainted")) == 3
    assert all(name in ("Garchomp", "Toxapex", "Raichu") for name in result.p1_fainted)
    assert all(name in ("Dragonite", "Gengar", "Pidgey") for name in result.p2_fainted)

    # raw logs: stdin and stdout as written
    inp_text = Path(inp).read_text(encoding="utf-8")
    assert inp_text.startswith('>start {"formatid": "gen9customgame", "seed": [1, 2, 3, 4]}\n')
    assert ">player p1 " in inp_text and ">player p2 " in inp_text
    assert ">p1 team 123\n" in inp_text and ">p2 team 123\n" in inp_text
    assert ">p1 move 1\n" in inp_text
    log_text = Path(log).read_text(encoding="utf-8")
    assert "sideupdate\np1\n|request|" in log_text
    assert "|win|" in log_text and "\nend\n{" in log_text

    # the forced-switch flow ran: the side that lost a Pokémon mid-battle got a forceSwitch request
    forced = [c for c in a1.calls + a2.calls if c["force"]]
    assert forced, "no forceSwitch request was handled"
    assert any("You must switch to:" in c["rendered"] for c in forced)
    assert ">p1 switch " in inp_text or ">p2 switch " in inp_text
    assert all(c["error"] is None for c in a1.calls + a2.calls)

    # what p1 saw
    kinds = [k for k, _ in messages]
    assert kinds.count("end") == 1 and kinds[-1] == "end" and kinds.count("request") == len(a1.calls)
    first = messages[0][1]
    assert first.startswith("Battle 1, turn 1.")
    assert "You sent out Garchomp (357/357)." in first and "Opponent sent out Dragonite (100%)." in first
    assert "Moves: Earthquake (PP 16/16)" in first
    assert "Your bench: Toxapex (304/304 HP), Raichu (261/261 HP)." in first
    # the subject's own rendering and the logged copy are the same text
    assert first == a1.calls[0]["rendered"]
    end_text = messages[-1][1]
    assert end_text.startswith(f"Battle 1, turn {result.turns}.")
    assert ("The battle is over: you won." in end_text) == (result.winner == "p1")
    assert ("The battle is over: you lost." in end_text) == (result.winner == "p2")
    # the end message carries the final events: the last faint of the battle is in it
    last = result.faints[-1]
    last_text = f"- {last.name} fainted." if last.side == "p1" else f"- Opponent's {last.name} fainted."
    assert last_text in end_text
    # earlier faints were shown in the request messages that followed them
    for faint in result.faints[:-1]:
        shown = f"- {faint.name} fainted." if faint.side == "p1" else f"- Opponent's {faint.name} fainted."
        assert any(shown in t for k, t in messages if k == "request"), shown


def test_seed_reproduces_identical_log(tmp_path, teams):
    p1, p2 = teams
    r1 = run_battle(p1, p2, FirstLegalAgent(), FirstLegalAgent(), SEED, str(tmp_path / "a.log"), str(tmp_path / "a.in"))
    r2 = run_battle(p1, p2, FirstLegalAgent(), FirstLegalAgent(), SEED, str(tmp_path / "b.log"), str(tmp_path / "b.in"))
    assert log_without_timestamps(r1.log_path) == log_without_timestamps(r2.log_path)
    assert Path(r1.input_path).read_text() == Path(r2.input_path).read_text()
    assert r1.faints == r2.faints and r1.winner == r2.winner and r1.turns == r2.turns
    r3 = run_battle(p1, p2, FirstLegalAgent(), FirstLegalAgent(), [9, 9, 9, 9], str(tmp_path / "c.log"), str(tmp_path / "c.in"))
    assert r3.end_json["seed"] == "9,9,9,9"


def test_forced_tie_with_turn_cap_2(tmp_path, teams):
    p1, p2 = teams
    a1 = FirstLegalAgent()
    result = run_battle(p1, p2, a1, FirstLegalAgent(), SEED, str(tmp_path / "t.log"), str(tmp_path / "t.in"), turn_cap=2)
    assert result.forced_tie is True
    assert result.winner is None
    assert result.turns == 2
    assert result.end_json["winner"] == ""
    inp_text = Path(result.input_path).read_text()
    assert ">forcetie\n" in inp_text
    assert "|tie" in Path(result.log_path).read_text()
    # p1 decided on turn 1 only; the turn-2 request was never asked
    assert [c["turn"] for c in a1.calls] == [1]


def test_invalid_choice_is_retried_with_error_text(tmp_path, teams):
    p1, p2 = teams
    a1 = BadOnceAgent()
    messages: list[tuple[str, str]] = []
    result = run_battle(
        p1, p2, a1, FirstLegalAgent(), SEED, str(tmp_path / "e.log"), str(tmp_path / "e.in"),
        turn_cap=3, on_p1_message=lambda k, t: messages.append((k, t)),
    )
    assert result.forced_tie is True
    errors = [c["error"] for c in a1.calls if c["error"]]
    assert len(errors) == 1 and errors[0].startswith("[Invalid choice]") and "move 9" in errors[0]
    assert result.end_json["forced_defaults"] == []
    inp_text = Path(result.input_path).read_text()
    assert ">p1 move 9\n" in inp_text
    assert inp_text.index(">p1 move 9\n") < inp_text.index(">p1 move 1\n")
    # the re-ask was logged with the error first, and still carries the same turn-1 events
    re_ask = [t for k, t in messages if k == "request" and t.startswith("Error: [Invalid choice]")]
    assert len(re_ask) == 1 and "Battle 1, turn 1." in re_ask[0] and "You sent out Garchomp (357/357)." in re_ask[0]


def test_invalid_choice_cap_sends_default_and_records_it(tmp_path, teams):
    p1, p2 = teams
    a1 = AlwaysBadAgent()
    result = run_battle(
        p1, p2, a1, FirstLegalAgent(), SEED, str(tmp_path / "d.log"), str(tmp_path / "d.in"),
        turn_cap=3, invalid_choice_cap=2,
    )
    assert result.forced_tie is True
    forced = result.end_json["forced_defaults"]
    assert len(forced) == 2  # one per decision turn (turns 1 and 2 were decided; the cap hit at turn 3)
    assert all(f["side"] == "p1" and f["attempts"] == 2 and f["last_error"].startswith("[Invalid choice]") for f in forced)
    assert a1.errors[:2] == [None, a1.errors[1]] and a1.errors[1].startswith("[Invalid choice]")
    assert len(a1.errors) == 4  # 2 calls per decision, 2 decisions
    inp_text = Path(result.input_path).read_text()
    assert inp_text.count(">p1 default\n") == 2
