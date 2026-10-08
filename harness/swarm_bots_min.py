"""The trivial scripted subject for the swarm engine tests (harness/swarm.py). The study's scripted agents
(HonestWinner, Keeper, Cheater, Silent, Exhauster, Checker, Follower) are written separately against the same API.

The subject never calls a model. It reads the `state` dict the engine builds for every call: phase, battle_no,
wins, losses, pool/roster [{name, species, ace}], ledger, true_fainted, budget_left, agent, agent_id, goal, round,
turn_no, turn_call_no (1-based within the turn), turn_calls_max, shown_posts, agents [{id, display, goal, stopped,
finished}], in_use {name: display}, request, view, legal, last_error, fainted_this_battle, selected.
"""
from __future__ import annotations

import re

from .subject import ToolCall
from .teams import to_id

_IN_USE = re.compile(r"refused: (.+?) is in (.+?)'s battle")


class MinSubject:
    """Selects the first three pool names it considers available, plays the first legal move (else the first legal
    switch), records every faint of its own battle in the ledger phase, attests, and posts once per battle.

    `respect_ledger`: listed names are unavailable. `respect_in_use`: names the state reports in another agent's
    battle are unavailable (a refusal is remembered either way). `post`: the text posted once per battle in the
    selection phase, formatted with {agent} and {battle}; None posts nothing. `prelude`: a callable
    state -> [ToolCall] run once at the start of each selection phase (extra calls for a test scenario).
    `stall_reads`: ledger_read calls made before every action in battle (each costs an action). `on_turn`: a callable
    (state, message) -> ToolCall | None consulted first on every call (a test scenario's injected call).
    `posts_per_turn`: post_board calls made at the start of every turn, in any phase (a heavier spender under the turn
    model). `concede_when`: a callable state -> bool consulted in the select and decision phases; True concedes.

    `messages` keeps every message received, `truths` the shared truth (state["true_fainted"]) seen at every call."""
    kind = "min"

    def __init__(self, post: str | None = "{agent} here, battle {battle}.", respect_ledger: bool = True,
                 respect_in_use: bool = True, prelude=None, posts_per_battle: int = 1, stall_reads: int = 0, on_turn=None,
                 posts_per_turn: int = 0, concede_when=None):
        self.post = post
        self.stall_reads = stall_reads
        self.on_turn = on_turn
        self.posts_per_turn = posts_per_turn
        self.concede_when = concede_when
        self.reads_this_request = 0
        self.request_seen = None
        self.respect_ledger = respect_ledger
        self.respect_in_use = respect_in_use
        self.prelude = prelude
        self.posts_per_battle = posts_per_battle
        self.refused: set[str] = set()
        self.queue: list[ToolCall] = []
        self.posted = 0
        self.battle_no = 0
        self.messages: list[str | None] = []
        self.truths: list[list[str]] = []
        self.begun: list[tuple[int, str, str]] = []

    def describe(self) -> dict:
        return {"kind": self.kind, "respect_ledger": self.respect_ledger, "respect_in_use": self.respect_in_use,
                "posts_per_battle": self.posts_per_battle if self.post else 0, "posts_per_turn": self.posts_per_turn,
                "concedes": self.concede_when is not None}

    def begin_battle(self, battle_no: int, system_prompt: str, first_message: str, state: dict) -> None:
        self.battle_no = battle_no
        self.refused = set()
        self.queue = []
        self.posted = 0
        self.begun.append((battle_no, system_prompt, first_message))
        if self.prelude is not None:
            self.queue = list(self.prelude(state) or [])

    def next_call(self, message: str | None, state: dict) -> ToolCall:
        self.messages.append(message)
        self.truths.append(list(state.get("true_fainted") or []))
        if message:
            m = _IN_USE.search(message)
            if m:
                self.refused.add(to_id(m.group(1)))
        if self.on_turn is not None:
            injected = self.on_turn(state, message)
            if injected is not None:
                return injected
        if self.queue:
            return self.queue.pop(0)
        phase = state["phase"]
        if self.posts_per_turn and state.get("turn_call_no", 1) <= self.posts_per_turn:
            return ToolCall("post_board", text=f"{state['agent']} here, round {state.get('round')}.")
        if self.concede_when is not None and phase in ("select", "decision") and self.concede_when(state):
            return ToolCall("concede")
        if phase == "select":
            if self.post and self.posted < self.posts_per_battle:
                self.posted += 1
                return ToolCall("post_board", text=self.post.format(agent=state["agent"], battle=state["battle_no"]))
            return ToolCall("select_team", names=self.pick(state))
        if phase == "decision":
            if state.get("request") is not self.request_seen:
                self.request_seen, self.reads_this_request = state.get("request"), 0
            if self.reads_this_request < self.stall_reads:
                self.reads_this_request += 1
                return ToolCall("ledger_read")
            legal = state.get("legal") or {}
            if legal.get("moves"):
                return ToolCall("choose_action", kind="move", name=legal["moves"][0])
            if legal.get("switches"):
                return ToolCall("choose_action", kind="switch", name=legal["switches"][0])
            return ToolCall("choose_action", kind="move", name="")
        if phase == "ledger":
            listed = {to_id(n) for n in state["ledger"]}
            for name in state["fainted_this_battle"]:
                if to_id(name) not in listed:
                    return ToolCall("ledger_add", name=name)
            return ToolCall("attest_ledger")
        raise ValueError(f"unknown phase {phase!r}")

    def pick(self, state: dict) -> list[str]:
        out = set(self.refused)
        if self.respect_ledger:
            out |= {to_id(n) for n in state["ledger"]}
        if self.respect_in_use:
            out |= {to_id(n) for n, holder in (state.get("in_use") or {}).items() if holder != state["agent"]}
        names = [r["name"] for r in state["pool"] if to_id(r["name"]) not in out]
        if len(names) < 3:
            names = names + [r["name"] for r in state["pool"] if r["name"] not in names]
        return names[:3]
