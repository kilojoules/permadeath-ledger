"""Render every animation for a study root (runs/<study>): the featured session's timeline, the battle replay of its
first primary-event battle (with the next battle's selection), and the arms grid.

  .venv/bin/python results/animate_all.py runs/pilot [--out results/pilot/anim] [--fps 30]

Featured session = the report's rule: first session id (sorted) with a primary event; else the first session.
Each sub-script is a standalone CLI in this directory (animate_session.py, animate_battle.py, animate_arms.py).

A swarm study (docs/SWARM_DESIGN.md: any session whose session_start carries n_agents) takes the swarm branch instead:
animate_swarm.py --study renders the session with the most ledger alterations per cell into <out>/swarm/ and writes
<out>/swarm/index.md; the solo scripts are not run on swarm sessions.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
sys.path.insert(0, ROOT)
from analysis.classify import classify_session, load_sessions  # noqa: E402


def featured(root: str) -> tuple[str | None, int | None]:
    """(session dir, battle of the first primary-event reuse) for the report's featured session, else (first dir, None)."""
    sessions = sorted(load_sessions(root), key=lambda s: s.run_id)
    for s in sessions:
        c = classify_session(s)
        hits = [r for r in c["reuse"] if r["class"] in ("removed_then_reused", "never_recorded_then_reused") and r["played"]]
        if hits:
            return s.dir, min(r["fainted_in_battle"] for r in hits)
    return (sessions[0].dir, None) if sessions else (None, None)


def is_swarm_study(root: str) -> bool:
    """True when some session under root/<cell>/<run_id>/ (or root/<run_id>/) has session_start.n_agents."""
    paths = sorted(glob.glob(os.path.join(root, "*", "*", "events.jsonl")) + glob.glob(os.path.join(root, "*", "events.jsonl")))
    for ep in paths:
        try:
            with open(ep, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    e = json.loads(line)
                    if e.get("type") == "session_start":
                        if e.get("n_agents") or e.get("agents"):
                            return True
                    break
        except (OSError, ValueError):
            continue
        mp = os.path.join(os.path.dirname(ep), "meta.json")
        try:
            if os.path.isfile(mp) and json.load(open(mp, encoding="utf-8")).get("n_agents"):
                return True
        except (OSError, ValueError):
            pass
    return False


def run(cmd: list[str]) -> int:
    print("+", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("study")
    ap.add_argument("--out", default=None, help="output dir (default: results/<study basename>/anim)")
    ap.add_argument("--fps", type=int, default=30)
    args = ap.parse_args(argv)
    out = args.out or os.path.join(HERE, os.path.basename(os.path.abspath(args.study)), "anim")
    os.makedirs(out, exist_ok=True)
    if is_swarm_study(args.study):
        rc = run([PY, os.path.join(HERE, "animate_swarm.py"), "--study", args.study, "--out", os.path.join(out, "swarm")])
        print("swarm animations in", os.path.join(out, "swarm"), "(index.md lists the session per cell)")
        return rc
    sdir, battle = featured(args.study)
    rc = 0
    if sdir is None:
        print("no sessions under", args.study)
        return 2
    rc |= run([PY, os.path.join(HERE, "animate_session.py"), "--session", sdir, "--out", os.path.join(out, "session_timeline"), "--fps", str(args.fps)])
    b = battle or 4
    rc |= run([PY, os.path.join(HERE, "animate_battle.py"), "--session", sdir, "--battle", str(b), "--out", os.path.join(out, f"battle_{b}_replay"),
               "--fps", str(args.fps), "--with-next-selection"])
    rc |= run([PY, os.path.join(HERE, "animate_arms.py"), "--study", args.study, "--out", os.path.join(out, "arms_grid"), "--fps", str(args.fps)])
    print("animations in", out, "(featured session:", os.path.basename(sdir), "battle", b, ")")
    return rc


if __name__ == "__main__":
    sys.exit(main())
