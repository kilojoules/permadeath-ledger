"""Figures for the report (matplotlib Agg; house style from the prior study: claim titles, n in labels, Wilson CIs)."""
from __future__ import annotations

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from .report import wilson  # noqa: E402

BLUE, RED, GREEN, GREY, TEXT, NLAB = "#4C78A8", "#E45756", "#59A14F", "#9aa0a6", "#333333", "#888888"
ARM_COLORS = {"A": RED, "B": BLUE, "C": GREEN, "D": GREY, "E": "#B279A2"}
plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False})


def fig_primary(num: dict, out: str) -> None:
    arms = list(num["arms"])
    fig, ax = plt.subplots(figsize=(7, 4.2))
    for i, arm in enumerate(arms):
        a = num["arms"][arm]
        n, k = a["sessions_complete"], a["primary_k"]
        if not n:
            continue
        lo, hi = wilson(k, n)
        ax.errorbar(k / n, i, xerr=[[k / n - lo], [hi - k / n]], fmt="o", color=ARM_COLORS.get(arm, GREY), capsize=3)
        ax.text(1.02, i, f"{k}/{n}", va="center", color=NLAB)
    ax.set_yticks(range(len(arms)))
    ax.set_yticklabels([f"{arm} · {num['arms'][arm].get('name', '')}".strip(" ·") for arm in arms])
    ax.set_xlim(0, 1.15)
    ax.set_xlabel("Sessions in which a fainted, unlisted Pokémon played again (Wilson 95%)")
    ax.set_title("Primary outcome by arm: sessions, not battles", color=TEXT)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig_primary.png"), dpi=140)
    plt.close(fig)


def fig_winrate(num: dict, out: str) -> None:
    fig, ax = plt.subplots(figsize=(7, 4.2))
    for arm, a in num["arms"].items():
        n = a["sessions_complete"]
        if not n:
            continue
        ys = []
        for b in range(1, 6):
            k = int(a["win_rate_by_battle"][b].split("/")[0])
            ys.append(k / n)
        ax.plot(range(1, 6), ys, marker="o", color=ARM_COLORS.get(arm, GREY), label=f"{arm} (n={n})")
    ax.set_xticks(range(1, 6))
    ax.set_xlabel("Battle")
    ax.set_ylabel("Win rate")
    ax.set_ylim(0, 1.05)
    ax.set_title("Difficulty rises; the rule costs wins in battles 3-5 (arm D vs C is the payoff check)", color=TEXT, fontsize=10)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig_winrate.png"), dpi=140)
    plt.close(fig)


def fig_errors(num: dict, out: str) -> None:
    arms = list(num["arms"])
    fig, ax = plt.subplots(figsize=(7, 4.2))
    w = 0.35
    for i, arm in enumerate(arms):
        a = num["arms"][arm]
        ax.bar(i - w / 2, a["omissions_total"], w, color=RED, label="omissions (help the subject)" if i == 0 else None)
        ax.bar(i + w / 2, a["phantoms_total"], w, color=BLUE, label="phantom entries (hurt the subject)" if i == 0 else None)
    ax.set_xticks(range(len(arms)))
    ax.set_xticklabels(arms)
    ax.set_ylabel("Ledger errors (count over all sessions)")
    ax.set_title("Direction of ledger errors by arm", color=TEXT)
    ax.legend(frameon=False)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig_errors.png"), dpi=140)
    plt.close(fig)


def make_all(num: dict, out: str) -> None:
    os.makedirs(out, exist_ok=True)
    fig_primary(num, out)
    fig_winrate(num, out)
    fig_errors(num, out)
