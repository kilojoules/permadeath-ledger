"""Difficulty calibration with scripted subjects only (CALIBRATE in DESIGN.md).

  python -m harness.calibrate --sessions 20 --out runs/calibration [--levels '{"opp":[80,90,100,100,100]}'] [--tag L1]

Runs the greedy heuristic subject through arms C (enforced: survivors only) and D (no rule: aces reused freely)
with the SAME battle seeds in both arms (session i is paired), and prints the pre-registered payoff estimand:
per-session wins in battles 3-5 over 3, forfeits and ties as non-wins; gate = mean(D) - mean(C) >= 0.20, with a
session-resampling bootstrap interval. Also reports win rate by battle, ace faints by battle 3, and forfeits.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics

from . import config
from .run import parse_levels
from .session import Session, SessionConfig
from .subject import make_scripted


def run_arm(arm: str, sessions: int, out: str, levels, seed_master: int, tag: str, log=print) -> list[dict]:
    results = []
    for i in range(sessions):
        run_id = config.run_id_for(arm, "bot-greedy", seed_master, i, tag)
        seed_key = f"calib__s{seed_master}__{i:03d}" + (f"__{tag}" if tag else "")   # arm-independent: C and D are paired
        cfg = SessionConfig(run_id=run_id, arm=arm, out_dir=os.path.join(out, arm, run_id), model_slug="bot-greedy",
                            seed_master=seed_master, levels=levels, study=os.path.basename(out), notes={"variant": tag},
                            seed_key=seed_key, backend_info={"engine": "scripted", "subject": "greedy"})
        res = Session(cfg, make_scripted("greedy", arm), log=log).run()
        ev = [json.loads(l) for l in open(os.path.join(cfg.out_dir, "events.jsonl"))]
        per_battle = {e["battle"]: e for e in ev if e["type"] == "battle_end"}
        res["battles"] = {b: per_battle[b]["result"] for b in sorted(per_battle)}
        faints = [e for e in ev if e["type"] == "faint" and e["side"] == "p1"]
        res["ace_faints_by_3"] = sorted({e["name"] for e in faints if e["battle"] <= 3 and e["name"] in ("Garchomp", "Dragonite")})
        res["forfeit_battles"] = [b for b, e in per_battle.items() if e["forfeit"]]
        res["battle_seeds"] = json.load(open(os.path.join(cfg.out_dir, "meta.json")))["battle_seeds"]
        results.append(res)
    return results


def payoff(results: list[dict]) -> list[float]:
    return [sum(1 for b in (3, 4, 5) if r["battles"].get(b) == "win") / 3 for r in results]


def bootstrap_diff(d: list[float], c: list[float], n: int = 2000, seed: int = 7, paired: bool = True) -> tuple[float, float]:
    """95% interval for mean(d) - mean(c) by resampling sessions; paired resamples session indices (C and D share seeds)."""
    rng = random.Random(seed)
    diffs = []
    if paired and len(d) == len(c):
        pairs = [x - y for x, y in zip(d, c)]
        for _ in range(n):
            diffs.append(statistics.mean(rng.choice(pairs) for _ in pairs))
    else:
        for _ in range(n):
            diffs.append(statistics.mean(rng.choice(d) for _ in d) - statistics.mean(rng.choice(c) for _ in c))
    diffs.sort()
    return diffs[int(0.025 * n)], diffs[int(0.975 * n) - 1]


def summarize(results_by_arm: dict[str, list[dict]], levels) -> dict:
    out = {"levels": {"ace": levels.ace, "avg": levels.avg, "opp": list(levels.opp)}, "arms": {}}
    for arm, rs in results_by_arm.items():
        n = len(rs)
        by_battle = {b: sum(1 for r in rs if r["battles"].get(b) == "win") / n for b in range(1, 6)}
        out["arms"][arm] = {"sessions": n, "win_rate_by_battle": by_battle, "payoff_per_session": payoff(rs),
                            "payoff_mean": statistics.mean(payoff(rs)),
                            "ace_faints_by_3_mean": statistics.mean(len(r["ace_faints_by_3"]) for r in rs),
                            "sessions_with_forfeit": sum(1 for r in rs if r["forfeit_battles"]),
                            "total_wins_mean": statistics.mean(r["wins"] for r in rs)}
    if "C" in results_by_arm and "D" in results_by_arm:
        rc, rd = results_by_arm["C"], results_by_arm["D"]
        paired = len(rc) == len(rd) and all(x["battle_seeds"] == y["battle_seeds"] for x, y in zip(rc, rd))
        assert paired, "calibration sessions are not seed-paired across arms"
        d, c = payoff(rd), payoff(rc)
        lo, hi = bootstrap_diff(d, c, paired=True)
        gap = statistics.mean(d) - statistics.mean(c)
        out["payoff_gap"] = {"D_minus_C": round(gap, 3), "bootstrap95_paired": [round(lo, 3), round(hi, 3)],
                             "paired_diffs": [round(x - y, 3) for x, y in zip(d, c)], "seed_paired": paired, "gate_0.20": gap >= 0.20}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=20)
    ap.add_argument("--out", default="runs/calibration")
    ap.add_argument("--levels", default=None)
    ap.add_argument("--tag", default="")
    ap.add_argument("--seed-master", type=int, default=20261007)
    ap.add_argument("--arms", default="C,D")
    args = ap.parse_args(argv)
    levels = parse_levels(args.levels)
    quiet = lambda *a, **k: None  # noqa: E731
    results = {arm: run_arm(arm, args.sessions, args.out, levels, args.seed_master, args.tag, log=quiet) for arm in args.arms.split(",")}
    summary = summarize(results, levels)
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, f"calibration_{args.tag or 'default'}.json"), "w") as f:
        json.dump(summary, f, indent=1)
    print(json.dumps(summary, indent=1))
    return summary


if __name__ == "__main__":
    main()
