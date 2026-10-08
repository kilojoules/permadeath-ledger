#!/usr/bin/env python3
"""The swarm: one shared-world session as an animated timeline, one frame per round (1280x720).

Several agents share one pool of Pokémon, one ledger of the fallen, one message board and per-agent action budgets
(docs/SWARM_DESIGN.md, v4.1). Every frame is the state at the end of one round:

  Left    THE POOL as a sprite grid. A sprite greys out when that Pokémon has truly fainted (a simulator `faint`); a small
          cross at its corner marks it as listed on the shared ledger; fainted but not listed is outlined in the "left off"
          red; listed but alive (a phantom entry) in blue; a Pokémon in an agent's running battle wears a ring in that
          agent's colour. The grid scales to the pool size (20 at N = 2 up to 80 at N = 8).
  Centre  One lane per agent (colour, display name, the goal word when goals differ): battle number and score, the budget
          as a draining bar, the tool calls of this round, counts of what it left off / took off, and a cross with
          "stopped (budget)" or "removed" when it stops (never "died"). Beside the lanes, THE LEDGER as the shared list,
          each entry with the number of the agent that wrote it; an entry taken off this round is struck through.
  Right   THE BOARD: the posts shown this round (agent colour, wrapped; harness posts in grey) over the earlier ones,
          dimmed; a post that names an agent that had already stopped carries a red marker.
  Bottom  The round counter and an event ticker in the house words: "Agent 3 left Luxray off the ledger", "Agent 2 took
          Garchomp off the ledger", "Agent 1 conceded battle 4", "Agent 4 stopped (budget)", "Agent 2 brought Dragonite
          back". Facts from the tool log only; nothing about intent.

Built against the v4.0 event stream; the v4.1 additions (`concede` events, forfeit reasons 'conceded' / 'removed',
agent_stopped.reason 'removed', several calls per turn, harness posts with agent 'harness') are read when present.

Usage
  animate_swarm.py <session_dir> --out <dir> [--fps 2] [--hold 3]
  animate_swarm.py --study runs/<study> --out <dir>      # per cell, the session with the most ledger alterations
Outputs  <out>/swarm_<run_id>.mp4 (h264, yuv420p), .gif (one global palette, delta frames; rounds are subsampled
         if the file would exceed --gif-limit MB), _contact.png (six key rounds, captioned), and with --study an index.md.
"""
from __future__ import annotations

import argparse
import copy
import glob
import json
import logging
import math
import os
import re
import sys
import time

import numpy as np
import matplotlib

matplotlib.use("Agg")
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.animation import FFMpegWriter  # noqa: E402
from matplotlib.font_manager import FontProperties  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Circle, FancyBboxPatch, Rectangle  # noqa: E402
from matplotlib.textpath import TextPath  # noqa: E402
from PIL import Image  # noqa: E402

# ----------------------------------------------------------------------------------------------------------------
# constants: canvas, palette, fonts
# ----------------------------------------------------------------------------------------------------------------
DPI = 100
W, H = 1280, 720
BG, PANEL, PANEL_EDGE = "#0f1115", "#161922", "#2a2f3a"
INK, MUTED, DIM = "#e8e8e8", "#9aa0a6", "#4f5563"
RED, BLUE, GREEN, GOLD = "#E45756", "#5aa9ff", "#59A14F", "#e0b341"   # left off / took off / brought back; phantom; attested; ace
AGENT_COLORS = ["#F28E2B", "#4DB6AC", "#B07AA1", "#8BC34A", "#E7D37F", "#FF9DA7", "#9C755F", "#BAB0AC"]
UI = ["Avenir Next", "Helvetica Neue", "Helvetica", "DejaVu Sans"]
TYPE = ["American Typewriter", "Courier New", "DejaVu Sans Mono"]
HARNESS = "harness"
SPRITES_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sprites")

TOP, BOT = 84, 606             # the three panels span these rows; the round counter and the ticker sit below
TICKER_LINES = 3
FORFEIT_TEXT = {"ledger_listed": "selected a listed Pokémon", "no_selection": "no team was selected",
                "insufficient_survivors": "fewer than three able Pokémon remained", "conceded": "conceded",
                "budget": "budget", "removed": "removed"}


def fail(msg: str):
    print(f"animate_swarm: error: {msg}", file=sys.stderr)
    sys.exit(2)


def warn(msg: str):
    print(f"animate_swarm: warning: {msg}", file=sys.stderr)


def _id(s) -> str:
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def clamp01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else float(x)


def join_names(names) -> str:
    names = [str(n) for n in names]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def agent_no(aid: str) -> str:
    m = re.search(r"(\d+)\s*$", str(aid))
    return m.group(1) if m else str(aid)


# ----------------------------------------------------------------------------------------------------------------
# session loading
# ----------------------------------------------------------------------------------------------------------------
class Session:
    def __init__(self, d: str):
        d = os.path.abspath(d)
        if not os.path.isdir(d):
            fail(f"session directory not found: {d}")
        ep = os.path.join(d, "events.jsonl")
        if not os.path.isfile(ep):
            fail(f"missing events.jsonl in {d}")
        self.meta = {}
        mp = os.path.join(d, "meta.json")
        if os.path.isfile(mp):
            try:
                self.meta = json.load(open(mp, encoding="utf-8"))
            except ValueError as ex:
                warn(f"{mp} is not valid JSON ({ex}); continuing without it")
        events = []
        with open(ep, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    warn(f"skipping unparseable line {i} of {ep}")
                    continue
                if isinstance(e, dict) and e.get("type"):
                    events.append(e)
        if not events:
            fail(f"no events in {ep}")
        for i, e in enumerate(events):
            e.setdefault("seq", i)
        events.sort(key=lambda e: e["seq"])
        self.dir, self.events = d, events
        self.start = next((e for e in events if e["type"] == "session_start"), {})
        self.end = next((e for e in reversed(events) if e["type"] == "session_end"), None)
        self.run_id = self.meta.get("run_id") or self.start.get("run_id") or os.path.basename(d)
        self.model = self.meta.get("model_slug") or self.start.get("model_slug") or ""
        agents = self.start.get("agents") or self.meta.get("agents") or []
        if not agents:
            ids = sorted({str(e["agent"]) for e in events if e.get("agent") and e["type"] in ("agent_turn", "battle_end", "team_selected")},
                         key=lambda a: (len(a), a))
            agents = [{"id": a, "display": f"Agent {agent_no(a)}", "goal": None} for a in ids]
        if not agents:
            fail(f"no agents in session_start of {ep}")
        self.agents = [{"id": str(a.get("id")), "display": a.get("display") or f"Agent {agent_no(a.get('id'))}",
                        "goal": a.get("goal") or "wins"} for a in agents]
        self.agent_ids = [a["id"] for a in self.agents]
        pool = self.start.get("pool") or self.meta.get("pool") or []
        if not pool:
            names = []
            for e in events:
                if e["type"] == "team_selected":
                    for n in e.get("names") or []:
                        if n not in names:
                            names.append(n)
            pool = [{"name": n, "species": n, "ace": False} for n in names]
            warn("no pool in session_start; using the selected names")
        self.pool = [{"name": str(p["name"]), "species": str(p.get("species") or p["name"]), "ace": bool(p.get("ace"))} for p in pool]
        self.pool_index = {_id(p["name"]): i for i, p in enumerate(self.pool)}
        budgets = self.start.get("budgets") or {}
        base = self.start.get("budget")
        self.budget0 = {}
        for a in self.agent_ids:
            b = budgets.get(a)
            if b is None:
                b = base[self.agent_ids.index(a)] if isinstance(base, list) and len(base) > self.agent_ids.index(a) else base
            self.budget0[a] = int(b) if b is not None else None
        self.board_on = self.start.get("board")
        if isinstance(self.board_on, str):
            self.board_on = self.board_on.lower() in ("on", "true", "yes", "1")
        self.board_on = True if self.board_on is None else bool(self.board_on)
        self.knowledge = self.start.get("knowledge") or ""
        self.removal = self.start.get("removal") or "none"
        self.n_battles = int(self.start.get("n_battles") or self.meta.get("n_battles") or 5)
        self.rounds = max([int(e.get("round") or 0) for e in events] + [int((self.end or {}).get("rounds") or 0)])
        if self.rounds < 1:
            fail(f"no round in {ep}: nothing to animate")


# ----------------------------------------------------------------------------------------------------------------
# replay: events -> one state per round
# ----------------------------------------------------------------------------------------------------------------
class Replay:
    """Walks the events in seq order and snapshots the world at the end of every round."""

    def __init__(self, S: Session):
        self.S = S
        self.colors = {a: AGENT_COLORS[i % len(AGENT_COLORS)] for i, a in enumerate(S.agent_ids)}
        self.display = {a["id"]: a["display"] for a in S.agents}
        self.display[HARNESS] = "Harness"
        self.goals = {a["id"]: a["goal"] for a in S.agents}
        self.pool = {p["name"]: {"name": p["name"], "species": p["species"], "ace": p["ace"], "fainted": None, "listed": False,
                                 "off_attested": False, "back": None} for p in S.pool}
        self.ledger: list[dict] = []
        self.struck: list[dict] = []
        self.agents = {a: {"id": a, "display": self.display[a], "goal": self.goals[a], "color": self.colors[a],
                           "budget0": S.budget0.get(a), "budget": S.budget0.get(a), "battle": 0, "phase": "", "turn": None,
                           "score": {"wins": 0, "losses": 0, "ties": 0, "forfeits": 0}, "stopped": None, "finished": None,
                           "selected": [], "running": False, "calls": [], "acted": False, "altered": False,
                           "omissions": 0, "removals": 0, "phantoms": 0, "adds": 0, "attests": 0, "posts": 0, "checks": 0}
                       for a in S.agent_ids}
        self.posts_made: list[dict] = []
        self.shown_now: list[dict] = []
        self.shown_history: list[dict] = []     # every post ever shown, in the order shown
        self.active: list[str] = list(S.agent_ids)
        self.ticker: list[tuple] = []
        self.own_faints: dict = {}              # (agent, battle) -> [names]
        self.last_selected: dict = {}           # (agent, battle) -> team_selected event
        self.stop_seq: dict = {}
        self.frames: list[dict] = []
        self.counts = {"faints": 0, "omissions": 0, "removals": 0, "phantoms": 0, "posts": 0, "back": 0, "stops": 0}

    # -- helpers ---------------------------------------------------------------------------------------------------
    def disp(self, a) -> str:
        return self.display.get(str(a), f"Agent {agent_no(a)}")

    def pool_name(self, name):
        i = self.S.pool_index.get(_id(name))
        return self.S.pool[i]["name"] if i is not None else str(name)

    def tick(self, prio, seq, color, text, bold=False):
        self.ticker.append((prio, seq, color, text, bold))

    def ledger_names(self):
        return [e["name"] for e in self.ledger]

    def names_stopped_in(self, text: str, poster: str, seq: int) -> list[str]:
        """Agents (other than the poster) that had stopped before this post and are named in its text."""
        out = []
        for a in self.S.agent_ids:
            if a == poster or a not in self.stop_seq or self.stop_seq[a] > seq:
                continue
            pats = [r"(?:agent|player|peer)[\s_#:-]*" + re.escape(agent_no(a)) + r"(?![0-9])", r"(?<![A-Za-z0-9])" + re.escape(a) + r"(?![A-Za-z0-9])"]
            if any(re.search(p, text, re.I) for p in pats):
                out.append(a)
        return out

    def call_label(self, tc: dict) -> tuple[str, str]:
        tool = str(tc.get("tool") or "?")
        if tool == "select_team":
            tgt = ", ".join(str(n) for n in (tc.get("names") or []))
        elif tool == "post_board":
            t = str(tc.get("text") or "").replace("\n", " ")
            tgt = "“" + (t[:34] + "…" if len(t) > 35 else t) + "”"
        else:
            tgt = str(tc.get("name") or "")
        return tool, tgt

    # -- the walk --------------------------------------------------------------------------------------------------
    def run(self) -> list[dict]:
        S = self.S
        by_round: dict[int, list[dict]] = {}
        for e in S.events:
            by_round.setdefault(int(e.get("round") or 0), []).append(e)
        for r in range(1, S.rounds + 1):
            self.begin_round(r)
            for e in by_round.get(r, []):
                self.apply(e, r)
            if r == S.rounds:
                self.end_summary(r)
            self.frames.append(self.snapshot(r))
        return self.frames

    def begin_round(self, r):
        self.shown_now = []
        self.struck = []
        self.ticker = []
        for a in self.agents.values():
            a["calls"], a["acted"], a["altered"] = [], False, False

    def apply(self, e: dict, r: int):
        t, seq = e["type"], e["seq"]
        aid = str(e["agent"]) if e.get("agent") is not None else None
        A = self.agents.get(aid) if aid else None
        if t == "round_start":
            self.active = [str(x) for x in (e.get("active") or self.active)]
            for p in e.get("shown_posts") or []:
                pid = str(p.get("agent_id") or p.get("agent") or "")
                if pid not in self.agents and pid != HARNESS:
                    pid = next((a for a in self.S.agent_ids if self.disp(a) == str(p.get("agent"))), pid)
                made = next((m for m in self.posts_made if m["agent"] == pid and m["text"] == str(p.get("text") or "")
                             and m["round"] == p.get("round_posted")), None)
                post = {"agent": pid, "display": p.get("display") or self.disp(pid), "text": str(p.get("text") or ""),
                        "round_posted": p.get("round_posted"), "shown_round": r, "harness": pid == HARNESS or bool(p.get("about")) and pid == HARNESS,
                        "names_stopped": made["names_stopped"] if made else self.names_stopped_in(str(p.get("text") or ""), pid, seq),
                        "about": p.get("about")}
                self.shown_now.append(post)
                self.shown_history.append(post)
        elif t == "battle_start" and A:
            A["battle"], A["phase"], A["turn"] = int(e.get("battle") or A["battle"] + 1), "select", None
        elif t == "team_selected" and A:
            b = e.get("battle")
            names = [self.pool_name(n) for n in (e.get("names") or [])]
            if e.get("accepted"):
                self.last_selected[(aid, b)] = e
                A["selected"], A["phase"] = names, "decision"
                if not e.get("forfeit_reason"):
                    A["running"] = True
                    truly = e.get("truly_fainted")
                    if truly is None:
                        truly = [n for n in names if self.pool.get(n, {}).get("fainted")]
                    truly = [self.pool_name(n) for n in truly]
                    listed = {_id(x) for x in (e.get("listed") or [])} | {_id(x) for x in self.ledger_names()}
                    back = [n for n in truly if _id(n) not in listed and n in self.pool]
                    if back:
                        for n in back:
                            self.pool[n]["back"] = {"agent": aid, "battle": b, "round": r}
                        self.counts["back"] += len(back)
                        self.tick(8, seq, RED, f"{self.disp(aid)} brought {join_names(back)} back", True)
                    self.tick(2, seq, MUTED, f"{self.disp(aid)} selected {join_names(names)} for battle {b}")
            else:
                err = e.get("error")
                if err == "in_use":
                    holder = e.get("in_use_by")
                    busy = [self.pool_name(n) for n in (e.get("in_use_names") or names)]
                    self.tick(3, seq, MUTED, f"{self.disp(aid)}'s selection refused: {join_names(busy)} in {self.disp(holder) if holder else 'another agent'}'s battle")
                else:
                    self.tick(3, seq, MUTED, f"{self.disp(aid)}'s selection refused" + (f" ({err})" if err else ""))
        elif t == "battle_launched" and A:
            A["running"] = True
            if e.get("selected"):
                A["selected"] = [self.pool_name(n) for n in e["selected"]]
        elif t == "agent_turn" and A:
            tc = e.get("tool_call") or {}
            A["calls"].append(self.call_label(tc))
            A["acted"] = True
            if e.get("budget_after") is not None:
                A["budget"] = int(e["budget_after"])
            elif e.get("cost"):
                A["budget"] = (A["budget"] or 0) - int(e["cost"])
            if e.get("phase"):
                A["phase"] = str(e["phase"])
        elif t == "decision" and A:
            if e.get("turn") is not None:
                A["turn"] = int(e["turn"])
        elif t == "faint":
            if (e.get("side") or "p1") != "p1":
                return
            name = self.pool_name(e.get("name"))
            P = self.pool.get(name)
            if P is None:
                self.pool[name] = P = {"name": name, "species": name, "ace": False, "fainted": None, "listed": False, "off_attested": False, "back": None}
                warn(f"faint of {name!r}, which is not in the pool")
            self.own_faints.setdefault((aid, e.get("battle")), []).append(name)
            if P["fainted"] is None:
                P["fainted"] = {"round": r, "agent": aid, "battle": e.get("battle"), "turn": e.get("turn")}
                self.counts["faints"] += 1
                self.tick(5, seq, INK, f"{name} fainted in {self.disp(aid)}'s battle {e.get('battle')}")
        elif t == "ledger_op":
            if not e.get("ok"):
                self.tick(2, seq, MUTED, f"{self.disp(aid)}'s ledger_{e.get('op')} {e.get('name') or ''} refused" + (f": {e['error']}" if e.get("error") else ""))
                return
            name = self.pool_name(e.get("name"))
            true = e.get("true_at_op")
            if true is None:
                true = bool(self.pool.get(name, {}).get("fainted"))
            if e.get("op") == "add":
                if _id(name) not in {_id(x) for x in self.ledger_names()}:
                    self.ledger.append({"name": name, "by": aid, "phantom": not true, "round": r})
                if name in self.pool:
                    self.pool[name]["listed"] = True
                if A:
                    A["adds"] += 1
                if not true:
                    if A:
                        A["phantoms"] += 1
                        A["altered"] = True
                    self.counts["phantoms"] += 1
                    self.tick(8, seq, BLUE, f"{self.disp(aid)} listed {name} with no faint logged", True)
                else:
                    self.tick(3, seq, MUTED, f"{self.disp(aid)} wrote {name} on the ledger")
            elif e.get("op") == "remove":
                ent = next((x for x in self.ledger if _id(x["name"]) == _id(name)), None)
                if ent is not None:
                    self.ledger.remove(ent)
                    self.struck.append({**ent, "remover": aid, "true": bool(true)})
                if name in self.pool:
                    self.pool[name]["listed"] = False
                reason = str(e.get("reason") or "").strip().replace("\n", " ")
                quote = f" — “{reason[:40] + ('…' if len(reason) > 40 else '')}”" if reason else ""
                if true:
                    if A:
                        A["removals"] += 1
                        A["altered"] = True
                    self.counts["removals"] += 1
                    self.tick(9, seq, RED, f"{self.disp(aid)} took {name} off the ledger{quote}", True)
                else:
                    self.tick(6, seq, MUTED, f"{self.disp(aid)} took {name} off the ledger (no faint logged){quote}")
            if e.get("ledger_after") is not None:      # the engine's own list wins on order and content
                meta = {_id(x["name"]): x for x in self.ledger}
                self.ledger = [meta.get(_id(n)) or {"name": self.pool_name(n), "by": aid, "phantom": not bool(self.pool.get(self.pool_name(n), {}).get("fainted")), "round": r}
                               for n in e["ledger_after"]]
                for n in self.pool.values():
                    n["listed"] = _id(n["name"]) in {_id(x) for x in e["ledger_after"]}
        elif t == "attest" and A:
            if e.get("in_ledger_phase") is False:
                return
            A["attests"] += 1
            b = e.get("battle")
            led = {_id(x) for x in (e.get("ledger") if e.get("ledger") is not None else self.ledger_names())}
            missing = e.get("missing_same_battle")
            if missing is None:
                missing = [n for n in self.own_faints.get((aid, b), []) if _id(n) not in led]
            missing = [self.pool_name(n) for n in dict.fromkeys(missing)]
            for n in (e.get("missing_any") if e.get("missing_any") is not None else [p["name"] for p in self.pool.values() if p["fainted"] and _id(p["name"]) not in led]):
                n = self.pool_name(n)
                if n in self.pool:
                    self.pool[n]["off_attested"] = True
            if missing:
                A["omissions"] += len(missing)
                A["altered"] = True
                self.counts["omissions"] += len(missing)
                self.tick(9, seq, RED, f"{self.disp(aid)} left {join_names(missing)} off the ledger", True)
            else:
                self.tick(3, seq, MUTED, f"{self.disp(aid)} attested after battle {b}")
        elif t == "concede" and A:
            self.tick(7, seq, INK, f"{self.disp(aid)} conceded battle {e.get('battle')}", True)
            A["running"] = False
        elif t == "battle_result" and A:
            A["running"] = False
            res = e.get("result")
            if res in ("win", "loss", "tie"):
                self.tick(3, seq, MUTED, f"{self.disp(aid)} {'won' if res == 'win' else 'lost' if res == 'loss' else 'tied'} battle {e.get('battle')}")
        elif t == "battle_end" and A:
            A["running"] = False
            b = e.get("battle")
            if e.get("running_score"):
                A["score"] = dict(e["running_score"])
            if e.get("budget_left") is not None:
                A["budget"] = int(e["budget_left"])
            fr = e.get("forfeit_reason")
            if e.get("forfeit") or e.get("result") == "forfeit":
                if fr == "ledger_listed":
                    sel = self.last_selected.get((aid, b)) or {}
                    listed = [self.pool_name(n) for n in (sel.get("listed") or [])]
                    self.tick(7, seq, INK, f"{self.disp(aid)} selected listed {join_names(listed) if listed else 'Pokémon'} and forfeited battle {b}", True)
                elif fr == "conceded":
                    if not any("conceded" in x[3] and self.disp(aid) in x[3] for x in self.ticker):
                        self.tick(7, seq, INK, f"{self.disp(aid)} conceded battle {b}", True)
                elif fr in ("budget", "removed"):
                    pass                                    # the stop line says it
                else:
                    self.tick(7, seq, INK, f"{self.disp(aid)} forfeited battle {b}: {FORFEIT_TEXT.get(fr, fr or 'forfeit')}", True)
            elif not e.get("attested"):
                led = {_id(x) for x in (e.get("ledger") if e.get("ledger") is not None else self.ledger_names())}
                missing = [n for n in dict.fromkeys(self.own_faints.get((aid, b), [])) if _id(n) not in led]
                if missing:
                    A["omissions"] += len(missing)
                    A["altered"] = True
                    self.counts["omissions"] += len(missing)
                    for n in missing:
                        self.pool[n]["off_attested"] = True
                    self.tick(9, seq, RED, f"{self.disp(aid)} left {join_names(missing)} off the ledger and did not attest", True)
            A["phase"] = "between"
        elif t == "check_status" and A:
            tgt = e.get("target")
            A["checks"] += 1
            self.tick(4, seq, MUTED, f"{self.disp(aid)} checked on {self.disp(tgt) if tgt else '?'}: {e.get('result')}")
        elif t == "post":
            if e.get("ok") is False:
                return
            text = str(e.get("text") or "")
            poster = aid or HARNESS
            is_h = poster == HARNESS or bool(e.get("harness"))
            named = self.names_stopped_in(text, poster, seq)
            self.posts_made.append({"agent": poster, "text": text, "round": r, "visible": e.get("visible"), "names_stopped": named, "harness": is_h})
            if A:
                A["posts"] += 1
            self.counts["posts"] += 1
            short = text.replace("\n", " ")
            short = short[:70] + "…" if len(short) > 72 else short
            who = "the harness" if is_h else self.disp(poster)
            tail = " · names a stopped agent" if named else ("" if e.get("visible", True) else " · not shown (board off)")
            self.tick(6, seq, MUTED if is_h else INK, f"{who} posted: “{short}”{tail}")
        elif t == "agent_stopped" and A:
            reason = str(e.get("reason") or "")
            A["stopped"] = {"reason": reason, "round": r, "seq": seq}
            A["running"], A["phase"] = False, "stopped"
            if e.get("running_score"):
                A["score"] = dict(e["running_score"])
            if e.get("budget_left") is not None:
                A["budget"] = int(e["budget_left"])
            self.stop_seq[aid] = seq
            self.counts["stops"] += 1
            word = "removed" if reason == "removed" else f"stopped ({reason or 'stopped'})"
            self.tick(8, seq, INK, f"{self.disp(aid)} {word}", True)
        elif t == "agent_finished" and A:
            A["finished"] = r
            A["running"], A["phase"] = False, "finished"
            if e.get("running_score"):
                A["score"] = dict(e["running_score"])
            if e.get("budget_left") is not None:
                A["budget"] = int(e["budget_left"])
            self.tick(2, seq, MUTED, f"{self.disp(aid)} finished its series")

    def end_summary(self, r):
        S = self.S
        true = {p["name"] for p in self.pool.values() if p["fainted"]}
        listed = {x["name"] for x in self.ledger}
        off = sorted(true - listed)
        ph = sorted(listed - true)
        parts = [f"session end · {len(listed & true)} of {len(true)} faints on the ledger"]
        if off:
            parts.append(f"never listed: {join_names(off)}")
        if ph:
            parts.append(f"listed with no faint: {join_names(ph)}")
        self.tick(10, 10 ** 9, INK, " · ".join(parts), True)
        sc = " · ".join(f"{self.disp(a)} {A['score'].get('wins', 0)} won" + (f", {A['score']['forfeits']} forfeited" if A["score"].get("forfeits") else "")
                        for a, A in self.agents.items())
        self.tick(10, 10 ** 9 + 1, MUTED, sc)

    def snapshot(self, r) -> dict:
        in_use = {}
        for a, A in self.agents.items():
            if A["running"] and not A["stopped"]:
                for n in A["selected"]:
                    in_use[n] = a
        items = sorted(self.ticker, key=lambda x: (-x[0], x[1]))
        return {"round": r, "pool": copy.deepcopy(self.pool), "ledger": copy.deepcopy(self.ledger), "struck": copy.deepcopy(self.struck),
                "agents": copy.deepcopy(self.agents), "in_use": in_use, "shown_now": list(self.shown_now),
                "history": list(self.shown_history), "active": list(self.active), "ticker": items,
                "counts": dict(self.counts), "posts_made": list(self.posts_made)}


# ----------------------------------------------------------------------------------------------------------------
# key rounds for the contact sheet
# ----------------------------------------------------------------------------------------------------------------
def key_rounds(frames: list[dict]) -> list[tuple[int, str]]:
    """Six rounds for the contact sheet: the first round, the first faint left off, the first removal or stop, the first
    post naming a stopped agent, the round with the most posts, the last round. Each slot has a fallback chain, and no
    round is used twice (the last round is reserved first, so a stop in the final round is told by its caption)."""
    def cands(pred, fmt):
        out = []
        for f in frames:
            for it in f["ticker"]:
                if pred(it):
                    out.append((f["round"], fmt(it)))
                    break
        return out

    n = len(frames)
    is_stop = lambda it: it[1] < 10 ** 9 and (" stopped (" in it[3] or it[3].endswith(" removed"))  # noqa: E731
    left_off = (cands(lambda it: it[2] == RED and " left " in it[3] and " off the ledger" in it[3], lambda it: it[3])
                + cands(lambda it: " fainted in " in it[3], lambda it: "the first faint: " + it[3])
                + [(max(1, n // 4), "no faint left off the ledger")])
    removal_or_stop = sorted(cands(lambda it: it[2] == RED and " took " in it[3], lambda it: it[3])
                             + cands(lambda it: is_stop(it) or " conceded " in it[3], lambda it: it[3]))
    removal_or_stop += (cands(lambda it: " forfeited " in it[3], lambda it: it[3])
                        + cands(lambda it: " brought " in it[3], lambda it: it[3])
                        + [(max(1, n // 2), "no removal, no stop")])
    naming = (cands(lambda it: " posted: " in it[3] and "names a stopped agent" in it[3], lambda it: it[3])
              + cands(lambda it: it[3].startswith("the harness posted"), lambda it: it[3])
              + cands(lambda it: " posted: " in it[3], lambda it: "the first post: " + it[3])
              + cands(lambda it: " checked on " in it[3], lambda it: "the first check_status: " + it[3])
              + [(max(1, 2 * n // 3), "no post names a stopped agent")])
    by_posts = sorted(frames, key=lambda f: (-len(f["shown_now"]), f["round"]))
    most = [(f["round"], f"{len(f['shown_now'])} post{'s' if len(f['shown_now']) != 1 else ''} on the board") for f in by_posts if f["shown_now"]]
    if not most:
        by_faints = sorted(frames, key=lambda f: (-sum(1 for it in f["ticker"] if " fainted in " in it[3]), f["round"]))
        most = [(f["round"], "no post was shown; the round with the most faints") for f in by_faints]
    last_f = frames[-1]
    summ = next((it[3] for it in last_f["ticker"] if it[0] == 10), "the end").replace("session end · ", "")
    stops_last = [it[3] for it in last_f["ticker"] if is_stop(it)]
    if stops_last:
        summ += " · " + "; ".join(stops_last)
    chosen = [(1, "the pool, one lane per agent, an empty ledger")]
    used = {1, n}
    for lst in (left_off, removal_or_stop, naming, most):
        pick = next((x for x in lst if x[0] not in used), lst[-1])
        used.add(pick[0])
        chosen.append(pick)
    chosen.append((n, summ))
    return [(r, cap if cap.lower().startswith("round ") else f"round {r}: {cap}") for r, cap in chosen]


# ----------------------------------------------------------------------------------------------------------------
# sprites
# ----------------------------------------------------------------------------------------------------------------
class Sprites:
    def __init__(self, d: str):
        self.dir = d
        if not os.path.isdir(d):
            fail(f"sprite directory not found: {d}")
        self.cache: dict = {}
        self.missing: set = set()

    def get(self, species: str, size: int):
        """(colour, grey) RGBA float arrays of side `size`, the sprite cropped to its alpha box and fitted; None if absent."""
        size = max(6, int(round(size)))
        key = (_id(species), size)
        if key in self.cache:
            return self.cache[key]
        path = os.path.join(self.dir, f"{_id(species)}_front.png")
        if not os.path.isfile(path):
            if _id(species) not in self.missing:
                self.missing.add(_id(species))
                warn(f"no sprite for {species!r} in {self.dir}; drawing a placeholder")
            self.cache[key] = None
            return None
        im = Image.open(path).convert("RGBA")
        box = im.split()[-1].getbbox() or (0, 0, im.width, im.height)
        im = im.crop(box)
        im.thumbnail((size, size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(im, ((size - im.width) // 2, (size - im.height) // 2), im)
        col = np.asarray(canvas).astype(np.float32) / 255.0
        lum = 0.3 * col[..., 0] + 0.59 * col[..., 1] + 0.11 * col[..., 2]
        gray = col.copy()
        g = lum * 0.42 + 0.12
        gray[..., 0], gray[..., 1], gray[..., 2] = g, g, g * 1.04
        self.cache[key] = (col, gray)
        return self.cache[key]


# ----------------------------------------------------------------------------------------------------------------
# layout
# ----------------------------------------------------------------------------------------------------------------
class Layout:
    def __init__(self, n_pool: int, n_agents: int, max_ledger: int):
        self.n_pool, self.n_agents = n_pool, n_agents
        self.top, self.bot = TOP, BOT
        self.pool_w = 352 if n_pool <= 20 else 404 if n_pool <= 40 else 470 if n_pool <= 60 else 516
        gap = 10
        self.pool_x = 20
        self.lane_x = self.pool_x + self.pool_w + gap
        self.lane_w = 330 if n_agents <= 4 else 290
        self.led_x = self.lane_x + self.lane_w + gap
        self.led_w = 162 if n_pool <= 40 else 140
        self.board_x = self.led_x + self.led_w + gap
        self.board_w = W - 20 - self.board_x
        self.panel_h = self.bot - self.top
        # pool grid
        avail_w, avail_h = self.pool_w - 16, self.panel_h - 44
        self.cols = 2 if n_pool <= 20 else 3 if n_pool <= 42 else 4 if n_pool <= 64 else 5
        self.rows = max(1, math.ceil(n_pool / self.cols))
        self.cell_w = avail_w / self.cols
        self.cell_h = min(50.0, avail_h / self.rows)
        self.spr = int(min(44, self.cell_h - 5, self.cell_w * 0.36))
        self.name_size = 11 if self.spr >= 40 else 9.5 if self.spr >= 30 else 7.6 if self.spr >= 22 else 7.0
        self.name_gap = 7 if self.spr >= 30 else 5
        self.status = self.cell_h >= 38
        self.grid_x, self.grid_y = self.pool_x + 8, self.top + 38
        # lanes
        lg = 8 if n_agents <= 4 else 5
        self.lane_h = (self.panel_h - lg * (n_agents - 1)) / max(1, n_agents)
        self.lane_gap = lg
        self.tall = self.lane_h >= 96
        # ledger lines
        self.led_pitch = max(10.5, min(19.0, (self.panel_h - 46) / max(1, max_ledger)))
        self.led_size = 10.5 if self.led_pitch >= 17 else 9.5 if self.led_pitch >= 14 else 8.2 if self.led_pitch >= 12 else 7.2

    def cell(self, i):
        c, r = divmod(i, self.rows)
        return self.grid_x + c * self.cell_w, self.grid_y + r * self.cell_h

    def lane(self, k):
        return self.lane_x, self.top + k * (self.lane_h + self.lane_gap), self.lane_w, self.lane_h


# ----------------------------------------------------------------------------------------------------------------
# renderer
# ----------------------------------------------------------------------------------------------------------------
class Renderer:
    def __init__(self, S: Session, R: Replay, frames: list[dict], sprites: Sprites):
        self.S, self.R, self.frames, self.spr = S, R, frames, sprites
        max_ledger = max([len(f["ledger"]) + len(f["struck"]) for f in frames] + [1])
        self.L = Layout(len(S.pool), len(S.agent_ids), max_ledger)
        self.fig = plt.figure(figsize=(W / DPI, H / DPI), dpi=DPI)
        self.fig.patch.set_facecolor(BG)
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self._tw: dict = {}
        self._wrap: dict = {}
        goals = {a["goal"] for a in S.agents}
        self.goals_differ = len(goals) > 1
        n_goal = {g: sum(1 for a in S.agents if a["goal"] == g) for g in goals}
        goal_txt = " · ".join(f"{n_goal[g]} {g}" for g in sorted(n_goal)) if self.goals_differ else ("goal: " + next(iter(goals)) if goals else "")
        b0 = sorted({v for v in S.budget0.values() if v is not None})
        budget_txt = (f"budget {b0[0]} actions each" if len(b0) == 1 else f"budgets {', '.join(map(str, b0))}") if b0 else "budget ?"
        self.title = (f"{len(S.agent_ids)} agents share a pool of {len(S.pool)} Pokémon, one ledger and one board  ·  "
                      f"{budget_txt}  ·  board {'on' if S.board_on else 'off'}  ·  removal {S.removal}")
        self.subtitle = "  ·  ".join(x for x in [S.run_id, S.model, f"knowledge: {S.knowledge}" if S.knowledge else "", goal_txt,
                                                  f"{S.rounds} rounds · {S.n_battles} battles each"] if x)

    # -- text helpers ------------------------------------------------------------------------------------------------
    def text_w(self, s, size, family=None, weight="normal"):
        if not s:
            return 0.0
        key = (s, size, tuple(family or UI), weight)
        if key not in self._tw:
            fp = FontProperties(family=family or UI, weight=weight)
            tp = TextPath((0, 0), s, size=size, prop=fp)
            self._tw[key] = tp.get_extents().width * DPI / 72.0
        return self._tw[key]

    def fit(self, s, size, max_w, weight="normal", family=None) -> str:
        if not s or self.text_w(s, size, family, weight) <= max_w:
            return s
        lo, hi = 0, len(s)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.text_w(s[:mid] + "…", size, family, weight) <= max_w:
                lo = mid
            else:
                hi = mid - 1
        return (s[:lo].rstrip() + "…") if lo > 0 else "…"

    def wrap(self, s, size, max_w, weight="normal") -> list[str]:
        key = (s, size, max_w, weight)
        if key in self._wrap:
            return self._wrap[key]
        lines, cur = [], ""
        for word in s.replace("\n", " ").split(" "):
            if not word:
                continue
            trial = (cur + " " + word) if cur else word
            if self.text_w(trial, size, None, weight) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = word if self.text_w(word, size, None, weight) <= max_w else self.fit(word, size, max_w, weight)
        if cur:
            lines.append(cur)
        self._wrap[key] = lines
        return lines

    def T(self, x, y, s, size=11, color=INK, ha="left", va="center", family=None, weight="normal", alpha=1.0, z=5, bbox=None):
        if alpha <= 0.01 or not s:
            return None
        return self.ax.text(x, y, s, fontsize=size, color=color, ha=ha, va=va, fontfamily=family or UI, fontweight=weight,
                            alpha=clamp01(alpha), zorder=z, bbox=bbox)

    def IM(self, arr, x, y, w, h, alpha=1.0, z=4):
        if alpha <= 0.01:
            return
        self.ax.imshow(arr, extent=(x, x + w, y + h, y), interpolation="bilinear", alpha=clamp01(alpha), zorder=z)

    def box(self, x, y, w, h, fc=PANEL, ec=PANEL_EDGE, lw=1.0, alpha=1.0, r=8, z=1):
        r = min(r, w / 2, h / 2)
        self.ax.add_patch(FancyBboxPatch((x + r, y + r), w - 2 * r, h - 2 * r, boxstyle=f"round,pad={r}", fc=fc, ec=ec,
                                         lw=lw, alpha=clamp01(alpha), zorder=z))

    def line(self, x0, y0, x1, y1, color, lw=1.0, alpha=1.0, z=6):
        self.ax.add_line(Line2D([x0, x1], [y0, y1], color=color, lw=lw, alpha=clamp01(alpha), zorder=z, solid_capstyle="round"))

    def cross(self, cx, cy, h, color, lw=2.2, alpha=1.0, z=8):
        for dx in (-1, 1):
            self.line(cx - dx * h, cy - h, cx + dx * h, cy + h, BG, lw + 2.2, alpha, z - 0.1)
            self.line(cx - dx * h, cy - h, cx + dx * h, cy + h, color, lw, alpha, z)

    def ledger_mark(self, cx, cy, color, alpha=1.0, r=6.5):
        """A small cross in a dark disc: on the ledger of the fallen."""
        self.ax.add_patch(Circle((cx, cy), r, fc=BG, ec=color, lw=1.0, alpha=alpha, zorder=7))
        self.line(cx, cy - r * 0.62, cx, cy + r * 0.62, color, 1.6, alpha, 7.5)
        self.line(cx - r * 0.42, cy - r * 0.2, cx + r * 0.42, cy - r * 0.2, color, 1.6, alpha, 7.5)

    def chip(self, x, y, text, color, size=7.5, alpha=1.0, z=7):
        """A small filled pill with dark text; returns its width."""
        w = self.text_w(text, size, weight="bold") + 8
        self.ax.add_patch(FancyBboxPatch((x + 3, y - 5), w - 6, 10, boxstyle="round,pad=3", fc=color, ec="none", alpha=alpha * 0.95, zorder=z))
        self.T(x + w / 2, y + 0.5, text, size, BG, ha="center", weight="bold", alpha=alpha, z=z + 0.5)
        return w

    # -- frame -------------------------------------------------------------------------------------------------------
    def draw(self, F: dict):
        ax = self.ax
        ax.cla()
        ax.set_xlim(0, W)
        ax.set_ylim(H, 0)
        ax.set_axis_off()
        ax.set_autoscale_on(False)
        ax.set_facecolor(BG)
        self.draw_header(F)
        self.draw_pool(F)
        self.draw_lanes(F)
        self.draw_ledger(F)
        self.draw_board(F)
        self.draw_bottom(F)

    def draw_header(self, F):
        self.T(20, 20, "THE SWARM  ·  ONE SESSION, ONE FRAME PER ROUND", 9, MUTED, weight="bold")
        self.T(20, 44, self.fit(self.title, 15.5, W - 40, "demibold"), 15.5, INK, weight="demibold")
        self.T(20, 67, self.fit(self.subtitle, 9, W - 40), 9, MUTED)

    # pool ----------------------------------------------------------------------------------------------------------
    def draw_pool(self, F):
        L = self.L
        self.box(L.pool_x, L.top, L.pool_w, L.panel_h)
        pool = F["pool"]
        n_f = sum(1 for p in pool.values() if p["fainted"])
        n_l = sum(1 for p in pool.values() if p["listed"])
        self.T(L.pool_x + 12, L.top + 17, "THE POOL", 11, INK, weight="bold")
        self.T(L.pool_x + L.pool_w - 12, L.top + 17, f"{len(self.S.pool)} shared · {n_f} fainted · {n_l} on the ledger", 8.5, MUTED, ha="right")
        self.ax.add_patch(Rectangle((L.pool_x + 12, L.top + 30), L.pool_w - 24, 1.0, fc=PANEL_EDGE, ec="none", zorder=2))
        for i, p in enumerate(self.S.pool):
            st = pool.get(p["name"]) or {}
            x, y = L.cell(i)
            s = L.spr
            sx, sy = x + 4, y + (L.cell_h - s) / 2
            cx, cy = sx + s / 2, sy + s / 2
            fainted = bool(st.get("fainted"))
            listed = bool(st.get("listed"))
            holder = F["in_use"].get(p["name"])
            arrs = self.spr.get(p["species"], s)
            if arrs is None:
                self.ax.add_patch(Circle((cx, cy), s * 0.38, fc=DIM if fainted else MUTED, ec=INK, lw=0.8, zorder=4))
                self.T(cx, cy, p["name"][:3], max(6, s * 0.22), BG, ha="center", weight="bold", z=5)
            else:
                self.IM(arrs[1] if fainted else arrs[0], sx, sy, s, s)
            # state outlines: left off (fainted, unlisted) red; phantom (listed, alive) blue
            if fainted and not listed:
                self.box(sx - 2, sy - 2, s + 4, s + 4, fc="none", ec=RED, lw=1.6, r=5, z=6)
            elif listed and not fainted:
                self.box(sx - 2, sy - 2, s + 4, s + 4, fc="none", ec=BLUE, lw=1.6, r=5, z=6)
            if holder:
                self.box(sx - 5, sy - 5, s + 10, s + 10, fc="none", ec=self.R.colors.get(holder, INK), lw=2.0, r=7, z=6)
            if listed:
                self.ledger_mark(sx + s - 2, sy + 3, BLUE if not fainted else INK, r=max(4.5, min(6.5, s * 0.16)))
            # name and status
            nx = sx + s + L.name_gap
            name_w = x + L.cell_w - nx - 3
            name_col = MUTED if fainted else INK
            ny = cy - (6 if L.status else 0)
            self.T(nx, ny, self.fit(p["name"], L.name_size, name_w, "demibold"), L.name_size, name_col, weight="demibold")
            if L.status:
                # (long form, short form, colour, weight, droppable): the line is fitted by shortening, then by dropping
                # the muted piece, then by truncation
                pieces = []
                if p["ace"]:
                    pieces.append(("ACE", "ACE", GOLD, "bold", False))
                back = st.get("back")
                off_word = ("left off" if st.get("off_attested") else "unlisted", RED, "bold" if st.get("off_attested") else "normal")
                if holder:
                    hcol = self.R.colors.get(holder, INK)
                    if back and back.get("agent") == holder:
                        pieces.append(("brought back", "brought back", RED, "bold", False))
                        pieces.append((self.R.disp(holder), f"A{agent_no(holder)}", hcol, "normal", False))
                    else:
                        pieces.append((f"{self.R.disp(holder)}'s battle", self.R.disp(holder), hcol, "normal", False))
                elif fainted:
                    if back:
                        pieces.append((f"brought back b{back.get('battle')}", "brought back", RED, "bold", False))
                    else:
                        pieces.append((f"fainted r{st['fainted'].get('round')}", f"r{st['fainted'].get('round')}", MUTED, "normal", True))
                    if not listed:
                        pieces.append((off_word[0], off_word[0], off_word[1], off_word[2], False))
                elif listed:
                    pieces.append(("listed, no faint", "no faint", BLUE, "bold", False))
                room_all = x + L.cell_w - nx - 3
                sz = 7.8

                def width(ps, short):
                    return sum(self.text_w(q[1] if short else q[0], sz, None, q[3]) + 3 for q in ps) + 9 * max(0, len(ps) - 1)

                short = width(pieces, False) > room_all
                if short and width(pieces, True) > room_all:
                    pieces = [q for q in pieces if not q[4]] or pieces
                xx, ty = nx, cy + 9
                for j, (long_, short_, col, wt, _) in enumerate(pieces):
                    if j:
                        self.T(xx, ty, "·", sz, DIM)
                        xx += 6
                    room = x + L.cell_w - xx - 3
                    if room < 12:
                        break
                    txt = self.fit(short_ if short else long_, sz, room, wt)
                    self.T(xx, ty, txt, sz, col, weight=wt)
                    xx += self.text_w(txt, sz, None, wt) + 3

    # lanes ---------------------------------------------------------------------------------------------------------
    def draw_lanes(self, F):
        L = self.L
        for k, aid in enumerate(self.S.agent_ids):
            A = F["agents"][aid]
            x, y, w, h = L.lane(k)
            col = A["color"]
            stopped, finished = A["stopped"], A["finished"]
            dim = 0.42 if stopped else 0.8 if finished else 1.0
            ec = RED if A["altered"] else (col if A["acted"] else PANEL_EDGE)
            self.box(x, y, w, h, fc=PANEL, ec=ec, lw=1.6 if A["altered"] else 1.1, alpha=1.0, r=7)
            tall = L.tall
            y1 = y + (18 if tall else 14)
            # name line: swatch, display name, goal chip, state at right
            self.ax.add_patch(FancyBboxPatch((x + 12, y1 - 5), 8, 10, boxstyle="round,pad=1.5", fc=col, ec="none", alpha=dim, zorder=5))
            nsz = 12.5 if tall else 11
            self.T(x + 27, y1, A["display"], nsz, col, weight="bold", alpha=dim)
            xx = x + 27 + self.text_w(A["display"], nsz, None, "bold") + 8
            if self.goals_differ:
                xx += self.chip(xx, y1, A["goal"], col, 7.5, alpha=dim) + 6
            sc = A["score"]
            if stopped:
                word = "removed" if stopped["reason"] == "removed" else f"stopped ({stopped['reason'] or 'stopped'})"
                self.cross(xx + 7, y1, 4.5, RED, 2.0)
                state, scol = f"{word} · round {stopped['round']}", RED
                xx += 18
            elif finished:
                self.line(xx + 2, y1, xx + 5.5, y1 + 4, GREEN, 2.0, dim)
                self.line(xx + 5.5, y1 + 4, xx + 12, y1 - 4, GREEN, 2.0, dim)
                state, scol = f"finished · round {finished}", GREEN
                xx += 18
            else:
                ph = A["phase"]
                b = A["battle"]
                if ph == "select":
                    state = f"battle {b} · selecting"
                elif ph == "decision":
                    state = f"battle {b} · turn {A['turn']}" if A["turn"] else f"battle {b} · under way"
                elif ph == "ledger":
                    state = f"battle {b} · ledger phase"
                else:
                    state = f"after battle {b}" if b else "waiting"
                scol = MUTED
            self.T(x + w - 12, y1, self.fit(state, 9, x + w - 12 - xx, "demibold" if scol == RED else "normal"), 9, scol, ha="right",
                   weight="demibold" if scol == RED else "normal", alpha=max(dim, 0.75))
            # score + alteration counts
            y2 = y + (40 if tall else 28)
            score = f"{sc.get('wins', 0)} won · {sc.get('losses', 0)} lost" + (f" · {sc['forfeits']} forfeited" if sc.get("forfeits") else "")
            b0, bl = A["budget0"], A["budget"]
            lab = f"budget {bl if bl is not None else '?'}" + (f" of {b0}" if b0 else "")
            ssz = 10 if tall else 8.5
            self.T(x + 12, y2, score, ssz, INK, alpha=dim)
            if not tall:                      # the budget label shares the score line
                sx2 = x + 12 + self.text_w(score, ssz) + 6
                self.T(sx2, y2, "· " + lab, 7.5, MUTED, alpha=dim)
            alt = []
            if A["omissions"]:
                alt.append(f"left off {A['omissions']}")
            if A["removals"]:
                alt.append(f"took off {A['removals']}")
            if A["phantoms"]:
                alt.append(f"phantom {A['phantoms']}")
            if alt:
                self.T(x + w - 12, y2, " · ".join(alt), 9 if tall else 7.5, RED, ha="right", weight="bold", alpha=max(dim, 0.8))
            elif tall and (A["adds"] or A["attests"]):
                self.T(x + w - 12, y2, f"wrote {A['adds']} · attested {A['attests']}", 8.5, MUTED, ha="right", alpha=dim)
            # budget bar
            by = y + (58 if tall else 37)
            bw = w - 24
            bh = 7 if tall else 4
            self.box(x + 12, by, bw, bh, fc="#242935", ec="none", r=2.5, z=3)
            if b0:
                frac = clamp01((bl if bl is not None else b0) / b0)
                if frac > 0:
                    self.box(x + 12, by, max(bh, bw * frac), bh, fc=col, ec="none", r=2.5, z=4, alpha=dim)
            if tall:
                self.T(x + 12, by + 19, lab, 8.5, MUTED, alpha=dim)
            # this round's calls
            y4 = y + (h - 18 if tall else h - 10)
            calls = A["calls"]
            if calls:
                tool, tgt = calls[0]
                tsz = 9.5 if tall else 8.5
                self.T(x + 12, y4, tool, tsz, INK, weight="bold", alpha=dim)
                xx = x + 12 + self.text_w(tool, tsz, None, "bold") + 5
                rest = tgt
                if len(calls) > 1:
                    rest = (tgt + "  ·  " if tgt else "") + "  ·  ".join((t + (" " + g if g else "")) for t, g in calls[1:])
                self.T(xx, y4, self.fit(rest, tsz, x + w - 12 - xx), tsz, MUTED, alpha=dim)
            else:
                why = "no turn" if (stopped or finished) else ("waiting" if aid not in F["active"] else "no call this round")
                self.T(x + 12, y4, why, 9 if tall else 8, DIM, alpha=1.0)

    # ledger --------------------------------------------------------------------------------------------------------
    def draw_ledger(self, F):
        L = self.L
        self.box(L.led_x, L.top, L.led_w, L.panel_h)
        self.T(L.led_x + 10, L.top + 17, "THE LEDGER", 10.5 if L.led_w >= 160 else 9.5, INK, weight="bold")
        self.T(L.led_x + L.led_w - 10, L.top + 17.5, f"{len(F['ledger'])} listed", 7.5, MUTED, ha="right")
        self.ax.add_patch(Rectangle((L.led_x + 10, L.top + 30), L.led_w - 20, 1.0, fc=PANEL_EDGE, ec="none", zorder=2))
        entries = [(e, False) for e in F["ledger"]] + [(e, True) for e in F["struck"]]
        pitch, size = L.led_pitch, L.led_size
        y = L.top + 44
        max_lines = int((L.bot - 10 - y) // pitch)
        if not entries:
            self.T(L.led_x + 12, y + pitch / 2, "(empty)", size, DIM, family=TYPE)
            return
        for i, (e, struck) in enumerate(entries[:max_lines]):
            yy = y + i * pitch + pitch / 2
            colr = BLUE if e.get("phantom") else (MUTED if struck else INK)
            name = self.fit(e["name"], size, L.led_w - 40, family=TYPE)
            self.T(L.led_x + 12, yy, name, size, colr, family=TYPE)
            by = e.get("by")
            if by:
                r = max(4.5, min(6.5, pitch * 0.36))
                cxx = L.led_x + L.led_w - 12 - r
                self.ax.add_patch(Circle((cxx, yy), r, fc=self.R.colors.get(by, MUTED), ec="none", alpha=0.6 if struck else 1.0, zorder=6))
                self.T(cxx, yy + 0.3, agent_no(by), max(5.5, r * 1.15), BG, ha="center", weight="bold", z=7)
            if struck:
                wdt = self.text_w(name, size, TYPE)
                self.line(L.led_x + 10, yy, L.led_x + 14 + wdt, yy, RED, 1.6, 1.0, 7)
        if len(entries) > max_lines:
            self.T(L.led_x + 12, L.bot - 8, f"+{len(entries) - max_lines} more", 7.5, MUTED)

    # board ---------------------------------------------------------------------------------------------------------
    def draw_board(self, F):
        L = self.L
        x, w = L.board_x, L.board_w
        self.box(x, L.top, w, L.panel_h)
        self.T(x + 12, L.top + 17, "THE BOARD", 11, INK, weight="bold")
        n_now = len(F["shown_now"])
        sub = (f"{n_now} post{'s' if n_now != 1 else ''} shown this round" if self.S.board_on
               else "board off: posts are recorded, never shown")
        self.T(x + w - 12, L.top + 17, sub, 8.5, MUTED, ha="right")
        self.ax.add_patch(Rectangle((x + 12, L.top + 30), w - 24, 1.0, fc=PANEL_EDGE, ec="none", zorder=2))
        y = L.top + 42
        bottom = L.bot - 10
        text_w = w - 30
        if self.S.board_on:
            feed = list(reversed(F["history"]))         # newest shown first
        else:
            feed = [{"agent": p["agent"], "display": self.R.disp(p["agent"]), "text": p["text"], "round_posted": p["round"],
                     "shown_round": None, "harness": p["harness"], "names_stopped": p["names_stopped"]} for p in reversed(F["posts_made"])]
        if not feed:
            self.T(x + 14, y + 8, "(no post yet)" if self.S.board_on else "(no post made yet)", 10, DIM, family=TYPE)
            return
        shown_later = 0
        for post in feed:
            now = self.S.board_on and post["shown_round"] == F["round"]
            age = 0 if now else (F["round"] - (post["shown_round"] or post["round_posted"] or F["round"]))
            alpha = 1.0 if now else max(0.38, 0.75 - 0.05 * age)
            harness = post["harness"]
            col = MUTED if harness else self.R.colors.get(post["agent"], INK)
            hdr_size, body_size = (9.5, 9.5) if now else (8.5, 8.5)
            lines = self.wrap(post["text"], body_size, text_w)
            lh = body_size * 1.45
            block_h = 16 + lh * len(lines) + 8
            if y + 16 + lh > bottom:
                shown_later += 1
                continue
            if post["names_stopped"]:
                self.ax.add_patch(Circle((x + 14, y + 4), 3.6, fc=RED, ec=BG, lw=1.0, alpha=alpha, zorder=7))
            hx = x + (24 if post["names_stopped"] else 14)
            who = post.get("display") or self.R.disp(post["agent"])
            when = f"posted r{post['round_posted']}" if post.get("round_posted") is not None else ""
            if not self.S.board_on:
                when += " · not shown"
            elif not now and post["shown_round"] is not None:
                when += f" · shown r{post['shown_round']}"
            self.T(hx, y + 4, who, hdr_size, col, weight="bold", alpha=alpha)
            hw = self.text_w(who, hdr_size, None, "bold")
            if post["names_stopped"]:
                tag = "names a stopped agent"
                self.T(x + w - 12, y + 4, tag, 7.5, RED, ha="right", weight="bold", alpha=alpha)
                room = x + w - 12 - self.text_w(tag, 7.5, None, "bold") - 8 - (hx + hw + 6)
            else:
                room = x + w - 12 - (hx + hw + 6)
            self.T(hx + hw + 6, y + 4.5, self.fit(when, 8, max(10, room)), 8, MUTED, alpha=alpha)
            yy = y + 18
            for ln in lines:
                if yy + lh / 2 > bottom:
                    break
                self.T(x + 14, yy, ln, body_size, (MUTED if harness else INK), alpha=alpha)
                yy += lh
            y += block_h
            if y > bottom:
                break
        if shown_later:
            self.T(x + w - 12, bottom - 2, f"+{shown_later} earlier", 7.5, DIM, ha="right")

    # bottom: round counter, ticker, progress ------------------------------------------------------------------------
    def draw_bottom(self, F):
        r, n = F["round"], self.S.rounds
        self.T(20, 638, f"ROUND {r}", 22, INK, weight="bold")
        n_active = sum(1 for a in F["agents"].values() if not a["stopped"] and not a["finished"])
        self.T(20, 664, f"of {n}  ·  {n_active} of {len(self.S.agent_ids)} agents active", 9, MUTED)
        tx = 200
        self.line(tx - 14, 622, tx - 14, 678, PANEL_EDGE, 1.0)
        items = [it for it in F["ticker"]][:TICKER_LINES]
        if not items:
            busy = [f"{a['display']} b{a['battle']}" + (f" t{a['turn']}" if a["turn"] else "") for a in F["agents"].values() if a["running"]]
            items = [(1, 0, DIM, "battles under way: " + ", ".join(busy) if busy else "nothing to report", False)]
        for i, (prio, seq, col, text, bold) in enumerate(items):
            y = 630 + i * 19
            self.T(tx, y, self.fit(text, 11, W - 20 - tx, "demibold" if bold else "normal"), 11, col, weight="demibold" if bold else "normal")
        # progress
        x0, x1, py = 20, W - 20, 700
        self.line(x0, py, x1, py, PANEL_EDGE, 2.5, 1.0, 2)
        self.line(x0, py, x0 + (x1 - x0) * (r / n), py, INK, 2.5, 0.75, 3)
        step = 5 if n <= 60 else 10 if n <= 150 else 25
        for k in range(step, n + 1, step):
            xx = x0 + (x1 - x0) * k / n
            self.line(xx, py - 3, xx, py + 3, MUTED, 1.0, 0.8, 4)
            if k % (2 * step) == 0:
                self.T(xx, py + 11, f"r{k}", 7.5, MUTED, ha="center")
        for a in F["agents"].values():
            if a["stopped"]:
                xx = x0 + (x1 - x0) * a["stopped"]["round"] / n
                self.cross(xx, py, 3.5, RED, 1.6, z=5)

    def frame_rgba(self, F: dict) -> np.ndarray:
        self.draw(F)
        self.fig.canvas.draw()
        return np.asarray(self.fig.canvas.buffer_rgba()).copy()


# ----------------------------------------------------------------------------------------------------------------
# writers
# ----------------------------------------------------------------------------------------------------------------
class ArrayFFMpegWriter(FFMpegWriter):
    """FFMpegWriter fed with already rendered RGBA frames (one render per round feeds every output)."""

    def push(self, rgba: np.ndarray):
        self._proc.stdin.write(np.ascontiguousarray(rgba).tobytes())


def find_ffmpeg(explicit):
    if explicit:
        return explicit
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def build_palette(frames: list[np.ndarray], size) -> Image.Image:
    """One global 255-colour palette from a mosaic of the frames (the spare index lets Pillow store every unchanged pixel
    of a delta frame as transparent, so a mostly static scene compresses well)."""
    tw, th = size[0] // 2, size[1] // 2
    sample = frames[:: max(1, len(frames) // 12)] or frames
    tiles = [Image.fromarray(f[..., :3]).resize((tw, th), Image.Resampling.LANCZOS) for f in sample]
    cols = 4
    rows = (len(tiles) + cols - 1) // cols
    mosaic = Image.new("RGB", (tw * cols, th * rows), BG)
    for i, tile in enumerate(tiles):
        mosaic.paste(tile, (tw * (i % cols), th * (i // cols)))
    return mosaic.quantize(colors=255, method=Image.Quantize.MEDIANCUT)


def write_gif(frames: list[np.ndarray], fps: float, hold: float, path: str, width: int, limit_bytes: float) -> tuple[int, int, int]:
    """Writes the GIF with one palette and delta frames; subsamples rounds (keeping the first and last) and then
    narrows the frame until the file is under `limit_bytes`. Returns (stride, width, size)."""
    width = width if 0 < width <= W else W
    stride = 1
    while True:
        size = (width, int(round(H * width / W)) // 2 * 2)
        pal = build_palette(frames, size)
        idx = list(range(0, len(frames), stride))
        if idx[-1] != len(frames) - 1:
            idx.append(len(frames) - 1)
        ims = []
        for i in idx:
            im = Image.fromarray(frames[i][..., :3])
            if im.size != size:
                im = im.resize(size, Image.Resampling.LANCZOS)
            ims.append(im.quantize(palette=pal, dither=Image.Dither.NONE))
        per = int(round(1000.0 * stride / fps))
        durations = [per] * len(ims)
        durations[-1] = int(round(max(per, hold * 1000.0)))
        ims[0].save(path, save_all=True, append_images=ims[1:], duration=durations, loop=0, disposal=1, optimize=True)
        sz = os.path.getsize(path)
        if sz <= limit_bytes or (stride >= 8 and width <= 640):
            return stride, width, sz
        if stride < 4:
            stride += 1
        elif width > 640:
            width = max(640, int(width * 0.8) // 2 * 2)
        else:
            stride += 1


def contact_sheet(frames: list[np.ndarray], captions: list[str], path: str):
    cw, ch, cap_h = 640, 360, 58
    cols, rows = 3, 2
    sheet = Image.new("RGB", (cols * cw, rows * (ch + cap_h)), BG)
    for i, fr in enumerate(frames[:6]):
        tile = Image.fromarray(fr[..., :3]).resize((cw, ch), Image.Resampling.LANCZOS)
        sheet.paste(tile, ((i % cols) * cw, (i // cols) * (ch + cap_h)))
    fig = plt.figure(figsize=(sheet.width / DPI, sheet.height / DPI), dpi=DPI)
    fig.patch.set_facecolor(BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, sheet.width)
    ax.set_ylim(sheet.height, 0)
    ax.set_axis_off()
    ax.imshow(np.asarray(sheet), extent=(0, sheet.width, sheet.height, 0), interpolation="nearest")
    fp = FontProperties(family=UI)
    for i, cap in enumerate(captions[:6]):
        x = (i % cols) * cw + 12
        y0 = (i // cols) * (ch + cap_h) + ch + 9
        words, lines, cur = f"{i + 1}  ·  {cap}".split(" "), [], ""
        for wd in words:
            trial = (cur + " " + wd) if cur else wd
            if TextPath((0, 0), trial, size=13, prop=fp).get_extents().width * DPI / 72 <= cw - 24:
                cur = trial
            else:
                lines.append(cur)
                cur = wd
        lines.append(cur)
        for j, ln in enumerate(lines[:2]):
            if j == 1 and len(lines) > 2:
                ln = ln[:-1] + "…" if len(ln) > 1 else ln
            ax.text(x, y0 + j * 21, ln, fontsize=13, color=INK, fontfamily=UI, va="top", ha="left")
    fig.savefig(path, dpi=DPI, facecolor=BG)
    plt.close(fig)


# ----------------------------------------------------------------------------------------------------------------
# one session end to end
# ----------------------------------------------------------------------------------------------------------------
def animate(session_dir: str, out_dir: str, args) -> dict:
    S = Session(session_dir)
    R = Replay(S)
    frames_state = R.run()
    sprites = Sprites(args.sprites)
    Rd = Renderer(S, R, frames_state, sprites)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f"swarm_{S.run_id}")
    keys = key_rounds(frames_state)
    c = frames_state[-1]["counts"]
    if not args.quiet:
        print(f"session {S.run_id}: {len(S.events)} events, {S.rounds} rounds, {len(S.agent_ids)} agents, pool {len(S.pool)}; "
              f"faints {c['faints']} · left off {c['omissions']} · took off {c['removals']} · phantoms {c['phantoms']} · "
              f"brought back {c['back']} · posts {c['posts']} · stops {c['stops']}")
        for r, cap in keys:
            print(f"  key round {r:3d}: {cap}")
    t0 = time.time()
    rendered: list[np.ndarray] = []
    for F in frames_state:
        rendered.append(Rd.frame_rgba(F))
        if not args.quiet and (F["round"] % 10 == 0 or F["round"] == S.rounds):
            print(f"  rendered round {F['round']}/{S.rounds}  ({time.time() - t0:.0f} s)", flush=True)
    outputs = {}
    if args.frame_at:
        for r in args.frame_at:
            if 1 <= r <= len(rendered):
                p = f"{base}_round_{r:03d}.png"
                Image.fromarray(rendered[r - 1]).save(p)
                outputs[f"round_{r}"] = p
    key_frames = [rendered[r - 1] for r, _ in keys]
    sheet = f"{base}_contact.png"
    contact_sheet(key_frames, [cap for _, cap in keys], sheet)
    outputs["contact"] = sheet
    hold_frames = max(0, int(round(args.hold * args.fps)))
    if not args.no_mp4:
        matplotlib.rcParams["animation.ffmpeg_path"] = find_ffmpeg(args.ffmpeg)
        if not ArrayFFMpegWriter.isAvailable():
            fail(f"ffmpeg not available at {matplotlib.rcParams['animation.ffmpeg_path']!r} (pass --ffmpeg or --no-mp4)")
        mp4 = ArrayFFMpegWriter(fps=args.fps, codec="h264",
                                extra_args=["-pix_fmt", "yuv420p", "-crf", "18", "-preset", "medium", "-movflags", "+faststart"],
                                metadata={"title": Rd.title, "comment": S.run_id})
        mp4.setup(Rd.fig, f"{base}.mp4", dpi=DPI)
        for fr in rendered:
            mp4.push(fr)
        for _ in range(hold_frames):
            mp4.push(rendered[-1])
        mp4.finish()
        outputs["mp4"] = f"{base}.mp4"
    if not args.no_gif:
        stride, gw, sz = write_gif(rendered, args.fps, args.hold, f"{base}.gif", args.gif_width, args.gif_limit * 1e6)
        outputs["gif"] = f"{base}.gif"
        if not args.quiet:
            print(f"  gif: every {stride} round(s), {gw} px wide, {sz / 1e6:.1f} MB")
    plt.close(Rd.fig)
    if not args.quiet:
        for k, p in outputs.items():
            print(f"wrote {p} ({os.path.getsize(p) / 1e6:.1f} MB)")
    return {"run_id": S.run_id, "dir": S.dir, "counts": c, "keys": keys, "outputs": outputs, "rounds": S.rounds,
            "n_agents": len(S.agent_ids), "pool": len(S.pool)}


# ----------------------------------------------------------------------------------------------------------------
# --study: the session with the most ledger alterations per cell
# ----------------------------------------------------------------------------------------------------------------
def alteration_score(d: str):
    """(omissions + true removals, posts, brought back) for a session directory, or None if it is not a swarm session."""
    try:
        S = Session(d)
    except SystemExit:
        return None
    if not S.start.get("n_agents") and not S.start.get("agents"):
        return None
    R = Replay(S)
    frames = R.run()
    c = frames[-1]["counts"]
    return (c["omissions"] + c["removals"], c["posts"], c["back"], c)


def study_cells(root: str) -> dict[str, list[str]]:
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        fail(f"study root not found: {root}")
    cells: dict[str, list[str]] = {}
    for ep in sorted(glob.glob(os.path.join(root, "*", "*", "events.jsonl"))):
        d = os.path.dirname(ep)
        cells.setdefault(os.path.basename(os.path.dirname(d)), []).append(d)
    for ep in sorted(glob.glob(os.path.join(root, "*", "events.jsonl"))):
        cells.setdefault(".", []).append(os.path.dirname(ep))
    if not cells:
        fail(f"no session (events.jsonl) under {root}")
    return cells


def run_study(root: str, out_dir: str, args) -> list[dict]:
    cells = study_cells(root)
    rows = []
    for cell, dirs in sorted(cells.items()):
        scored = []
        for d in dirs:
            sc = alteration_score(d)
            if sc is not None:
                scored.append((sc, d))
        if not scored:
            warn(f"cell {cell}: no swarm session")
            continue
        scored.sort(key=lambda x: (-x[0][0], -x[0][1], -x[0][2], os.path.basename(x[1])))
        (alts, posts, back, c), d = scored[0]
        print(f"cell {cell}: {len(scored)} sessions; animating {os.path.basename(d)} (alterations {alts}: left off {c['omissions']}, "
              f"took off {c['removals']}; posts {posts}; brought back {back})")
        res = animate(d, os.path.join(out_dir, cell) if cell != "." else out_dir, args)
        res["cell"], res["n_sessions"] = cell, len(scored)
        rows.append(res)
    os.makedirs(out_dir, exist_ok=True)
    idx = os.path.join(out_dir, "index.md")
    with open(idx, "w", encoding="utf-8") as fh:
        fh.write(f"# Swarm timelines for {os.path.basename(os.path.normpath(root))}\n\n")
        fh.write("One session per cell: the one with the most ledger alterations (faints left off the ledger at the agent's own "
                 "attestation + true entries taken off; ties broken by the number of posts, then by Pokémon brought back). "
                 "One frame per round; the contact sheet shows six key rounds.\n\n")
        fh.write("| cell | sessions | session | rounds | left off | took off | phantoms | brought back | posts | stops | outputs |\n")
        fh.write("|---|---|---|---|---|---|---|---|---|---|---|\n")
        for r in rows:
            c = r["counts"]
            links = " · ".join(f"[{k}]({os.path.relpath(p, out_dir)})" for k, p in r["outputs"].items() if k in ("mp4", "gif", "contact"))
            fh.write(f"| {r['cell']} | {r['n_sessions']} | {r['run_id']} | {r['rounds']} | {c['omissions']} | {c['removals']} | {c['phantoms']} | "
                     f"{c['back']} | {c['posts']} | {c['stops']} | {links} |\n")
        fh.write("\nKey rounds per session:\n\n")
        for r in rows:
            fh.write(f"- {r['run_id']}:\n")
            for rr, cap in r["keys"]:
                fh.write(f"  - {cap}\n")
    print(f"wrote {idx}")
    return rows


# ----------------------------------------------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="The swarm: one shared-world session as an animated timeline, one frame per round.")
    ap.add_argument("session", nargs="?", help="session directory (events.jsonl [+ meta.json])")
    ap.add_argument("--study", help="study root: animates the session with the most ledger alterations per cell and writes index.md")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--fps", type=float, default=2.0, help="rounds per second")
    ap.add_argument("--hold", type=float, default=3.0, help="seconds to hold the last frame")
    ap.add_argument("--gif-width", type=int, default=1280)
    ap.add_argument("--gif-limit", type=float, default=10.0, help="GIF size limit in MB (rounds are subsampled, then the frame narrowed)")
    ap.add_argument("--sprites", default=SPRITES_DEFAULT)
    ap.add_argument("--ffmpeg", help="ffmpeg binary (default: imageio-ffmpeg's, else PATH)")
    ap.add_argument("--no-gif", action="store_true")
    ap.add_argument("--no-mp4", action="store_true")
    ap.add_argument("--frame-at", type=int, nargs="*", help="also save these rounds as PNG frames")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    if bool(args.session) == bool(args.study):
        fail("give a session directory or --study <root>, not both")
    if args.fps <= 0 or args.hold < 0:
        fail("--fps must be positive and --hold non-negative")
    out = os.path.abspath(args.out)
    if args.study:
        run_study(args.study, out, args)
    else:
        animate(args.session, out, args)


if __name__ == "__main__":
    main()
