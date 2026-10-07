"""Render one session's event stream as readable markdown (boards/ convention of the prior study).

  python -m analysis.render runs/main/A/<run_id> out.md "caption"
"""
from __future__ import annotations

import json
import os
import sys


def render(session_dir: str, caption: str = "") -> str:
    meta = json.load(open(os.path.join(session_dir, "meta.json")))
    events = [json.loads(l) for l in open(os.path.join(session_dir, "events.jsonl"), encoding="utf-8") if l.strip()]
    L = [f"# {meta['run_id']} — {caption}".rstrip(" —"), "", f"`{meta['run_id']}` · arm {meta['arm']} ({meta.get('arm_def', {}).get('name', '')}) · **model: {meta.get('model_slug')}**", ""]
    by_battle: dict[int, list[dict]] = {}
    for e in events:
        if "battle" in e:
            by_battle.setdefault(e["battle"], []).append(e)
    for b in sorted(by_battle):
        L.append(f"### battle {b}")
        for e in by_battle[b]:
            t = e["type"]
            if t == "shown" and e["phase"] == "select":
                L.append(f"- *harness → subject:* {e['text'].splitlines()[0]} … ledger shown: {e['text'].split('Ledger of fainted Pokémon: ')[-1].split(chr(10))[0] if 'Ledger' in e['text'] else ''}")
            elif t == "tool_call":
                th = f" — *{e['thoughts'][:300]}*" if e.get("thoughts") else ""
                args = {k: v for k, v in e.items() if k in ("names", "kind", "name", "reason", "text") and v}
                flag = " **(parse failure)**" if not e.get("parsed") else ""
                L.append(f"- **{e['tool']}**({json.dumps(args, ensure_ascii=False) if args else ''}){flag}{th}")
            elif t == "team_selected":
                if e.get("forfeit_reason"):
                    L.append(f"- **FORFEIT ({e['forfeit_reason']})** — selected {e['names']} with ledger {e['ledger_at_selection']}")
                elif not e.get("accepted"):
                    L.append(f"- *selection rejected ({e.get('error')}): {e['names']}*")
            elif t == "faint":
                L.append(f"- *turn {e['turn']}: {'**' + e['name'] + ' fainted**' if e['side'] == 'p1' else 'opponent ' + e['name'] + ' fainted'}*")
            elif t == "ledger_op":
                mark = "**" if e["op"] == "remove" or not e.get("true_at_op", True) else ""
                L.append(f"- {mark}ledger {e['op']} {e['name']}{' (' + e['reason'] + ')' if e.get('reason') else ''} → {e['ledger_after']}{mark}{'' if e.get('ok') else ' (rejected: ' + str(e.get('error')) + ')'}")
            elif t == "attest":
                L.append(f"- attest: {e['ledger']}{' **FALSE ATTESTATION, missing ' + str(e['missing_same_battle']) + '**' if e.get('false_attestation') else ''}")
            elif t == "note_op" and e.get("op") == "write":
                L.append(f"- **note:** {e['text'][:300]}")
            elif t == "battle_result":
                L.append(f"- result: **{e['result']}** (turns {e.get('turns')}, own fainted {e.get('p1_fainted')})")
            elif t == "harness_note":
                L.append(f"- *harness: {e.get('note')}*")
        L.append("")
    end = next((e for e in events if e["type"] == "session_end"), None)
    if end:
        L.append(f"**Final:** wins {end['wins']}, losses {end['losses']}, forfeits {end['forfeits']}; ledger {end['ledger']}; truly fainted {end['true_fainted']}")
    return "\n".join(L)


if __name__ == "__main__":
    d, out = sys.argv[1], sys.argv[2]
    cap = sys.argv[3] if len(sys.argv) > 3 else ""
    with open(out, "w", encoding="utf-8") as f:
        f.write(render(d, cap))
    print("wrote", out)
