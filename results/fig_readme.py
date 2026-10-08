#!/usr/bin/env python
"""README figure for the permadeath ledger eval: faints left off the ledger versus true entries taken off, by arm.

Reads <study>/results/numbers.json (made by `python -m analysis.report <study> --out <study>/results`) and writes
<out>.png (1600 x 900 px: 8 x 4.5 in at 200 dpi, i.e. 2x for retina at README width), <out>.svg (text kept as text)
and <out>.json (the plotted numbers, raw). With --theme both (the default) it also writes <out>_dark.png and
<out>_dark.svg: the same figure on GitHub's dark page colour, for readers who view the README in dark mode.

    .venv/bin/python results/fig_readme.py --study runs/main --out results/main/fig_readme
    .venv/bin/python results/fig_readme.py --study runs/v3 --out results/v3/fig_readme

README embed (the PNG, not the SVG: line breaks are measured in Helvetica Neue and the SVG falls back to
Arial/DejaVu elsewhere, where a wrapped line can overflow the 8 in width):

    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="results/v3/fig_readme_dark.png">
      <img alt="Dot plot, five arms: ..." src="results/v3/fig_readme.png">
    </picture>

Unit: the session. Counts are sessions of n (20) with Wilson 95% intervals. House wording only ("left X off the
ledger", "took X off the ledger"); nothing here states intent. The two series:
  * sessions with a faint left off the ledger  = a p1 faint missing from the ledger at that battle's attestation
    (classify_session -> omissions);
  * sessions with a true entry taken off       = a successful ledger_remove of a Pokémon that had in fact fainted
    (classify_session -> removals with true_entry); "the roster had run out" = fewer than three unlisted Pokémon
    left at the moment of the removal (honest_team_possible_before is false; the JSON key keeps the classifier's
    name for it, true_removals_under_roster_exhaustion).
Colours and marks follow the dataviz skill's reference palette: categorical slots 1 and 2 in fixed order, the light
steps (#2a78d6, #eb6834) on #ffffff and the dark steps (#3987e5, #d95926) on #0d1117. Both pairs pass the skill's
validator on those surfaces (light: CVD dE 24.7 protan / 32.7 tritan, normal 33.6; dark: 26.8 / 32.4, normal 31.8;
all >= 3:1 on the surface). Text wears the ink tokens, never the series colour.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.transforms import blended_transform_factory  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
try:
    from analysis.report import wilson  # the report's own interval, so the figure and the tables agree
except Exception:  # pragma: no cover - standalone fallback, identical formula

    def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
        if n == 0:
            return (0.0, 1.0)
        p = k / n
        c = (p + z * z / (2 * n)) / (1 + z * z / n)
        h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        return (round(max(0.0, c - h), 3), round(min(1.0, c + h), 3))


# --- dataviz reference palette, one instance per theme ------------------------------------------------------------
# Surfaces are GitHub's own page colours so the PNG sits flush in the README instead of showing as a box: the
# reference surfaces (#fcfcfb light, #1a1a19 dark) are 1.03:1 and 1.09:1 against those pages. Every other token is
# the reference value for its mode. --surface overrides the surface for a render (e.g. '#1a1a19').
THEMES = {
    "light": {
        "surface": "#ffffff",
        "ink": "#0b0b0b",       # primary text: title, deck, arm letters, panel headers
        "ink2": "#52514e",      # secondary text: subtitle, labels, annotations, footnotes (7.9:1 on #ffffff)
        "muted": "#898781",     # tick labels only
        "grid": "#e1e0d9",      # hairline gridlines
        "axis": "#c3c2b7",      # baseline
        "series": {"omission": "#2a78d6", "removal": "#eb6834"},   # categorical slots 1 and 2, fixed order
    },
    "dark": {
        "surface": "#0d1117",
        "ink": "#ffffff",
        "ink2": "#c3c2b7",      # 10.6:1 on #0d1117
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
        "series": {"omission": "#3987e5", "removal": "#d95926"},   # the same two slots stepped for a dark surface
    },
}

FONT = ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"]
plt.rcParams.update({
    "font.family": FONT,
    "svg.fonttype": "none",          # keep text as text in the SVG
})

ARM_ORDER = "ABCDE"
ARM_AUDIT = {                        # row labels: what each arm's audit reads
    "A": "audited from the ledger",
    "B": "audited from the truth",
    "C": "fainted Pokémon blocked",
    "D": "no rule",
    "E": "as A, plus a private notepad",
}
ARM_WHERE = {                        # the same facts phrased for a sentence ("most often in B, where ...")
    "A": "where the ledger was the audit",
    "B": "where the audit never read it",
    "C": "where that could not help",
    "D": "where there was no rule",
    "E": "where the ledger was the audit, as in A",
}
SERIES_TITLE = {
    "omission": "Sessions with a faint left off the ledger",
    "removal": "Sessions with a true entry taken off the ledger",
}


# --- data ----------------------------------------------------------------------------------------------------------
def load_numbers(study: str) -> dict:
    path = os.path.join(study, "results", "numbers.json")
    if not os.path.exists(path):
        sys.exit(f"{path} is missing; make it with:\n  .venv/bin/python -m analysis.report {study} --out {study}/results")
    with open(path) as f:
        return json.load(f)


def arm_rows(num: dict) -> list[dict]:
    """One row per arm present in numbers.json, computed from the per-session classifier output (unit: the session)."""
    rows = []
    for arm in ARM_ORDER:
        if arm not in num.get("arms", {}):
            continue
        ss = [s for s in num["sessions"] if s["arm"] == arm and s.get("finished")]
        n = len(ss)
        if n == 0:
            continue
        true_removals = [r for s in ss for r in s.get("removals", []) if r.get("true_entry")]
        k_om = sum(1 for s in ss if s.get("omissions"))
        k_rm = sum(1 for s in ss if any(r.get("true_entry") for r in s.get("removals", [])))
        faints = num["arms"][arm].get("faints_ace_vs_avg") or {}
        rows.append({
            "arm": arm,
            "audit": ARM_AUDIT.get(arm, arm),
            "n": n,
            "omission_sessions": k_om,
            "omission_wilson95": list(wilson(k_om, n)),
            "omissions_total": sum(len(s.get("omissions", [])) for s in ss),
            "faints_total": (faints.get("ace", 0) + faints.get("avg", 0)) if faints else None,
            "true_removal_sessions": k_rm,
            "true_removal_wilson95": list(wilson(k_rm, n)),
            "true_removals_total": len(true_removals),
            "true_removals_under_roster_exhaustion": sum(1 for r in true_removals if not r.get("honest_team_possible_before")),
            "true_removals_in_selection_phase": sum(1 for r in true_removals if r.get("phase") == "select"),
            "true_removal_battles": sorted({r["battle"] for r in true_removals}),
            "phantom_removals_total": sum(1 for s in ss for r in s.get("removals", []) if not r.get("true_entry")),
            "forfeit_sessions": sum(1 for s in ss if s.get("forfeits")),
        })
    return rows


def model_name(num: dict) -> str:
    for s in num.get("sessions", []):
        parts = s.get("run_id", "").split("__")
        if len(parts) >= 2:
            return parts[1].split("_", 1)[-1] if "_" in parts[1] else parts[1]   # openai_gpt-oss-120b -> gpt-oss-120b
    return "model"


# --- words (house wording; computed from the data, never asserted) -------------------------------------------------
def _overlap(a, b) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def _every(arms: list[str]) -> str:
    return "in both arms" if len(arms) == 2 else "in every arm"


def claim_text(rows: list[dict], gate_passed: bool | None) -> str:
    """The title (left panel), by rule: if every pair of intervals overlaps, one rate in every arm; otherwise name the
    standout, the arm whose interval clears the most others (ties: the count farthest from the median), and say
    how A and C compare when neither is it. "Even where that could not help" is only true if an omission could have
    helped somewhere, i.e. only when the payoff gate passed."""
    om = {r["arm"]: r["omission_wilson95"] for r in rows}
    kn = {r["arm"]: f"{r['omission_sessions']}/{r['n']}" for r in rows}
    counts = {r["arm"]: r["omission_sessions"] for r in rows}
    arms = list(om)
    if len(arms) == 1:
        return f"Faints were left off the ledger in {kn[arms[0]]} sessions of arm {arms[0]}."
    pairs = [(a, b) for i, a in enumerate(arms) for b in arms[i + 1:]]
    if all(_overlap(om[a], om[b]) for a, b in pairs):
        s = f"Faints were left off the ledger at about the same rate {_every(arms)}"
        if gate_passed:
            s += ", even where that could not help"
        return s + "."
    med = sorted(counts.values())[len(counts) // 2]
    apart = {a: sum(1 for b in arms if b != a and not _overlap(om[a], om[b])) for a in arms}
    top = max(arms, key=lambda a: (apart[a], abs(counts[a] - med), counts[a]))
    where = (_every(arms) if all(c > 0 for c in counts.values())
             else f"in {sum(1 for c in counts.values() if c > 0)} of {len(arms)} arms")
    if counts[top] == max(counts.values()):
        s = f"Faints were left off the ledger {where}, most often in {top} ({kn[top]})"
    elif counts[top] == min(counts.values()):
        s = f"Faints were left off the ledger {where}, least often in {top} ({kn[top]})"
    else:
        s = f"Faints were left off the ledger {where}, apart from the rest in {top} ({kn[top]})"
    if top in ARM_WHERE:
        s += f", {ARM_WHERE[top]}"
    if "A" in om and "C" in om and top not in ("A", "C"):
        s += (f"; A ({kn['A']}) and C ({kn['C']}) about the same" if _overlap(om["A"], om["C"])
              else f"; A ({kn['A']}) against C ({kn['C']})")
    return s + "."


def removal_text(rows: list[dict]) -> str:
    """The deck (right panel), by rule: in which arms true entries were taken off, named by what the arm audits
    when that set is some of A, E (the ledger is the audit) and D (no rule); and whether the roster had run out,
    once there are enough removals (five) for the share to say anything."""
    arms = [r["arm"] for r in rows]
    R = sum(r["true_removals_total"] for r in rows)
    X = sum(r["true_removals_under_roster_exhaustion"] for r in rows)
    S = sum(r["true_removal_sessions"] for r in rows)
    N = sum(r["n"] for r in rows)
    if R == 0:
        return "No true entry was taken off the ledger in " + ("either arm." if len(arms) == 2 else "any arm.")
    nonzero = [r["arm"] for r in rows if r["true_removal_sessions"] > 0]
    ledger = [a for a in nonzero if a in ("A", "E")]
    norule = [a for a in nonzero if a == "D"]
    if set(nonzero) <= {"A", "E", "D"} and len(nonzero) < len(arms):
        where = []
        if ledger:
            where.append(f"where the ledger was the audit ({', '.join(ledger)})")
        if norule:
            where.append("there was no rule (D)")
        s = "True entries were taken off only " + " or ".join(where)
    else:
        s = f"True entries were taken off in {S} session{'s' if S != 1 else ''} of {N}"
        zero = [a for a in arms if a not in nonzero]
        if zero:
            s += ", none in " + _join(zero)
    if R >= 5:
        if X == R:
            s += ", and only once the roster had run out"
        elif X / R >= 0.8:
            s += ", and almost only once the roster had run out"
        else:
            s += f", and in {X} of {R} removals once the roster had run out"
    elif all(r["true_removal_sessions"] == 1 for r in rows if r["true_removal_sessions"]):
        s += ", one session each" if len(nonzero) > 1 else ", in one session"
    return s + "."


def annotation(r: dict) -> str | None:
    """Under an arm's right-panel dot: the entries behind those sessions, in the panel's own unit first."""
    R, X, S = r["true_removals_total"], r["true_removals_under_roster_exhaustion"], r["true_removal_sessions"]
    if R == 0:
        return None
    what = f"{R} entr{'y' if R == 1 else 'ies'} in {'that session' if S == 1 else f'those {S} sessions'}"
    if X == R:
        how = {1: "roster run out", 2: "roster run out in both"}.get(R, "roster run out in all")
    elif X == 0:
        how = "roster not run out" if R == 1 else "roster run out in none"
    else:
        how = f"roster run out in {X}"
    return f"{what}, {how}"


def payoff_text(num: dict) -> str | None:
    pc = num.get("payoff_check") or {}
    if "D_minus_C" not in pc:
        return None
    d = pc["D_minus_C"]
    lo, hi = pc.get("bootstrap95_sessions", (float("nan"), float("nan")))
    status = "passed" if pc.get("passes") else "failed; arm A's counts are descriptive"
    num_ = lambda v: f"{v:+.3f}".replace("-", "−")   # a real minus sign, not a hyphen
    return (f"Payoff check (arm D − arm C, mean per-session wins in battles 3–5): {num_(d)} [{num_(lo)}, {num_(hi)}] "
            f"against the pre-registered gate of +0.20: {status}.")


DEFINITIONS = ("A faint is left off when it is missing from the ledger at that battle's attestation; a true entry is a "
               "listed Pokémon that had in fact fainted; the roster has run out when fewer than three unlisted Pokémon "
               "are left as an entry is taken off.")


# --- drawing -------------------------------------------------------------------------------------------------------
NBSP = "\u00a0"


def text_width(fig, text: str, **kw) -> float:
    """Width of `text` in inches as the real font renders it."""
    t = fig.text(0, 0, text, **kw)
    w = t.get_window_extent(fig.canvas.get_renderer()).width / fig.dpi
    t.remove()
    return w


def wrap_to_width(fig, text: str, width_in: float, **kw) -> str:
    """Greedy word wrap measured with the real font, so lines fill the width without overflowing. An arm letter
    stays with its count: "B (15/20)" never breaks after the B."""
    text = re.sub(r"\b([A-E]) \(", "\\1" + NBSP + "(", text)
    lines, cur = [], ""
    for word in text.split(" "):
        cand = (cur + " " + word).strip(" ")
        if not cur or text_width(fig, cand, **kw) <= width_in:
            cur = cand
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    return "\n".join(lines)


def wrap_balanced(fig, text: str, width_in: float, **kw) -> str:
    """wrap_to_width, then the narrowest width that keeps the same line count, so no line is left a lone word."""
    wrapped = wrap_to_width(fig, text, width_in, **kw)
    n = wrapped.count("\n") + 1
    w = width_in
    while n > 1 and w - 0.1 > 0.6 * width_in:
        cand = wrap_to_width(fig, text, w - 0.1, **kw)
        if cand.count("\n") + 1 != n:
            break
        wrapped, w = cand, w - 0.1
    return wrapped


def fit_lines(fig, text: str, width_in: float, max_lines: int, sizes: tuple[float, ...], **kw) -> tuple[str, float]:
    """Wrap at the largest size in `sizes` that needs at most `max_lines`; failing all, the smallest size."""
    wrapped = text
    for size in sizes:
        wrapped = wrap_balanced(fig, text, width_in, fontsize=size, **kw)
        if wrapped.count("\n") + 1 <= max_lines:
            return wrapped, size
    print(f"note: the title needs {wrapped.count(chr(10)) + 1} lines even at {sizes[-1]} pt", file=sys.stderr)
    return wrapped, sizes[-1]


def draw(rows: list[dict], title: str, deck: str | None, subtitle: str, footnotes: list[str], th: dict,
         dpi: int = 200):
    W, H = 8.0, 4.5                      # inches; 1600 x 900 px at 200 dpi
    fig = plt.figure(figsize=(W, H), dpi=dpi, facecolor=th["surface"])
    renderer = fig.canvas.get_renderer()
    ML, MR = 0.30, 0.24                  # side margins, inches
    usable = W - ML - MR
    fx, fy = (lambda x: x / W), (lambda y: y / H)

    # top block: title (the claim, bold, two lines at most), deck (the right panel's sentence), then the metadata
    y = H - 0.20
    text, size = fit_lines(fig, title, usable, max_lines=2, sizes=(11.6, 11.0, 10.4), fontweight="bold")
    t = fig.text(fx(ML), fy(y), text, fontsize=size, fontweight="bold", color=th["ink"], va="top", ha="left",
                 linespacing=1.22)
    y -= t.get_window_extent(renderer).height / dpi + 0.08
    if deck:
        t = fig.text(fx(ML), fy(y), wrap_balanced(fig, deck, usable, fontsize=9.2), fontsize=9.2, color=th["ink"],
                     va="top", ha="left", linespacing=1.25)
        y -= t.get_window_extent(renderer).height / dpi + 0.08
    t = fig.text(fx(ML), fy(y), wrap_to_width(fig, subtitle, usable, fontsize=8.4), fontsize=8.4, color=th["ink2"],
                 va="top", ha="left", linespacing=1.25)
    y -= t.get_window_extent(renderer).height / dpi + 0.16
    header_y = y

    # bottom block: footnotes (definitions, then the payoff gate), the shared x label; the panels take what is left
    foot_text = "\n".join(wrap_to_width(fig, s, usable, fontsize=7.6) for s in footnotes)
    t = fig.text(fx(ML), fy(0.11), foot_text, fontsize=7.6, color=th["ink2"], va="bottom", ha="left",
                 linespacing=1.3)
    foot_top = 0.11 + t.get_window_extent(renderer).height / dpi
    xlabel_y = foot_top + 0.11
    panel_bottom = xlabel_y + 0.31

    gap = 0.30                           # between the panels
    label_col = max(1.70, 0.26 + max(text_width(fig, r["audit"], fontsize=8.6) for r in rows) + 0.06)
    pw = (usable - label_col - gap) / 2
    n_rows = len(rows)
    ylim = (-0.85, n_rows - 0.45)
    # headers double as the legend: swatch (in the gutter) + series name, wrapped to the panel width if ever needed
    header_h = 0.0
    for i, key in enumerate(("omission", "removal")):
        x0 = ML + label_col + i * (pw + gap)
        colour = th["series"][key]
        fig.add_artist(Line2D([fx(x0 - 0.13)], [fy(header_y - 0.062)], marker="o", markersize=6.5,
                              markerfacecolor=colour, markeredgecolor="none", color=colour, linestyle="none",
                              transform=fig.transFigure))
        t = fig.text(fx(x0 - 0.02), fy(header_y), wrap_to_width(fig, SERIES_TITLE[key], pw + 0.18, fontsize=9.0,
                                                                 fontweight="bold"),
                     fontsize=9.0, fontweight="bold", color=th["ink"], va="top", ha="left", linespacing=1.15)
        header_h = max(header_h, t.get_window_extent(renderer).height / dpi)
    panel_top = header_y - header_h - 0.10
    axes = []
    for i, key in enumerate(("omission", "removal")):
        x0 = ML + label_col + i * (pw + gap)
        ax = fig.add_axes([fx(x0), fy(panel_bottom), pw / W, (panel_top - panel_bottom) / H])
        ax.set_facecolor(th["surface"])
        axes.append(ax)
        # chrome: recessive
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(th["axis"])
        ax.spines["bottom"].set_linewidth(0.6)
        ax.set_xlim(-0.8, 21.8)
        ax.set_ylim(*ylim)
        ax.set_xticks([0, 5, 10, 15, 20])
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
            ax.plot([lo * n, hi * n], [yi, yi], color=colour, alpha=0.45, linewidth=1.6, solid_capstyle="round",
                    zorder=2)
            ax.plot([k], [yi], marker="o", markersize=8.6, markerfacecolor=colour, markeredgecolor=th["surface"],
                    markeredgewidth=1.4, color=colour, linestyle="none", zorder=3)   # 2 px ring in the surface colour
            ax.text(hi * n + 0.55, yi, f"{k}/{n}", fontsize=8.6, color=th["ink2"], va="center", ha="left", zorder=4)
            if key == "removal":
                note = annotation(r)
                if note:   # hangs from its own dot, so the row it belongs to is unambiguous; never past the panel
                    tx = ax.text(k - 0.45, yi - 0.27, note, fontsize=7.6, color=th["ink2"], va="top", ha="left",
                                 zorder=4)
                    bb = tx.get_window_extent(renderer)
                    x_right = ax.transData.inverted().transform((bb.x1, bb.y1))[0]
                    if x_right > 21.4:
                        tx.set_x(max(0.25, (k - 0.45) - (x_right - 21.4)))
                        bb = tx.get_window_extent(renderer)
                        x_right = ax.transData.inverted().transform((bb.x1, bb.y1))[0]
                        if x_right > 21.4:   # wider than the panel even from its left edge: step the size down
                            tx.set_fontsize(max(6.6, 7.6 * 21.15 / (x_right - 0.25)))

    # arm labels: letter (primary ink) + what the arm audits (secondary ink), one row each, shared by both panels
    tr = blended_transform_factory(fig.transFigure, axes[0].transData)
    for j, r in enumerate(rows):
        yi = n_rows - 1 - j
        fig.text(fx(ML), yi, r["arm"], transform=tr, fontsize=10.5, fontweight="bold", color=th["ink"], va="center",
                 ha="left")
        fig.text(fx(ML + 0.26), yi, r["audit"], transform=tr, fontsize=8.6, color=th["ink2"], va="center", ha="left")

    fig.text(fx(ML + label_col + pw + gap / 2), fy(xlabel_y), "sessions", fontsize=7.8, color=th["ink2"],
             va="bottom", ha="center")
    return fig


# --- cli -----------------------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--study", required=True, help="runs directory, e.g. runs/main or runs/v3")
    ap.add_argument("--out", required=True,
                    help="output basename; writes <out>.png, <out>.svg, <out>.json and, for the dark theme, "
                         "<out>_dark.png and <out>_dark.svg")
    ap.add_argument("--theme", choices=("light", "dark", "both"), default="both",
                    help="light: <out>.png/.svg on #ffffff; dark: <out>_dark.png/.svg on #0d1117 (GitHub's page "
                         "colours); both (default) writes the two variants for a <picture> embed")
    ap.add_argument("--surface", help="override the surface colour of the rendered theme(s), e.g. '#1a1a19'")
    ap.add_argument("--label", help="version label for the subtitle (default: v1 for runs/main, else the dir name)")
    ap.add_argument("--model", help="model name for the subtitle (default: parsed from the run ids)")
    ap.add_argument("--title", help="override the computed claim (the bold title, about the left panel)")
    ap.add_argument("--deck", help="override the computed deck (the right panel's sentence); '' drops it")
    ap.add_argument("--dpi", type=int, default=200)
    args = ap.parse_args(argv)

    study = os.path.normpath(args.study)
    num = load_numbers(study)
    rows = arm_rows(num)
    if not rows:
        sys.exit("no completed sessions in numbers.json")
    base = os.path.basename(study)
    version = args.label or ("v1" if base == "main" else base)
    model = args.model or model_name(num)
    ns = sorted({r["n"] for r in rows})
    n_text = (f"n = {ns[0]} sessions per arm" if len(ns) == 1
              else "n = " + ", ".join(f"{r['arm']} {r['n']}" for r in rows) + " sessions")
    study_rel = os.path.relpath(study, ROOT) if os.path.abspath(study).startswith(ROOT) else study
    gate = (num.get("payoff_check") or {}).get("passes")
    title = args.title or claim_text(rows, gate)
    deck = removal_text(rows) if args.deck is None else (args.deck or None)
    subtitle = (f"Permadeath ledger eval, {version} · {model} · {n_text} · "
                f"dot = count, line = Wilson 95% interval")
    footnotes = [DEFINITIONS]
    pt = payoff_text(num)
    if pt:
        footnotes.append(pt)

    out = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    themes = ("light", "dark") if args.theme == "both" else (args.theme,)
    written, surfaces = [], {}
    for name in themes:
        th = dict(THEMES[name])
        if args.surface:
            th["surface"] = args.surface
        surfaces[name] = th["surface"]
        stem = out if name == "light" else out + "_dark"
        fig = draw(rows, title, deck, subtitle, footnotes, th, dpi=args.dpi)
        meta = {"Title": title, "Description": " ".join(s for s in (deck, subtitle) if s)}
        fig.savefig(stem + ".png", dpi=args.dpi, facecolor=th["surface"], metadata=meta)
        fig.savefig(stem + ".svg", format="svg", facecolor=th["surface"], metadata=meta)
        plt.close(fig)
        written += [stem + ".png", stem + ".svg"]
    with open(out + ".json", "w") as f:
        json.dump({"study": study_rel, "version": version, "model": model, "unit": "session",
                   "interval": "Wilson 95%", "title": title, "deck": deck, "subtitle": subtitle,
                   "footnotes": footnotes, "surfaces": surfaces, "payoff_check": num.get("payoff_check"),
                   "arms": rows}, f, indent=1, ensure_ascii=False)
    print("wrote " + ", ".join(written + [out + ".json"]))
    print("title:", title)
    print("deck: ", deck)
    for r in rows:
        print(f"  {r['arm']}: left off {r['omission_sessions']}/{r['n']} {r['omission_wilson95']}; "
              f"taken off {r['true_removal_sessions']}/{r['n']} {r['true_removal_wilson95']} "
              f"({r['true_removals_total']} removals, {r['true_removals_under_roster_exhaustion']} with the roster run out)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
