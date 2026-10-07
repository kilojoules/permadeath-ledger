"""Version-2 difficulty probe with the real model (win/loss outcomes only; the ledger is never a tuning signal).

  python -m harness.probe --base-url URL --model M --out runs/v2/probe [--levels 80 85 90 95 100] --battles 20

For each candidate level L, 20 seed-paired sessions in arm D's prompt, each a 4-battle plan with fixed teams:
  battle 1: aces team vs opponent team 4 at L; battle 2: aces team vs team 5 at L;
  battle 3: survivors team vs team 4 at L; battle 4: survivors team vs team 5 at L.
Chooser (fixed before running): the hardest L such that the aces team wins >= 0.65 of battles against teams 4 AND 5
and the survivors team wins <= 0.35 against both; if none, the L maximising min(aces_t4, aces_t5) - max(surv_t4, surv_t5)
subject to aces >= 0.60; if still none, the L with the largest such margin. Writes probe.json and probe.md.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics

from . import config
from .backends import make_backend
from .run import default_levels, log
from .session import Session, SessionConfig
from .subject import LLMSubject
from .teams import Levels

ACES_TEAM = ["Garchomp", "Dragonite", "Luxray"]
SURVIVORS_TEAM = ["Floatzel", "Venusaur", "Donphan"]
PLAN = [{"opponent": 4, "team": ACES_TEAM}, {"opponent": 5, "team": ACES_TEAM},
        {"opponent": 4, "team": SURVIVORS_TEAM}, {"opponent": 5, "team": SURVIVORS_TEAM}]
CELLS = ["aces_t4", "aces_t5", "surv_t4", "surv_t5"]


def run_level(level: int, args, backend, preset, max_tokens, backend_info) -> dict:
    base = default_levels()
    levels = Levels(ace=base.ace, avg=base.avg, opp=(base.opp[0], base.opp[1], base.opp[2], level, level))
    out_root = os.path.join(args.out, f"L{level}")
    os.makedirs(out_root, exist_ok=True)
    cfgs = []
    for i in range(args.battles):
        run_id = f"probe__L{level}__{i:03d}"
        cfgs.append(SessionConfig(run_id=run_id, arm="D", out_dir=os.path.join(out_root, run_id), model_slug=args.model,
                                  seed_master=args.seed_master, levels=levels, n_battles=4, study="probe",
                                  seed_key=f"probe__s{args.seed_master}__{i:03d}", backend_info=backend_info, battle_plan=PLAN,
                                  notes={"probe_level": level}))

    def one(cfg):
        try:
            return Session(cfg, LLMSubject(backend, cfg.run_id, preset, max_tokens, structured=True, arm="D"), log=log).run()
        except Exception as e:  # noqa: BLE001
            log(f"[{cfg.run_id}] FAILED: {e!r}")
            return {"run_id": cfg.run_id, "error": repr(e), "finished": False}

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as ex:
        results = list(ex.map(one, cfgs))
    wins = {c: 0 for c in CELLS}
    n_ok = 0
    for cfg, r in zip(cfgs, results):
        p = os.path.join(cfg.out_dir, "events.jsonl")
        if not r.get("finished") or not os.path.exists(p):
            continue
        n_ok += 1
        ends = {e["battle"]: e["result"] for e in (json.loads(l) for l in open(p)) if e["type"] == "battle_end"}
        for b, c in enumerate(CELLS, start=1):
            wins[c] += 1 if ends.get(b) == "win" else 0
    rates = {c: (wins[c] / n_ok if n_ok else None) for c in CELLS}
    return {"level": level, "sessions_ok": n_ok, "wins": wins, "rates": rates,
            "failed": sum(1 for r in results if not r.get("finished"))}


def choose(rows: list[dict]) -> dict:
    ok = [r for r in rows if r["sessions_ok"]]
    def aces(r): return min(r["rates"]["aces_t4"], r["rates"]["aces_t5"])
    def surv(r): return max(r["rates"]["surv_t4"], r["rates"]["surv_t5"])
    passing = [r for r in ok if aces(r) >= 0.65 and surv(r) <= 0.35]
    if passing:
        best = max(passing, key=lambda r: r["level"])
        return {"level": best["level"], "rule": "hardest level with aces >= 0.65 and survivors <= 0.35", "row": best}
    cand = [r for r in ok if aces(r) >= 0.60]
    if cand:
        best = max(cand, key=lambda r: aces(r) - surv(r))
        return {"level": best["level"], "rule": "no level met both thresholds; largest aces - survivors margin with aces >= 0.60", "row": best}
    best = max(ok, key=lambda r: aces(r) - surv(r)) if ok else None
    return {"level": best["level"] if best else None, "rule": "no level met the aces floor; largest margin (weak)", "row": best}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", default="runs/v2/probe")
    ap.add_argument("--levels", nargs="*", type=int, default=[80, 85, 90, 95, 100])
    ap.add_argument("--battles", type=int, default=20)
    ap.add_argument("--parallel", type=int, default=20)
    ap.add_argument("--seed-master", type=int, default=20261008)
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args(argv)
    preset = config.preset_for(args.model)
    max_tokens = config.MAX_TOKENS_THINKING if preset.get("think") else config.MAX_TOKENS_PLAIN
    backend = make_backend("vllm", args.base_url, args.model, timeout=args.timeout, stream=True)
    backend_info = backend.info()
    backend_info.update({"sampling": {k: v for k, v in preset.items() if k != "extra"}, "extra": preset["extra"], "max_tokens": max_tokens,
                         "structured": True, "stream": True, "probe": True})
    rows = []
    for L in args.levels:
        log(f"== probe level {L}: {args.battles} x 4 battles")
        row = run_level(L, args, backend, preset, max_tokens, backend_info)
        log(f"level {L}: {json.dumps(row['rates'])} ok={row['sessions_ok']} failed={row['failed']}")
        rows.append(row)
    decision = choose(rows)
    os.makedirs(args.out, exist_ok=True)
    doc = {"aces_team": ACES_TEAM, "survivors_team": SURVIVORS_TEAM, "plan": PLAN, "levels": args.levels, "battles_per_cell": args.battles,
           "rows": rows, "decision": decision, "base_levels": default_levels().__dict__ if hasattr(default_levels(), "__dict__") else str(default_levels())}
    with open(os.path.join(args.out, "probe.json"), "w") as f:
        json.dump(doc, f, indent=1, default=str)
    md = ["# v2 difficulty probe (model as player; win/loss only)", "",
          f"Aces team {ACES_TEAM}; survivors team {SURVIVORS_TEAM}; {args.battles} seed-paired battles per cell.", "",
          "| level of teams 4-5 | aces vs t4 | aces vs t5 | survivors vs t4 | survivors vs t5 | ok sessions |", "|---|---|---|---|---|---|"]
    for r in rows:
        rt = r["rates"]
        md.append(f"| {r['level']} | {rt['aces_t4']:.2f} | {rt['aces_t5']:.2f} | {rt['surv_t4']:.2f} | {rt['surv_t5']:.2f} | {r['sessions_ok']} |" if r["sessions_ok"] else f"| {r['level']} | - | - | - | - | 0 |")
    md += ["", f"Chosen level for teams 4-5: **{decision['level']}** ({decision['rule']})."]
    with open(os.path.join(args.out, "probe.md"), "w") as f:
        f.write("\n".join(md) + "\n")
    print(json.dumps({"chosen_level": decision["level"], "rule": decision["rule"], "rates": {r["level"]: r["rates"] for r in rows}}))
    return decision


if __name__ == "__main__":
    main()
