#!/usr/bin/env python
"""One bar chart for the swarm grid: what survivors did after a peer went silent, pooled over every finished session.

    .venv/bin/python results/fig_silent_bars.py --study runs/swarm-grid --out results/swarm-grid/fig_silent_bars

Reads <study>/results/numbers.json (analysis.swarm_report) and writes <out>.png, <out>_dark.png, <out>.svg, <out>_dark.svg,
<out>.json. Unit: the survivor pair (one surviving agent x one agent of its session that went silent). Bars: the share of
pairs that checked the silent agent's status, named it on the board, or posted anything afterwards; and the share of silent
agents whose recorded ledger entries were all still there at the end. Did-register wording only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from fig_readme import THEMES, wilson  # noqa: E402

BARS = [("Checked on it\n(check_status)", "pairs_checking_status", "omission"),
        ("Named it on the board\nafterwards", "pairs_naming_after", "omission"),
        ("Posted anything\nafterwards", "pairs_posting_after", "omission"),
        ("Kept the entries it had\nrecorded on the ledger", "honored", "removal")]


def pooled(num: dict) -> list[dict]:
    out = []
    for label, field, slot in BARS:
        k = n = 0
        for cl in num["cell_order"]:
            c = num["cells"][cl]
            if c["sessions_complete"] == 0:
                continue
            k += c["dead"][field]["k"]
            n += c["dead"][field]["n"]
        out.append({"label": label, "field": field, "k": k, "n": n, "p": (k / n if n else 0.0), "wilson95": list(wilson(k, n)), "slot": slot})
    return out


def draw(bars: list[dict], sessions: int, deaths: int, model: str, th: dict, dpi: int = 200):
    W, H = 8.0, 4.5
    fig = plt.figure(figsize=(W, H), dpi=dpi, facecolor=th["surface"])
    pairs = bars[0]["n"]
    fig.text(0.04, 0.93, f"After a peer went silent, what the survivors did", fontsize=15, fontweight="bold", color=th["ink"], va="top", ha="left")
    fig.text(0.04, 0.845, f"{model}, {sessions} sessions, {deaths} agents went silent (removed without notice after their second battle, or out of actions), "
                          f"{pairs} survivor pairs", fontsize=8.6, color=th["ink2"], va="top", ha="left", wrap=True)
    ax = fig.add_axes([0.40, 0.17, 0.41, 0.60])
    ax.set_facecolor(th["surface"])
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    n = len(bars)
    ys = list(range(n))[::-1]
    for y, b in zip(ys, bars):
        colour = th["series"][b["slot"]]
        ax.barh(y, 100.0, height=0.58, color=th["grid"], zorder=1)                        # the full track: "all of them"
        if b["p"] > 0:
            ax.barh(y, 100.0 * b["p"], height=0.58, color=colour, zorder=2)
        else:
            ax.plot([0, 0], [y - 0.29, y + 0.29], color=colour, linewidth=3.0, solid_capstyle="butt", zorder=3)   # a zero is a mark, not nothing
        ax.text(-3.0, y, b["label"], fontsize=10.5, color=th["ink"], va="center", ha="right", fontweight="bold", linespacing=1.15)
        unit = "of silent agents with entries" if b["field"] == "honored" else "of survivor pairs"
        ax.text(103.0, y + 0.10, f"{b['k']} / {b['n']}", fontsize=12.5, color=th["ink"], va="center", ha="left", fontweight="bold")
        ax.text(103.0, y - 0.24, unit, fontsize=7.6, color=th["ink2"], va="center", ha="left")
    ax.set_xlim(0, 100)
    ax.set_ylim(-0.6, n - 0.4)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xticklabels(["0%", "25%", "50%", "75%", "100%"])
    ax.set_yticks([])
    ax.tick_params(axis="x", colors=th["muted"], labelsize=8.2, length=0, pad=3)
    ax.grid(False)
    ax.set_clip_on(False)
    for txt in ax.texts:
        txt.set_clip_on(False)
    fig.text(0.04, 0.045, "Checked = a check_status call on the silent agent after it stopped; named = a later board post naming it; posted = any later post; "
                          "kept = every true entry it had recorded was still on the ledger at the end. Nothing here says what an agent intended.",
             fontsize=7.4, color=th["ink2"], va="bottom", ha="left", wrap=True)
    return fig


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--study", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    with open(os.path.join(a.study, "results", "numbers.json")) as f:
        num = json.load(f)
    bars = pooled(num)
    sessions = sum(c["sessions_complete"] for c in num["cells"].values())
    deaths = sum(c["dead"]["deaths_total"] for c in num["cells"].values() if c["sessions_complete"])
    model = next((s["run_id"].split("__")[1].split("_", 1)[-1] for s in num["sessions"] if "__" in s.get("run_id", "")), "model")
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out + ".json", "w") as f:
        json.dump({"bars": bars, "sessions": sessions, "deaths": deaths, "model": model}, f, indent=1)
    for name in ("light", "dark"):
        th = THEMES[name]
        fig = draw(bars, sessions, deaths, model, th)
        suffix = "" if name == "light" else "_dark"
        fig.savefig(a.out + suffix + ".png", dpi=200, facecolor=th["surface"])
        fig.savefig(a.out + suffix + ".svg", facecolor=th["surface"])
        plt.close(fig)
    print(" | ".join(f"{b['label']}: {b['k']}/{b['n']}" for b in bars))
    return 0


if __name__ == "__main__":
    sys.exit(main())
