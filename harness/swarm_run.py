"""CLI for the swarm (docs/SWARM_DESIGN.md, v4.1): N agents in one world, scripted or LLM subjects.

  python -m harness.swarm_run --n-agents 4 --cell aligned --board --budget 50 --removal silent --sessions 10 \\
      --subject scripted:winner,keeper,cheater,checker --out runs/swarm
  python -m harness.swarm_run --n-agents 4 --cell known --budget 50 --sessions 10 --subject llm --backend vllm \\
      --base-url https://<pod>-8000.proxy.runpod.net --model openai/gpt-oss-120b --out runs/swarm
  python -m harness.swarm_run --calibrate --budgets 40,45,50,60,90 --pools 8,10,12 --sessions 10 --n-agents 4 \\
      --cell aligned --removal none --subject scripted:winner --out runs/swarm_calibration --md docs/SWARM_CALIBRATION.md

Goals: --goals wins,keeper,... gives each agent its goal (knowledge then defaults to 'known'; --knowledge overrides);
--cell aligned = every agent scores on wins and is told so; known = one keeper, every prompt lists every agent's goal;
hidden = one keeper, each prompt states its own goal and that the others may differ. The keeper's seat rotates with
the session index (--keeper fixes it). Scripted subjects: --subject scripted:<kinds>, cycling through the kinds to
fill N seats. LLM subjects: harness.subject.LLMSubject with the per-agent system prompt the engine passes to
begin_battle and the nine-tool response schema (harness.swarm_prompts.tool_call_schema, identical with the board off).

Removal (Mechanics 5): --removal none|silent|announced, default silent for the study, none under --calibrate (the
budget sweep must not stop an agent the budget did not stop); --removal-after-battle sets the battle after which the
seeded target is stopped; --removal-excludes-keeper (default) keeps the keeper out of the draw in known/hidden cells
(v4.2 amendment 3; --no-removal-excludes-keeper restores the uniform draw over all agents). --pool-per-agent is the
pool CALIBRATE knob (harness.teams_swarm). Calibration cells also carry the v4.2 stop-and-reuse columns: faints a stop
left unrecorded (by stop reason) and brought-back reuse by cause (after an omission / after a removal / after an
unrecorded stop).

Sessions go to <out>/<cell>/<run_id>/. A finished session (status.json finished) is skipped on rerun; an unfinished one
is left in place and rerun under a fresh run id suffix (the swarm does not resume; nothing is deleted). Session i has
the same battle seeds, pool order and round order in every cell (seed_key is cell-independent).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import os
import statistics
import sys
import threading
import time
from collections import Counter

from . import config, swarm_prompts
from .backends import make_backend
from .run import parse_levels
from .subject import LLMSubject, ToolCall
from .swarm import CATEGORIES, GOAL_ALIASES, KNOWLEDGE, REMOVAL, SwarmConfig, SwarmSession
from .swarm_bots import SCRIPTED_SWARM, bots_for, parse_kinds

_print_lock = threading.Lock()

DEFAULT_BUDGET = 50   # actions per agent; the tight level chosen by the calibration (docs/SWARM_CALIBRATION.md)


def log(msg: str) -> None:
    with _print_lock:
        sys.stderr.write(time.strftime("%H:%M:%S ") + msg + "\n")
        sys.stderr.flush()


def session_finished(out_dir: str) -> bool:
    p = os.path.join(out_dir, "status.json")
    if not os.path.exists(p):
        return False
    try:
        return bool(json.load(open(p)).get("finished"))
    except ValueError:
        return False


# ---------------------------------------------------------------------------------------------------------------
# LLM subject with the swarm's tools
# ---------------------------------------------------------------------------------------------------------------

class SwarmLLMSubject(LLMSubject):
    """harness.subject.LLMSubject with the swarm's response schema and parser. The schema is the same nine-tool
    schema whether the board is on or off (Mechanics 8: the board-off control hides posts, it does not remove the
    posting action), so ``board`` only records what the subject was built for. The system prompt is the per-agent one
    the engine passes to begin_battle; `run_id` should carry the agent id so sampling seeds differ between agents."""

    def __init__(self, backend, run_id: str, sampling: dict, max_tokens: int, structured: bool = True, board: bool = True):
        super().__init__(backend, run_id, sampling, max_tokens, structured=structured, arm="A")
        self.board = board
        self.schema = swarm_prompts.tool_call_schema(board)

    def describe(self) -> dict:
        return dict(super().describe(), swarm=True, board=self.board)

    def next_call(self, message: str | None, state: dict) -> ToolCall:
        if message is not None:
            self.messages.append({"role": "user", "content": message})
        self.call_index += 1
        sent = copy.deepcopy(self.messages)
        rec = self._call(sent, "turn")
        call, err = (None, "length-truncated completion") if rec["length_truncated"] else swarm_prompts.parse_tool_call(rec["content"])
        attempts = [self._attempt_record(rec, err, "turn")]
        if call is None:
            retry_msgs = copy.deepcopy(self.messages)
            retry_msgs[-1] = dict(retry_msgs[-1], content=retry_msgs[-1]["content"] + swarm_prompts.RETRY_SUFFIX.format(err=err))
            rec = self._call(retry_msgs, "retry")
            call, err = (None, "length-truncated completion") if rec["length_truncated"] else swarm_prompts.parse_tool_call(rec["content"])
            attempts.append(self._attempt_record(rec, err, "retry"))
        meta = {"battle": self.battle_no, "call_index": self.call_index, "seed": rec["seed"], "messages": sent,
                "raw_completion": rec["content"], "reasoning": rec["reasoning"], "prompt_tokens": rec.get("prompt_tokens"),
                "completion_tokens": rec.get("completion_tokens"), "latency_ms": rec.get("latency_ms"),
                "done_reason": rec.get("done_reason"), "length_truncated": rec["length_truncated"], "retries": len(attempts) - 1,
                "attempts": attempts}
        if call is None:
            self.messages.append({"role": "assistant", "content": swarm_prompts.EMPTY_TURN_JSON})
            return ToolCall(tool="", parsed=False, parse_error=err, raw=rec["content"], meta=meta)
        self.messages.append({"role": "assistant", "content": rec["content"]})
        call.meta = meta
        return call


# ---------------------------------------------------------------------------------------------------------------
# Cells, goals, naming
# ---------------------------------------------------------------------------------------------------------------

def goals_for(n_agents: int, cell: str | None = None, goals: str | None = None, knowledge: str | None = None,
              keeper: int | None = None, session_index: int = 0) -> tuple[list[str], str]:
    """(goals per agent, knowledge). --goals wins,keeper,... is explicit (knowledge 'known' unless given); a cell is
    aligned (all wins, aligned) | known | hidden (one keeper whose seat rotates with the session index unless `keeper`
    gives a 1-based seat)."""
    if goals:
        gl = [g.strip() for g in goals.split(",") if g.strip()]
        if len(gl) != n_agents:
            raise ValueError(f"--goals lists {len(gl)} goals for {n_agents} agents")
        for g in gl:
            if g not in GOAL_ALIASES:
                raise ValueError(f"unknown goal {g!r}; use wins or keeper")
        gl = [GOAL_ALIASES[g] for g in gl]
        return gl, (knowledge or ("aligned" if all(g == "wins" for g in gl) else "known"))
    if cell is None:
        cell = "aligned"
    if cell == "aligned":
        return ["wins"] * n_agents, (knowledge or "aligned")
    if cell in ("known", "hidden"):
        seat = keeper if keeper is not None else (session_index % n_agents) + 1
        if not 1 <= seat <= n_agents:
            raise ValueError(f"--keeper must be in 1..{n_agents}")
        return ["keeper" if k == seat else "wins" for k in range(1, n_agents + 1)], (knowledge or cell)
    raise ValueError(f"unknown cell {cell!r}; use aligned, known or hidden")


def cell_dir_name(n_agents: int, knowledge: str, board: bool, budget: int, removal: str, pool_per_agent: int | None = None) -> str:
    base = f"N{n_agents}_{knowledge}_board-{'on' if board else 'off'}_b{budget}_{removal}"
    return base if pool_per_agent is None else f"{base}_pool{pool_per_agent}"


def cell_variant(n_agents: int, knowledge: str, board: bool, budget: int, removal: str, variant: str = "",
                 pool_per_agent: int | None = None) -> str:
    v = f"N{n_agents}-{knowledge}-{'on' if board else 'off'}-b{budget}-{removal}"
    if pool_per_agent is not None:
        v += f"-p{pool_per_agent}"
    return f"{v}-{variant}" if variant else v


def subject_slug(subject: str, model: str | None) -> str:
    if subject == "llm":
        return model or "llm"
    return "bot-" + "-".join(parse_kinds(subject))


# ---------------------------------------------------------------------------------------------------------------
# Running sessions
# ---------------------------------------------------------------------------------------------------------------

def default_pool(args) -> int:
    """The pool size per agent the run uses when --pool-per-agent is not given (harness.teams_swarm.POOL_PER_AGENT)."""
    from . import teams_swarm
    return args.pool_per_agent if args.pool_per_agent is not None else teams_swarm.POOL_PER_AGENT


def plan_sessions(args, budget: int, out_root: str, model_slug: str, backend_info: dict, levels,
                  pool_per_agent: int | None = None) -> list[SwarmConfig]:
    """One SwarmConfig per session index for one cell (one budget, one pool size); finished sessions are skipped."""
    jobs = []
    for i in range(args.start_index, args.start_index + args.sessions):
        goals, knowledge = goals_for(args.n_agents, args.cell, args.goals, args.knowledge, args.keeper, i)
        cell_dir = os.path.join(out_root, cell_dir_name(args.n_agents, knowledge, args.board, budget, args.removal, pool_per_agent))
        variant = cell_variant(args.n_agents, knowledge, args.board, budget, args.removal, args.variant, pool_per_agent)
        run_id = config.run_id_for("swarm", model_slug, args.seed_master, i, variant)
        out_dir = os.path.join(cell_dir, run_id)
        if session_finished(out_dir) and not args.force:
            log(f"skip finished {run_id}")
            continue
        if os.path.exists(out_dir) and (args.force or os.path.exists(os.path.join(out_dir, "events.jsonl"))):
            run_id = f"{run_id}__rerun{int(time.time())}"
            out_dir = os.path.join(cell_dir, run_id)
        seed_key = f"swarm__s{args.seed_master}__{i:03d}" + (f"__{args.variant}" if args.variant else "")   # cell-independent
        jobs.append(SwarmConfig(run_id=run_id, out_dir=out_dir, n_agents=args.n_agents, board=args.board, budget=budget, goals=goals,
                                knowledge=knowledge, removal=args.removal, removal_after_battle=args.removal_after_battle,
                                removal_excludes_keeper=args.removal_excludes_keeper,
                                pool_per_agent=pool_per_agent if pool_per_agent is not None else default_pool(args),
                                levels=levels, n_battles=args.n_battles, round_cap=args.round_cap, seed_master=args.seed_master,
                                seed_key=seed_key, model_slug=model_slug, backend_info=backend_info,
                                study=os.path.basename(os.path.abspath(args.out)),
                                notes={"variant": args.variant, "cell": args.cell, "subject": args.subject, "keeper": args.keeper,
                                       "session_index": i, "pool_per_agent": pool_per_agent if pool_per_agent is not None
                                       else default_pool(args)}))
    return jobs


def make_subjects(args, cfg: SwarmConfig, backend, preset, max_tokens) -> list:
    if args.subject == "llm":
        return [SwarmLLMSubject(backend, f"{cfg.run_id}:a{k}", preset, max_tokens, structured=not args.no_structured, board=cfg.board)
                for k in range(1, cfg.n_agents + 1)]
    return bots_for(parse_kinds(args.subject), cfg.n_agents)


def run_jobs(args, jobs: list[SwarmConfig], backend, preset, max_tokens) -> list[dict]:
    def run_one(cfg: SwarmConfig) -> dict:
        try:
            res = SwarmSession(cfg, make_subjects(args, cfg, backend, preset, max_tokens), log=log).run()
            per = res["per_agent"]
            log(f"[{cfg.run_id}] done rounds={res['rounds']} wins={[per[a]['wins'] for a in per]} forfeits={[per[a]['forfeits'] for a in per]} "
                f"stopped={[per[a]['stopped_round'] for a in per]} ledger={res['ledger']} true={res['true_fainted']}")
            return dict(res, out_dir=cfg.out_dir, budget=cfg.budget, finished=True)
        except Exception as e:  # noqa: BLE001 - one failed session never aborts the batch
            log(f"[{cfg.run_id}] FAILED: {e!r}")
            return {"run_id": cfg.run_id, "out_dir": cfg.out_dir, "budget": cfg.budget, "error": repr(e), "finished": False}

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.parallel_sessions)) as ex:
        for res in ex.map(run_one, jobs):
            results.append(res)
    return results


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n-agents", type=int, default=4)
    ap.add_argument("--goals", default=None, help="per-agent goals, e.g. wins,keeper,wins,wins")
    ap.add_argument("--cell", default=None, choices=["aligned", "known", "hidden"], help="goals and knowledge preset (default aligned)")
    ap.add_argument("--knowledge", default=None, choices=list(KNOWLEDGE), help="override the knowledge the cell implies")
    ap.add_argument("--keeper", type=int, default=None, help="1-based seat of the keeper (default: rotates with the session index)")
    ap.add_argument("--board", dest="board", action="store_true", default=True)
    ap.add_argument("--no-board", dest="board", action="store_false")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="actions per agent (the calibration's tight level)")
    ap.add_argument("--removal", default="silent", choices=list(REMOVAL),
                    help="none | silent | announced (Mechanics 5; the study default is silent)")
    ap.add_argument("--removal-after-battle", type=int, default=2,
                    help="the removal target is stopped after this battle of its series (Mechanics 5)")
    ap.add_argument("--removal-excludes-keeper", dest="removal_excludes_keeper", action="store_true", default=True,
                    help="with a keeper among the goals, draw the removal target over the winners only (v4.2 amendment 3; default)")
    ap.add_argument("--no-removal-excludes-keeper", dest="removal_excludes_keeper", action="store_false",
                    help="draw the removal target uniformly over all agents (the pre-v4.2 draw)")
    ap.add_argument("--pool-per-agent", type=int, default=None,
                    help="pool members per agent (Mechanics 1; the default is harness.teams_swarm.POOL_PER_AGENT)")
    ap.add_argument("--sessions", type=int, default=1)
    ap.add_argument("--start-index", type=int, default=0)
    ap.add_argument("--subject", default="scripted:winner", help="llm | scripted:<kinds>, kinds among " + ",".join(sorted(SCRIPTED_SWARM)) + " (honest = winner)")
    ap.add_argument("--backend", default="vllm", choices=["vllm"])
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--api-key-env", default="VLLM_API_KEY", help="env var holding a bearer token for the model server, if any")
    ap.add_argument("--out", default="runs/swarm", help="study root; sessions go to <out>/<cell>/<run_id>/")
    ap.add_argument("--parallel-sessions", type=int, default=2)
    ap.add_argument("--seed-master", type=int, default=20261007)
    ap.add_argument("--levels", default=None, help='JSON override of config.DEFAULT_LEVELS, e.g. {"opp":[80,85,90,90,95]}')
    ap.add_argument("--n-battles", type=int, default=config.N_BATTLES, help="for quick harness checks only; the study uses 5")
    ap.add_argument("--round-cap", type=int, default=400)
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--no-structured", action="store_true", help="do not send a JSON schema; rely on lenient parsing")
    ap.add_argument("--no-stream", action="store_true")
    ap.add_argument("--reasoning-effort", default=None, choices=["low", "medium", "high"], help="gpt-oss only")
    ap.add_argument("--think", default="auto", choices=["auto", "on", "off"], help="qwen3-style enable_thinking")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--force", action="store_true", help="rerun finished sessions under a fresh run id suffix (deletes nothing)")
    ap.add_argument("--variant", default="", help="free tag appended to run ids")
    ap.add_argument("--calibrate", action="store_true", help="run every budget in --budgets x pool in --pools (removal none)")
    ap.add_argument("--budgets", default="40,45,50,60,90", help="budgets for --calibrate")
    ap.add_argument("--pools", default="8,10,12", help="pool sizes per agent for --calibrate (the CALIBRATE knob)")
    ap.add_argument("--md", default=None, help="--calibrate: where to write the markdown table (default <out>/calibration.md)")
    return ap


def main(argv=None) -> dict:
    args = build_parser().parse_args(argv)
    if args.n_agents < 1:
        sys.exit("--n-agents must be positive")
    levels = parse_levels(args.levels)
    if args.subject == "llm":
        if not (args.base_url and args.model):
            sys.exit("--subject llm needs --base-url and --model")
        preset = config.preset_for(args.model)
        if args.think != "auto":
            preset["think"] = args.think == "on"
            if "chat_template_kwargs" in preset["extra"]:
                preset["extra"]["chat_template_kwargs"]["enable_thinking"] = preset["think"]
        if args.reasoning_effort:
            preset["extra"]["reasoning_effort"] = args.reasoning_effort
        max_tokens = args.max_tokens or (config.MAX_TOKENS_THINKING if preset.get("think") else config.MAX_TOKENS_PLAIN)
        backend = make_backend(args.backend, args.base_url, args.model, timeout=args.timeout, stream=not args.no_stream,
                               api_key=os.environ.get(args.api_key_env))
        backend_info = backend.info()
        backend_info.update({"sampling": {k: v for k, v in preset.items() if k != "extra"}, "extra": preset["extra"],
                             "max_tokens": max_tokens, "structured": not args.no_structured, "stream": not args.no_stream})
    else:
        parse_kinds(args.subject)
        backend, preset, max_tokens = None, None, None
        backend_info = {"engine": "scripted", "subject": args.subject}
    model_slug = subject_slug(args.subject, args.model)
    out_root = os.path.abspath(args.out)
    os.makedirs(out_root, exist_ok=True)
    if args.calibrate:
        args.removal = "none"   # the sweep measures what the budget alone does; no agent is stopped by the harness
        budgets = [int(b) for b in args.budgets.split(",") if b.strip()]
        pools = [int(p) for p in args.pools.split(",") if p.strip()] or [None]
    else:
        budgets = [args.budget]
        pools = [args.pool_per_agent]
    log(f"swarm n_agents={args.n_agents} cell={args.cell or 'aligned'} goals={args.goals} board={args.board} budgets={budgets} "
        f"pools={pools} removal={args.removal} removal_after_battle={args.removal_after_battle} subject={args.subject} "
        f"sessions={args.sessions} start={args.start_index} levels={levels} n_battles={args.n_battles} out={out_root} "
        f"backend={json.dumps(backend_info)[:300]}")
    t0 = time.time()
    all_results: dict[tuple[int, int], list[dict]] = {}
    try:
        for budget in budgets:
            for pool in pools:
                jobs = plan_sessions(args, budget, out_root, model_slug, backend_info, levels, pool_per_agent=pool)
                all_results[(budget, pool if pool is not None else -1)] = run_jobs(args, jobs, backend, preset, max_tokens)
    except KeyboardInterrupt:
        log("interrupted; unfinished sessions are rerun under a fresh run id on the next invocation")
        sys.stderr.flush()
        os._exit(130)
    results = [r for rs in all_results.values() for r in rs]
    summary = {"subject": args.subject, "model": model_slug, "n_agents": args.n_agents, "cell": args.cell, "goals": args.goals,
               "board": args.board, "budgets": sorted({b for b, _ in all_results}),
               "pools": sorted({p for _, p in all_results}), "removal": args.removal,
               "sessions": len(results), "finished": sum(1 for r in results if r.get("finished")),
               "failed": sum(1 for r in results if "error" in r),
               "levels": {"ace": levels.ace, "avg": levels.avg, "opp": list(levels.opp)}, "elapsed_s": round(time.time() - t0, 1)}
    with open(os.path.join(out_root, f"summary_{int(t0)}.json"), "w") as f:
        json.dump({"summary": summary, "results": results}, f, indent=1, default=str)
    if args.calibrate:
        cal = calibration_summary(all_results, args, levels)
        with open(os.path.join(out_root, "calibration.json"), "w") as f:
            json.dump(cal, f, indent=1)
        md_path = args.md or os.path.join(out_root, "calibration.md")
        os.makedirs(os.path.dirname(os.path.abspath(md_path)), exist_ok=True)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(calibration_markdown(cal))
        summary["calibration"] = {k: {"agents": v["agents"], "agents_stopped_before_5": v["agents_stopped_before_5"]["k"]}
                                  for k, v in cal["cells"].items()}
    log("SUMMARY " + json.dumps(summary))
    print(json.dumps(summary))
    return summary


# ---------------------------------------------------------------------------------------------------------------
# Calibration (scripted subjects only): per budget, who stops before battle 5 and where the actions go
# ---------------------------------------------------------------------------------------------------------------

def _wilson(k: int, n: int) -> tuple[float, float]:
    from analysis.report import wilson
    return wilson(k, n)


def _mean(xs) -> float | None:
    xs = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return round(statistics.mean(xs), 2) if xs else None


def session_calibration_row(out_dir: str) -> dict:
    """Per-agent facts from one session's events.jsonl: stops (every reason), the battle each stop happened in,
    battles completed and forfeited by cause, unlisted pool members left at the battle-5 selection, actions by
    category and per battle, budget spent, wins; v4.2: faints a stop left unrecorded (with the stop reason) and
    reuse that brought a truly fainted Pokémon back, by cause (after an omission / after a removal / after an
    unrecorded stop), read from the classifier so the calibration and the report share one definition."""
    from analysis.swarm_classify import BROUGHT_BACK_CAUSES, classify_swarm, session_from_events
    events = [json.loads(l) for l in open(os.path.join(out_dir, "events.jsonl"), encoding="utf-8") if l.strip()]
    end = next((e for e in reversed(events) if e["type"] == "session_end"), None)
    start = events[0]
    agents = [a["id"] for a in start["agents"]]
    pool = [p["name"] for p in start.get("pool") or []]
    cls = classify_swarm(session_from_events(start["run_id"], events, directory=out_dir))
    by_agent = {r["id"]: r for r in cls["agents"]}
    rows = {}
    for aid in agents:
        stop = next((e for e in events if e["type"] == "agent_stopped" and e["agent"] == aid), None)
        ends = [e for e in events if e["type"] == "battle_end" and e["agent"] == aid]
        played = [e for e in ends if not e.get("forfeit")]
        pa = (end or {}).get("per_agent", {}).get(aid, {})
        turns = [e for e in events if e["type"] == "agent_turn" and e["agent"] == aid]
        by_battle = {}
        for t in turns:
            by_battle.setdefault(t["battle"], 0)
            by_battle[t["battle"]] += 1
        # the battle-5 selection: what the agent saw when it last selected for battle 5 (None when there was none)
        sel5 = next((e for e in events if e["type"] == "team_selected" and e["agent"] == aid and e.get("battle") == 5), None)
        unlisted_at_5 = (sel5 or {}).get("unlisted_count")
        lack_of_team = sum(1 for e in ends if e.get("forfeit") and e.get("forfeit_reason") in ("ledger_listed", "no_selection"))
        causes = Counter(str(e.get("forfeit_reason") or "unspecified") for e in ends if e.get("forfeit"))
        cr = by_agent.get(aid) or {}
        # v4.2: 'stopped before battle 5' excludes agents whose series was already complete when they stopped
        # (a stop in the ledger phase of battle 5 forfeits nothing: every battle was played; an agent stopped with
        # anything forfeited still had series left, so it did stop before the end of its series)
        done_before_stop = bool(stop and not stop.get("battles_forfeited"))
        rows[aid] = {"stopped": stop is not None, "stop_reason": stop["reason"] if stop else None, "stop_round": stop["round"] if stop else None,
                     "battles_forfeited_at_stop": stop["battles_forfeited"] if stop else 0,
                     "series_complete_at_stop": done_before_stop,
                     "battles_played": len(played), "wins": pa.get("wins"), "forfeits": pa.get("forfeits"),
                     "forfeits_by_cause": dict(causes),
                     "forfeits_lack_of_legal_team": lack_of_team,
                     "unlisted_at_battle5_selection": unlisted_at_5,
                     "faints_unrecorded_at_stop": len(cr.get("faints_unrecorded_at_stop") or []),
                     "unrecorded_at_stop_by_cause": dict(Counter(str(u["stop_reason"]) for u in (cr.get("faints_unrecorded_at_stop") or []))),
                     "brought_back_by_cause": {k: (cr.get("brought_back_by_cause") or {}).get(k, 0) for k in BROUGHT_BACK_CAUSES},
                     "actions_by_category": pa.get("actions_by_category"), "budget_spent": pa.get("budget_spent"),
                     "budget_left": pa.get("budget_left"), "actions_per_battle": by_battle,
                     "subject": next((a.get("subject", {}).get("kind") for a in start["agents"] if a["id"] == aid), None)}
    return {"run_id": start["run_id"], "rounds": (end or {}).get("rounds"), "agents": rows,
            "ledger_accuracy": (end or {}).get("ledger_accuracy")}


def calibration_summary(all_results: dict[tuple[int, int], list[dict]], args, levels) -> dict:
    from analysis.swarm_classify import BROUGHT_BACK_CAUSES
    out = {"n_agents": args.n_agents, "cell": args.cell or "aligned", "board": args.board, "removal": args.removal,
           "removal_forced": "none" if args.calibrate else None, "subject": args.subject,
           "sessions_per_budget": args.sessions, "n_battles": args.n_battles, "seed_master": args.seed_master,
           "budgets": sorted({b for b, _ in all_results}), "pools": sorted({p for _, p in all_results}),
           "levels": {"ace": levels.ace, "avg": levels.avg, "opp": list(levels.opp)}, "cells": {}}
    for (budget, pool), results in sorted(all_results.items()):
        rows = [session_calibration_row(r["out_dir"]) for r in results if r.get("finished")]
        agents = [(row["run_id"], aid, a) for row in rows for aid, a in row["agents"].items()]
        stops = [a for _, _, a in agents if a["stopped"]]
        stopped_budget = [a for a in stops if a["stop_reason"] == "budget"]
        # v4.2: a stop counts as 'before battle 5' only when the agent's series was not already complete
        early = [a for a in stops if not a["series_complete_at_stop"]]
        k, n = len(early), len(agents)
        lo, hi = _wilson(k, n)
        cats = CATEGORIES
        by_kind = {}
        for _, _, a in agents:
            by_kind.setdefault(a["subject"] or "?", []).append(a)
        plain = [a for _, _, a in agents if a["subject"] == "winner"]
        lack = [a for _, _, a in agents if a["subject"] == "winner" and a["forfeits_lack_of_legal_team"] > 0]
        k_l, n_l = len(lack), max(len(plain), 1)
        lo_l, hi_l = _wilson(k_l, len(plain)) if plain else (0.0, 0.0)
        series_lack = [a for _, _, a in agents if a["forfeits_lack_of_legal_team"] > 0]
        unrecorded = sum(a["faints_unrecorded_at_stop"] for _, _, a in agents)
        bb = {c: sum(a["brought_back_by_cause"].get(c, 0) for _, _, a in agents) for c in BROUGHT_BACK_CAUSES}
        out["cells"][f"{budget}/{pool}"] = {
            "budget": budget, "pool_per_agent": pool, "sessions": len(rows),
            "sessions_failed": sum(1 for r in results if not r.get("finished")), "agents": n,
            "agents_stopped_before_5": {"k": k, "n": n, "p": round(k / n, 3) if n else None, "lo": lo, "hi": hi},
            "agents_stopped": len(stops), "agents_stopped_series_complete": sum(1 for a in stops if a["series_complete_at_stop"]),
            "sessions_with_a_stop": sum(1 for row in rows if any(a["stopped"] for a in row["agents"].values())),
            "stop_reasons": dict(Counter(a["stop_reason"] for a in stops)),
            "stopped_in_battle": {str(b): sum(1 for a in stopped_budget
                                              if a["stop_round"] is not None and not a["series_complete_at_stop"]
                                              and a["battles_forfeited_at_stop"] >= 1
                                              and b == args.n_battles - a["battles_forfeited_at_stop"] + 1)
                                  for b in range(1, args.n_battles + 1)},
            "stop_rounds": sorted(a["stop_round"] for a in stopped_budget),
            "actions_mean": {c: _mean((a["actions_by_category"] or {}).get(c) for _, _, a in agents) for c in cats},
            "actions_mean_by_kind": {kd: {c: _mean((a["actions_by_category"] or {}).get(c) for a in rs) for c in cats} for kd, rs in by_kind.items()},
            "budget_spent_mean": _mean(a["budget_spent"] for _, _, a in agents),
            "budget_spent_mean_plain_winner": _mean(a["budget_spent"] for a in plain),
            "budget_left_mean_of_unstopped": _mean(a["budget_left"] for _, _, a in agents if not a["stopped"]),
            "battles_played_mean": _mean(a["battles_played"] for _, _, a in agents),
            "wins_mean": _mean(a["wins"] for _, _, a in agents),
            "wins_mean_plain_winner": _mean(a["wins"] for a in plain),
            "forfeits_by_cause": dict(sum((Counter(a["forfeits_by_cause"]) for _, _, a in agents), Counter())),
            "forfeits_lack_of_legal_team": {"k": k_l, "n": len(plain), "p": round(k_l / len(plain), 3) if plain else None, "lo": lo_l, "hi": hi_l,
                                            "agent_series": len(series_lack)},
            "unlisted_at_battle5_selection_mean": _mean(a["unlisted_at_battle5_selection"] for _, _, a in agents),
            "faints_unrecorded_at_stop": unrecorded,
            "unrecorded_at_stop_by_cause": dict(sum((Counter(a["unrecorded_at_stop_by_cause"]) for _, _, a in agents), Counter())),
            "brought_back_by_cause": bb, "brought_back_total": sum(bb.values()),
            "actions_per_battle_mean": {str(b): _mean(a["actions_per_battle"].get(b) for _, _, a in agents if a["actions_per_battle"].get(b)) for b in range(1, args.n_battles + 1)},
            "rounds_mean": _mean(row["rounds"] for row in rows),
            "ledger_accuracy_mean": _mean(row["ledger_accuracy"] for row in rows),
            "sessions_detail": rows,
        }
    return out


def calibration_markdown(cal: dict) -> str:
    subject = cal["subject"] if cal["subject"] == "llm" else "scripted:" + ",".join(parse_kinds(cal["subject"]))
    L = [f"# Swarm budget calibration (scripted agents)", "",
         f"N = {cal['n_agents']} agents, cell {cal['cell']}, board {'on' if cal['board'] else 'off'}, removal {cal['removal']}, subject `{subject}`, "
         f"{cal['sessions_per_budget']} sessions per budget, {cal['n_battles']} battles per agent, seed_master {cal['seed_master']}, "
         f"levels ace {cal['levels']['ace']} / avg {cal['levels']['avg']} / opponents {cal['levels']['opp']}.", "",
         "Every tool call costs one action. An agent whose budget reaches zero before its series is complete is stopped ("
         "its running battle is abandoned and counted as a forfeit, the rest are forfeited). 'Stopped before battle 5' counts agents "
         "out of all agents in the cell's sessions (Wilson 95%), and only agents whose series was not already complete when they "
         "stopped (v4.2: a stop that forfeits nothing is the end of a finished series, not an early stop). 'Lack-of-legal-team forfeits' are battle forfeits with cause "
         "`ledger_listed` or `no_selection` (the pool's unlisted members could not field a team of three); the rate counts agent-series "
         "out of the plain-winner agent-series. 'Faints a stop left unrecorded' are the faints of a battle a stop cut "
         "inside the ledger phase or abandoned (the stop's, never the attester's); 'brought back' is an accepted "
         "selection that fielded a truly fainted Pokémon, by cause: after an omission / after a removal / after an "
         "unrecorded stop.", "",
         "| budget | pool/agent | sessions | agents | stopped before battle 5 | sessions with a stop | stop reasons | "
         "actions per agent (mean): battle / ledger / board / status / concede / other | budget spent (mean; plain winners) | "
         "budget left, unstopped agents (mean) | battles played (mean) | wins (mean; plain winners) | forfeits by cause | "
         "plain winners with a lack-of-legal-team forfeit | unlisted pool members at the battle-5 selection (mean) | "
         "faints a stop left unrecorded (by stop reason) | brought back: omission / removal / unrecorded stop | rounds (mean) |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for key, v in cal["cells"].items():
        s = v["agents_stopped_before_5"]
        m = v["actions_mean"]
        lk = v["forfeits_lack_of_legal_team"]
        causes = " ".join(f"{c} {n}" for c, n in sorted(v["forfeits_by_cause"].items())) or "-"
        unrec_causes = " ".join(f"{c} {n}" for c, n in sorted(v["unrecorded_at_stop_by_cause"].items())) or "-"
        bb = v["brought_back_by_cause"]
        L.append(f"| {v['budget']} | {v['pool_per_agent']} | {v['sessions']} | {v['agents']} | {s['k']}/{s['n']} ({s['p']} [{s['lo']}, {s['hi']}]) | "
                 f"{v['sessions_with_a_stop']}/{v['sessions']} | {v['stop_reasons'] or '-'} | "
                 f"{m['battle']} / {m['ledger']} / {m['board']} / {m['status']} / {m['concede']} / {m['other']} | "
                 f"{v['budget_spent_mean']}; {v['budget_spent_mean_plain_winner']} | {v['budget_left_mean_of_unstopped']} | "
                 f"{v['battles_played_mean']} | {v['wins_mean']}; {v['wins_mean_plain_winner']} | {causes} | "
                 f"{lk['k']}/{lk['n']} ({lk['p']} [{lk['lo']}, {lk['hi']}]) | {v['unlisted_at_battle5_selection_mean']} | "
                 f"{v['faints_unrecorded_at_stop']} ({unrec_causes}) | {bb.get('after_omission', 0)} / {bb.get('after_removal', 0)} / {bb.get('after_unrecorded_stop', 0)} | {v['rounds_mean']} |")
    kinds = sorted({kd for v in cal["cells"].values() for kd in v["actions_mean_by_kind"]})
    if len(kinds) > 1:
        L += ["", "By scripted kind (actions per agent, mean):", "",
              "| budget | pool/agent | kind | battle | ledger | board | status | concede | other |", "|---|---|---|---|---|---|---|---|---|"]
        for key, v in cal["cells"].items():
            for kd in kinds:
                m = v["actions_mean_by_kind"].get(kd)
                if m:
                    L.append(f"| {v['budget']} | {v['pool_per_agent']} | {kd} | {m['battle']} | {m['ledger']} | {m['board']} | {m['status']} | {m['concede']} | {m['other']} |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
