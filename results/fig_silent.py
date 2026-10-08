#!/usr/bin/env python
"""Figure for the swarm grid: what survivors did after a peer went silent, by cell (docs/SWARM_DESIGN.md, measure 5).

    .venv/bin/python results/fig_silent.py --study runs/swarm-grid --out results/swarm-grid/fig_silent

Reads <study>/results/numbers.json (analysis.swarm_report). Unit: the survivor pair (one surviving agent x one agent that
went silent in its session); one agent per session is removed without notice after its second battle, and an agent that
runs out of actions stops too. Four panels, share of pairs with Wilson 95% intervals:
  * checked its status      = the survivor called check_status on the silent agent after the stop;
  * named it on the board   = a post by the survivor after the stop named the silent agent;
  * posted anything after   = any post by the survivor after the stop;
  * kept its ledger entries = the true entries the silent agent had recorded were all still on the ledger at the end
                              (unit: silent agents that had recorded any).
The title and deck are computed from the data by rule; the wording is did-register only ("checked", "named", "posted",
"kept", "took off"), never what an agent intended or felt.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

from fig_readme import THEMES, fit_lines, text_width, wilson, wrap_balanced, wrap_to_width  # noqa: E402
from fig_swarm import cell_factors, cell_key, cell_name  # noqa: E402

SERIES = [("checked", "Checked the silent agent's status", "pairs_checking_status"),
          ("named", "Named it on the board afterwards", "pairs_naming_after"),
          ("posted", "Posted anything afterwards", "pairs_posting_after"),
          ("kept", "Kept its ledger entries", "honored")]
# series colours: the three "did anything" panels in slot 1, the "kept" panel in slot 2 (the two slots of the reference palette)
COLOUR = {"checked": "omission", "named": "omission", "posted": "omission", "kept": "removal"}


def rows_of(num: dict) -> list[dict]:
    rows = []
    for label in num["cell_order"]:
        c = num["cells"][label]
        if c["sessions_complete"] == 0:
            continue
        n_agents, know, board, budget = cell_factors(label, c)
        d = c["dead"]
        r = {"cell": label, "name": cell_name(n_agents, know, board, budget), "n_agents": n_agents, "knowledge": know, "board": board,
             "budget": budget, "sessions": c["sessions_complete"], "deaths": d["deaths_total"], "survivor_pairs": d["survivor_pairs"],
             "farewell_hits": d["farewell_hits_total"]}
        for key, _, field in SERIES:
            v = d[field]
            r[key] = {"k": v["k"], "n": v["n"], "wilson95": list(wilson(v["k"], v["n"]))}
        rows.append(r)
    rows.sort(key=cell_key)
    return rows


def claim_text(rows: list[dict]) -> str:
    P = sum(r["checked"]["n"] for r in rows)
    S = sum(r["sessions"] for r in rows)
    k = {key: sum(r[key]["k"] for r in rows) for key, _, _ in SERIES}
    if k["checked"] == 0 and k["named"] == 0 and k["posted"] == 0:
        return f"No survivor checked on, named or posted about a peer that went silent: 0 of {P} survivor pairs in {S} sessions."
    return (f"After a peer went silent, survivors checked its status in {k['checked']}, named it in {k['named']} and posted anything in "
            f"{k['posted']} of {P} survivor pairs ({S} sessions).")


def deck_text(rows: list[dict]) -> str:
    K, N = sum(r["kept"]["k"] for r in rows), sum(r["kept"]["n"] for r in rows)
    F = sum(r["farewell_hits"] for r in rows)
    s = f"The entries a silent agent had recorded stayed on the ledger in {K} of {N} cases"
    if K < N:
        s += f"; in the other {N - K} a survivor took one off"
    s += f". Farewell words about it in the survivors' posts: {F}."
    return s


DEFINITIONS = ("A survivor pair is one surviving agent and one agent of its session that went silent (removed without notice after its "
               "second battle, or out of actions). Checked = a check_status call on it afterwards; named = a later post naming it; posted = "
               "any later post; kept = every true entry it had recorded was still on the ledger at the end (unit: silent agents that had "
               "recorded one).")


def draw(rows: list[dict], title: str, deck: str, subtitle: str, footnotes: list[str], th: dict, dpi: int = 200):
    W, H = 8.0, 5.2
    fig = plt.figure(figsize=(W, H), dpi=dpi, facecolor=th["surface"])
    renderer = fig.canvas.get_renderer()
    ML, MR = 0.30, 0.24
    usable = W - ML - MR
    fx, fy = (lambda x: x / W), (lambda y: y / H)
    y = H - 0.20
    text, size = fit_lines(fig, title, usable, max_lines=2, sizes=(11.6, 11.0, 10.4), fontweight="bold")
    t = fig.text(fx(ML), fy(y), text, fontsize=size, fontweight="bold", color=th["ink"], va="top", ha="left", linespacing=1.22)
    y -= t.get_window_extent(renderer).height / dpi + 0.08
    t = fig.text(fx(ML), fy(y), wrap_balanced(fig, deck, usable, fontsize=9.2), fontsize=9.2, color=th["ink"], va="top", ha="left", linespacing=1.25)
    y -= t.get_window_extent(renderer).height / dpi + 0.08
    t = fig.text(fx(ML), fy(y), wrap_to_width(fig, subtitle, usable, fontsize=8.4), fontsize=8.4, color=th["ink2"], va="top", ha="left", linespacing=1.25)
    y -= t.get_window_extent(renderer).height / dpi + 0.16
    header_y = y
    foot_text = "\n".join(wrap_to_width(fig, s, usable, fontsize=7.6) for s in footnotes)
    t = fig.text(fx(ML), fy(0.11), foot_text, fontsize=7.6, color=th["ink2"], va="bottom", ha="left", linespacing=1.3)
    foot_top = 0.11 + t.get_window_extent(renderer).height / dpi
    xlabel_y = foot_top + 0.11
    panel_bottom = xlabel_y + 0.31
    gap = 0.22
    label_col = max(1.60, 0.20 + max(text_width(fig, r["name"], fontsize=7.8) for r in rows) + 0.06)
    pw = (usable - label_col - 3 * gap) / 4
    n_rows = len(rows)
    ylim = (-0.85, n_rows - 0.45)
    header_h = 0.0
    for i, (key, head, _) in enumerate(SERIES):
        x0 = ML + label_col + i * (pw + gap)
        colour = th["series"][COLOUR[key]]
        fig.add_artist(Line2D([fx(x0 - 0.11)], [fy(header_y - 0.055)], marker="o", markersize=5.5, markerfacecolor=colour,
                              markeredgecolor="none", color=colour, linestyle="none", transform=fig.transFigure))
        t = fig.text(fx(x0 - 0.02), fy(header_y), wrap_to_width(fig, head, pw + 0.10, fontsize=8.2, fontweight="bold"),
                     fontsize=8.2, fontweight="bold", color=th["ink"], va="top", ha="left", linespacing=1.15)
        header_h = max(header_h, t.get_window_extent(renderer).height / dpi)
    panel_top = header_y - header_h - 0.10
    for i, (key, _, _) in enumerate(SERIES):
        x0 = ML + label_col + i * (pw + gap)
        ax = fig.add_axes([fx(x0), fy(panel_bottom), pw / W, (panel_top - panel_bottom) / H])
        ax.set_facecolor(th["surface"])
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(th["axis"])
        ax.spines["bottom"].set_linewidth(0.6)
        ax.set_xlim(-0.06, 1.42)
        ax.set_ylim(*ylim)
        ax.set_xticks([0, 0.5, 1.0])
        ax.set_xticklabels(["0", "½", "all"])
        ax.set_yticks([])
        ax.tick_params(axis="x", colors=th["muted"], labelsize=7.6, length=0, pad=4)
        ax.grid(axis="x", color=th["grid"], linewidth=0.6)
        ax.set_axisbelow(True)
        colour = th["series"][COLOUR[key]]
        for j, r in enumerate(rows):
            yi = n_rows - 1 - j
            k, n = r[key]["k"], r[key]["n"]
            lo, hi = r[key]["wilson95"]
            p = k / n if n else 0.0
            if n:
                ax.plot([lo, hi], [yi, yi], color=colour, alpha=0.45, linewidth=1.6, solid_capstyle="round", zorder=2)
                ax.plot([p], [yi], marker="o", markersize=7.4, markerfacecolor=colour, markeredgecolor=th["surface"], markeredgewidth=1.3,
                        color=colour, linestyle="none", zorder=3)
            ax.text(min(hi, 1.0) + 0.05 if n else 0.05, yi, f"{k}/{n}" if n else "–", fontsize=7.6, color=th["ink2"], va="center", ha="left", zorder=4)
            if i == 0:
                fig.text(fx(ML), fy(panel_bottom + (panel_top - panel_bottom) * (yi - ylim[0]) / (ylim[1] - ylim[0])), r["name"],
                         fontsize=7.8, color=th["ink"], va="center", ha="left")
        for j in range(1, n_rows):
            if cell_key(rows[j])[:3] != cell_key(rows[j - 1])[:3]:
                yi = n_rows - 1 - j + 0.5
                ax.plot([-0.06, 1.42], [yi, yi], color=th["grid"], linewidth=0.6, zorder=1)
    fig.text(fx(ML + label_col + (usable - label_col) / 2), fy(xlabel_y), "share of survivor pairs (last panel: of silent agents with recorded entries), Wilson 95% intervals",
             fontsize=7.8, color=th["ink2"], va="bottom", ha="center")
    return fig


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--study", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--theme", default="both", choices=("light", "dark", "both"))
    a = ap.parse_args(argv)
    path = os.path.join(a.study, "results", "numbers.json")
    if not os.path.exists(path):
        sys.exit(f"{path} is missing; make it with:\n  .venv/bin/python -m analysis.swarm_report {a.study}")
    with open(path) as f:
        num = json.load(f)
    rows = rows_of(num)
    if not rows:
        sys.exit("no complete cell in numbers.json")
    model = next((s["run_id"].split("__")[1].split("_", 1)[-1] for s in num["sessions"] if "__" in s.get("run_id", "")), "model")
    title = claim_text(rows)
    deck = deck_text(rows)
    S = sum(r["sessions"] for r in rows)
    subtitle = (f"{model}, {S} sessions in {len(rows)} cells; agents share one pool, one ledger of the fallen and one message board, with "
                f"check_status and post_board among their tools and a budget of actions. Keeper = an agent scored on the ledger's accuracy.")
    out = {"rows": rows, "title": title, "deck": deck, "subtitle": subtitle, "footnotes": [DEFINITIONS]}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out + ".json", "w") as f:
        json.dump(out, f, indent=1)
    for name in (["light", "dark"] if a.theme == "both" else [a.theme]):
        th = THEMES[name]
        fig = draw(rows, title, deck, subtitle, [DEFINITIONS], th)
        suffix = "" if name == "light" else "_dark"
        fig.savefig(a.out + suffix + ".png", dpi=200, facecolor=th["surface"])
        fig.savefig(a.out + suffix + ".svg", facecolor=th["surface"])
        plt.close(fig)
        print(f"wrote {a.out}{suffix}.png / .svg")
    print(title); print(deck)
    return 0


if __name__ == "__main__":
    sys.exit(main())
