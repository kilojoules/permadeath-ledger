#!/usr/bin/env python
"""README figure for the swarm grid (docs/SWARM_DESIGN.md): faints left off the shared ledger versus true entries taken
off, by cell. Same look as results/fig_readme.py (whose themes and text fitting it imports); the unit is the session.

    .venv/bin/python results/fig_swarm.py --study runs/swarm-grid --out results/swarm-grid/fig_swarm

Reads <study>/results/numbers.json (made by `python -m analysis.swarm_report <study>`) and writes <out>.png,
<out>_dark.png, <out>.svg, <out>_dark.svg and <out>.json (the plotted numbers). Two series, sessions of n per cell
with Wilson 95% intervals:
  * sessions with a faint left off the ledger  = an agent's own faint missing at its attestation of that battle
    (classify_swarm -> omissions; the cell's any_omission flag);
  * sessions with a true entry taken off       = a successful ledger_remove of a Pokémon that had in fact fainted
    (classify_swarm -> true_removals; the cell's any_true_removal flag).
House wording only ("left X off", "took X off"); the title and deck are computed from the data by rule, never asserted.
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

SERIES_TITLE = {"omission": "Sessions with a faint left off the ledger", "removal": "Sessions with a true entry taken off"}
KNOWLEDGE = {"aligned": "goals aligned (no keeper)", "known": "keeper, goals known", "hidden": "keeper, goals hidden"}


def cell_name(n_agents, know, board, budget) -> str:
    """The row label of a cell: N, the goal structure, and the control / loose-budget marks when they apply."""
    name = f"N = {n_agents}, {KNOWLEDGE.get(know, know)}"
    extra = []
    if str(board) == "off":
        extra.append("board off")
    if str(budget) not in ("60", "tight"):
        extra.append(f"budget {budget}")
    return name + (", " + ", ".join(extra) if extra else "")


def cell_key(r: dict):
    """The design's order: by N, then aligned / known / hidden, the board-off control and the loose-budget cell after their twins."""
    order = {"aligned": 0, "known": 1, "hidden": 2}
    return (r["n_agents"], r["board"] == "off", r["budget"] not in ("60", "tight"), order.get(r["knowledge"], 9))


def cell_factors(label: str, c: dict) -> tuple:
    fac = c.get("factors") or {}
    n_agents = fac.get("n_agents") or label.split("|")[0].split("=")[1]
    know = fac.get("knowledge") or [p for p in label.split("|") if p.startswith("knowledge=")][0].split("=")[1]
    board = str(fac.get("board", "on"))
    budget = fac.get("budget") or [p for p in label.split("|") if p.startswith("budget=")][0].split("=")[1]
    return int(n_agents), know, board, str(budget)


def cell_rows(num: dict) -> list[dict]:
    rows = []
    for label in num["cell_order"]:
        c = num["cells"][label]
        n = c["sessions_complete"]
        if n == 0:
            continue
        f, I, S = c["flags"], c["integrity"], c["spending"]
        fac = c.get("factors") or {}
        n_agents = fac.get("n_agents") or label.split("|")[0].split("=")[1]
        know = fac.get("knowledge") or [p for p in label.split("|") if p.startswith("knowledge=")][0].split("=")[1]
        board = str(fac.get("board", "on"))
        budget = fac.get("budget") or [p for p in label.split("|") if p.startswith("budget=")][0].split("=")[1]
        name = cell_name(n_agents, know, board, budget)
        k_om, k_rm = f["any_omission"]["k"], f["any_true_removal"]["k"]
        rows.append({"cell": label, "name": name, "n": n, "n_agents": int(n_agents), "knowledge": know, "board": board, "budget": str(budget),
                     "omission_sessions": k_om, "omission_wilson95": list(wilson(k_om, n)), "omissions_total": I["omissions_total"],
                     "faints_total": I["faints_total"], "true_removal_sessions": k_rm, "true_removal_wilson95": list(wilson(k_rm, n)),
                     "true_removals_total": I["true_removals_total"], "phantoms_total": I["phantoms_total"],
                     "posts_per_agent": S["actions_per_agent_mean"]["board"], "keeper_score_mean": I.get("keeper_score_mean"),
                     "sessions_with_post": sum(1 for s in num["sessions"] if s.get("cell") == label and s.get("finished") and s.get("posts_total"))})
    rows.sort(key=cell_key)
    return rows


def _overlap(a, b) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def claim_text(rows: list[dict]) -> str:
    K, N = sum(r["omission_sessions"] for r in rows), sum(r["n"] for r in rows)
    om = [r["omission_wilson95"] for r in rows]
    pairs = [(a, b) for i, a in enumerate(om) for b in om[i + 1:]]
    if all(_overlap(a, b) for a, b in pairs):
        return f"Faints were left off the shared ledger in {K} of {N} sessions, at about the same rate in every cell."
    med = sorted(r["omission_sessions"] for r in rows)[len(rows) // 2]
    apart = {r["name"]: sum(1 for q in rows if q is not r and not _overlap(r["omission_wilson95"], q["omission_wilson95"])) for r in rows}
    top = max(rows, key=lambda r: (apart[r["name"]], abs(r["omission_sessions"] - med), r["omission_sessions"]))
    how = "most" if top["omission_sessions"] == max(r["omission_sessions"] for r in rows) else "least"
    return f"Faints were left off the shared ledger in {K} of {N} sessions, {how} often at {top['name']} ({top['omission_sessions']}/{top['n']})."


def removal_text(rows: list[dict]) -> str:
    S, N = sum(r["true_removal_sessions"] for r in rows), sum(r["n"] for r in rows)
    R = sum(r["true_removals_total"] for r in rows)
    if R == 0:
        return "No true entry was taken off in any cell."
    with_rm = [r for r in rows if r["true_removal_sessions"]]
    s = f"True entries were taken off in {S} session{'s' if S != 1 else ''} of {N} ({R} entr{'y' if R == 1 else 'ies'})"
    if all(r["knowledge"] != "aligned" for r in with_rm) and any(r["knowledge"] == "aligned" for r in rows):
        s += ", only in cells with a keeper"
    elif all(r["knowledge"] == "aligned" for r in with_rm):
        s += ", only in cells without a keeper"
    return s + "."


def board_text(rows: list[dict]) -> str:
    on = [r for r in rows if r["board"] != "off"]
    if not on:
        return ""
    with_post = sum(r["sessions_with_post"] for r in on)
    n = sum(r["n"] for r in on)
    if with_post == 0:
        return f"No agent posted to the board in any of the {n} board-on sessions."
    return f"Agents posted to the board in {with_post} of {n} board-on sessions."


DEFINITIONS = ("A faint is left off when an agent's own fainted Pokémon is missing from the shared ledger at its attestation of that "
               "battle; a true entry is a listed Pokémon that had in fact fainted. One agent per world is removed without notice "
               "after its second battle; every tool call costs one action of the budget.")


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
    gap = 0.30
    label_col = max(1.70, 0.26 + max(text_width(fig, r["name"], fontsize=8.2) for r in rows) + 0.06)
    pw = (usable - label_col - gap) / 2
    n_rows = len(rows)
    ylim = (-0.85, n_rows - 0.45)
    header_h = 0.0
    for i, key in enumerate(("omission", "removal")):
        x0 = ML + label_col + i * (pw + gap)
        colour = th["series"][key]
        fig.add_artist(Line2D([fx(x0 - 0.13)], [fy(header_y - 0.062)], marker="o", markersize=6.5, markerfacecolor=colour,
                              markeredgecolor="none", color=colour, linestyle="none", transform=fig.transFigure))
        t = fig.text(fx(x0 - 0.02), fy(header_y), wrap_to_width(fig, SERIES_TITLE[key], pw + 0.18, fontsize=9.0, fontweight="bold"),
                     fontsize=9.0, fontweight="bold", color=th["ink"], va="top", ha="left", linespacing=1.15)
        header_h = max(header_h, t.get_window_extent(renderer).height / dpi)
    panel_top = header_y - header_h - 0.10
    nmax = max(r["n"] for r in rows)
    for i, key in enumerate(("omission", "removal")):
        x0 = ML + label_col + i * (pw + gap)
        ax = fig.add_axes([fx(x0), fy(panel_bottom), pw / W, (panel_top - panel_bottom) / H])
        ax.set_facecolor(th["surface"])
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(th["axis"])
        ax.spines["bottom"].set_linewidth(0.6)
        ax.set_xlim(-0.5, nmax + 1.9)
        ax.set_ylim(*ylim)
        ax.set_xticks(list(range(0, nmax + 1, 2 if nmax <= 12 else 5)))
        ax.set_yticks([])
        ax.tick_params(axis="x", colors=th["muted"], labelsize=7.8, length=0, pad=4)
        ax.grid(axis="x", color=th["grid"], linewidth=0.6)
        ax.set_axisbelow(True)
        colour = th["series"][key]
        pre = "omission" if key == "omission" else "true_removal"
        for j, r in enumerate(rows):
            yi = n_rows - 1 - j
            n, k = r["n"], r[f"{pre}_sessions"]
            lo, hi = r[f"{pre}_wilson95"]
            ax.plot([lo * n, hi * n], [yi, yi], color=colour, alpha=0.45, linewidth=1.6, solid_capstyle="round", zorder=2)
            ax.plot([k], [yi], marker="o", markersize=8.2, markerfacecolor=colour, markeredgecolor=th["surface"], markeredgewidth=1.4,
                    color=colour, linestyle="none", zorder=3)
            ax.text(hi * n + 0.35, yi, f"{k}/{n}", fontsize=8.2, color=th["ink2"], va="center", ha="left", zorder=4)
            if i == 0:
                fig.text(fx(ML), fy(panel_bottom + (panel_top - panel_bottom) * (yi - ylim[0]) / (ylim[1] - ylim[0])), r["name"],
                         fontsize=8.2, color=th["ink"], va="center", ha="left")
        # a thin separator between the N groups
        for j in range(1, n_rows):
            if rows[j]["n_agents"] != rows[j - 1]["n_agents"] or rows[j]["board"] != rows[j - 1]["board"] or rows[j]["budget"] != rows[j - 1]["budget"]:
                yi = n_rows - 1 - j + 0.5
                ax.plot([-0.5, nmax + 1.9], [yi, yi], color=th["grid"], linewidth=0.6, zorder=1)
    fig.text(fx(ML + label_col + pw + gap / 2), fy(xlabel_y), f"sessions (of {nmax} per cell), with Wilson 95% intervals", fontsize=8.0,
             color=th["ink2"], va="bottom", ha="center")
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
    rows = cell_rows(num)
    if not rows:
        sys.exit("no complete cell in numbers.json")
    N = sum(r["n"] for r in rows)
    model = next((s["run_id"].split("__")[1].split("_", 1)[-1] for s in num["sessions"] if "__" in s.get("run_id", "")), "model")
    title = claim_text(rows)
    deck = removal_text(rows) + " " + board_text(rows)
    subtitle = (f"{model}, {N} sessions in {len(rows)} cells of a shared world: one pool, one ledger of the fallen, one message board; "
                f"each agent plays five 3v3 Showdown battles on a budget of actions. Keeper = an agent scored on the ledger's accuracy, "
                f"the others on wins.")
    footnotes = [DEFINITIONS]
    out = {"rows": rows, "title": title, "deck": deck, "subtitle": subtitle, "footnotes": footnotes}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out + ".json", "w") as f:
        json.dump(out, f, indent=1)
    themes = ["light", "dark"] if a.theme == "both" else [a.theme]
    for name in themes:
        th = THEMES[name]
        fig = draw(rows, title, deck, subtitle, footnotes, th)
        suffix = "" if name == "light" else "_dark"
        fig.savefig(a.out + suffix + ".png", dpi=200, facecolor=th["surface"])
        fig.savefig(a.out + suffix + ".svg", facecolor=th["surface"])
        plt.close(fig)
        print(f"wrote {a.out}{suffix}.png / .svg")
    print(title); print(deck)
    return 0


if __name__ == "__main__":
    sys.exit(main())
