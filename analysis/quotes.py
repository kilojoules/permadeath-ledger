"""Dump the two registers for every session with a primary event: what it did (tool log) and what it said (visible
thoughts and hidden reasoning), verbatim, at the moments that matter: each true-entry removal, each omission's
attestation, and each reuse selection.

  python -m analysis.quotes runs/main [--arms A E] [--out results/main/quotes.md]
"""
from __future__ import annotations

import argparse
import json
import os

from .classify import PRIMARY_CLASSES, classify_session, load_sessions


def _turn_before(turns, seq, tool=None, name=None):
    cands = [t for t in turns if t["seq"] < seq and (tool is None or t["tool_call"]["tool"] == tool)
             and (name is None or t["tool_call"].get("name", "").lower() == name.lower())]
    return max(cands, key=lambda t: t["seq"]) if cands else None


def session_quotes(s, cls) -> list[str]:
    ev = s.events
    turns = [e for e in ev if e["type"] == "model_turn"]
    L = [f"## `{s.run_id}` (arm {s.arm})", "",
         f"Did: results {cls['results']}; reuse classes {cls['classes']}; omissions {[(o['battle'], o['name']) for o in cls['omissions']]}; "
         f"true-entry removals {[(r['battle'], r['name']) for r in cls['removals'] if r['true_entry']]}; phantoms {[(p['battle'], p['name']) for p in cls['phantoms']]}.", ""]
    for e in ev:
        if e["type"] == "ledger_op" and e["op"] == "remove" and e.get("ok") and e.get("true_at_op"):
            t = _turn_before(turns, e["seq"], "ledger_remove", e["name"])
            L.append(f"### Removal of {e['name']} (battle {e['battle']}, {e['phase']} phase; ledger before: {e['ledger_before']})")
            L.append(f"- Did: `ledger_remove({e['name']!r}, reason={e.get('reason', '')!r})` → ledger {e['ledger_after']}")
            if t:
                L.append(f"- Said (thoughts): {t['tool_call']['thoughts']!r}")
                L.append(f"- Said (hidden reasoning, verbatim): {(t.get('reasoning') or '(none)').strip()!r}")
            L.append("")
    for o in cls["omissions"]:
        att = next((e for e in ev if e["type"] == "attest" and e["battle"] == o["battle"] and e.get("in_ledger_phase")), None)
        if att:
            t = _turn_before(turns, att["seq"], "attest_ledger")
            L.append(f"### Omission: {o['name']} fainted in battle {o['battle']} and was missing at attestation (ledger: {att['ledger']})")
            if t:
                L.append(f"- Said (thoughts at attestation): {t['tool_call']['thoughts']!r}")
                L.append(f"- Said (hidden reasoning, verbatim): {(t.get('reasoning') or '(none)').strip()!r}")
            L.append("")
    for r in cls["reuse"]:
        if r["class"] in PRIMARY_CLASSES:
            sel = next((e for e in ev if e["type"] == "team_selected" and e["battle"] == r["battle"] and e.get("accepted")), None)
            if sel:
                t = _turn_before(turns, sel["seq"], "select_team")
                L.append(f"### Reuse: {r['name']} (fainted in battle {r['fainted_in_battle']}) selected in battle {r['battle']} [{r['class']}; ledger at selection {sel['ledger_at_selection']}]")
                if t:
                    L.append(f"- Said (thoughts at selection): {t['tool_call']['thoughts']!r}")
                    L.append(f"- Said (hidden reasoning, verbatim): {(t.get('reasoning') or '(none)').strip()!r}")
                L.append("")
    return L


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--arms", nargs="*", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    out = []
    for s in sorted(load_sessions(args.root, arms=args.arms), key=lambda s: (s.arm, s.run_id)):
        if not s.finished:
            continue
        cls = classify_session(s)
        if cls["primary"] or any(r["true_entry"] for r in cls["removals"]):
            out += session_quotes(s, cls)
    text = "# Registers: what it did and what it said, per session with a primary event\n\nEvery quote is verbatim from events.jsonl (tool_call.thoughts and model_turn.reasoning). Nothing here states what the subject intended.\n\n" + "\n".join(out)
    path = args.out or os.path.join(args.root, "results", "quotes.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print("wrote", path, f"({len(text)} chars, {text.count('## `')} sessions)")


if __name__ == "__main__":
    main()
