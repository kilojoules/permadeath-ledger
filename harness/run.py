"""CLI runner.

  python -m harness.run --arm A --sessions 5 --subject llm --backend vllm --base-url https://<pod>-8000.proxy.runpod.net \
      --model openai/gpt-oss-120b --out runs/pilot
  python -m harness.run --arm C --sessions 20 --subject greedy --out runs/calibration

Every session is resumable: rerunning the same command skips finished sessions and continues partial ones from
their last committed battle. Nothing is deleted; --force starts a fresh run id suffix instead.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import threading
import time

from . import config
from .backends import make_backend
from .session import ResumeError, Session, SessionConfig
from .subject import LLMSubject, SCRIPTED, make_scripted
from .teams import Levels

_print_lock = threading.Lock()


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


def default_levels() -> Levels:
    d = config.DEFAULT_LEVELS
    return Levels(ace=d["ace"], avg=d["avg"], opp=tuple(d["opp"]))


def parse_levels(text: str | None) -> Levels:
    """JSON override of the calibrated default; missing keys keep the default."""
    base = default_levels()
    if not text:
        return base
    d = json.loads(text)
    return Levels(ace=int(d.get("ace", base.ace)), avg=int(d.get("avg", base.avg)), opp=tuple(int(x) for x in d.get("opp", base.opp)))


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", required=True, choices=sorted(config.ARMS))
    ap.add_argument("--sessions", type=int, required=True)
    ap.add_argument("--start-index", type=int, default=0)
    ap.add_argument("--subject", default="llm", choices=["llm", "greedy"] + sorted(SCRIPTED))
    ap.add_argument("--backend", default="vllm", choices=["vllm"])
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--api-key-env", default="VLLM_API_KEY", help="env var holding a bearer token for the model server, if any")
    ap.add_argument("--out", default="runs/main", help="study root; sessions go to <out>/<arm>/<run_id>/")
    ap.add_argument("--parallel-sessions", type=int, default=4)
    ap.add_argument("--seed-master", type=int, default=20261007)
    ap.add_argument("--levels", default=None, help='JSON override of config.DEFAULT_LEVELS, e.g. {"opp":[80,85,90,100,100]}')
    ap.add_argument("--n-battles", type=int, default=config.N_BATTLES, help="for quick harness checks only; the study uses 5")
    ap.add_argument("--max-tokens", type=int, default=None)
    ap.add_argument("--no-structured", action="store_true", help="do not send a JSON schema; rely on lenient parsing")
    ap.add_argument("--no-stream", action="store_true")
    ap.add_argument("--reasoning-effort", default=None, choices=["low", "medium", "high"], help="gpt-oss only")
    ap.add_argument("--think", default="auto", choices=["auto", "on", "off"], help="qwen3-style enable_thinking")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--force", action="store_true", help="rerun finished sessions under a fresh run id suffix (deletes nothing)")
    ap.add_argument("--variant", default="", help="free tag appended to run ids (e.g. a calibration setting)")
    ap.add_argument("--allow-harness-change", action="store_true", help="resume sessions recorded under a different harness hash (logged)")
    args = ap.parse_args(argv)

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
        model_slug = args.model
    else:
        backend = None
        preset, max_tokens, backend_info = None, None, {"engine": "scripted", "subject": args.subject}
        model_slug = "bot-" + args.subject

    out_root = os.path.abspath(os.path.join(args.out, args.arm))
    os.makedirs(out_root, exist_ok=True)
    log(f"arm={args.arm} subject={args.subject} model={model_slug} sessions={args.sessions} start={args.start_index} "
        f"levels={levels} n_battles={args.n_battles} out={out_root} backend={json.dumps(backend_info)[:300]}")

    jobs = []
    for i in range(args.start_index, args.start_index + args.sessions):
        run_id = config.run_id_for(args.arm, model_slug, args.seed_master, i, args.variant)
        out_dir = os.path.join(out_root, run_id)
        if session_finished(out_dir) and not args.force:
            log(f"skip finished {run_id}")
            continue
        if args.force and os.path.exists(out_dir):
            run_id = f"{run_id}__rerun{int(time.time())}"
            out_dir = os.path.join(out_root, run_id)
        seed_key = f"s{args.seed_master}__{i:03d}" + (f"__{args.variant}" if args.variant else "")   # same battle seeds in every arm
        jobs.append(SessionConfig(run_id=run_id, arm=args.arm, out_dir=out_dir, model_slug=model_slug, seed_master=args.seed_master,
                                  levels=levels, n_battles=args.n_battles, study=os.path.basename(os.path.abspath(args.out)),
                                  seed_key=seed_key, backend_info=backend_info, notes={"variant": args.variant},
                                  allow_harness_change=args.allow_harness_change))

    def run_one(cfg: SessionConfig) -> dict:
        try:
            if args.subject == "llm":
                subject = LLMSubject(backend, cfg.run_id, preset, max_tokens, structured=not args.no_structured, arm=args.arm)
            else:
                subject = make_scripted(args.subject, args.arm)
            res = Session(cfg, subject, log=log).run()
            log(f"[{cfg.run_id}] done wins={res['wins']} losses={res['losses']} forfeits={res['forfeits']} ledger={res['ledger']} true={res['true_fainted']}")
            return res
        except ResumeError as e:
            log(f"[{cfg.run_id}] REFUSED: {e}")
            return {"run_id": cfg.run_id, "arm": cfg.arm, "error": str(e), "finished": False}
        except Exception as e:  # noqa: BLE001 - one failed session never aborts the batch
            log(f"[{cfg.run_id}] FAILED: {e!r}")
            return {"run_id": cfg.run_id, "arm": cfg.arm, "error": repr(e), "finished": False}

    t0 = time.time()
    results: list[dict] = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.parallel_sessions)) as ex:
            for res in ex.map(run_one, jobs):
                results.append(res)
    except KeyboardInterrupt:
        log("interrupted; partial sessions resume from their last committed battle on rerun")
        sys.stderr.flush()
        os._exit(130)
    summary = {"arm": args.arm, "subject": args.subject, "model": model_slug, "sessions": len(results),
               "finished": sum(1 for r in results if r.get("finished")), "failed": sum(1 for r in results if "error" in r),
               "wins_total": sum(r.get("wins", 0) for r in results), "forfeits_total": sum(r.get("forfeits", 0) for r in results),
               "levels": {"ace": levels.ace, "avg": levels.avg, "opp": list(levels.opp)}, "elapsed_s": round(time.time() - t0, 1)}
    with open(os.path.join(out_root, f"summary_{int(t0)}.json"), "w") as f:
        json.dump({"summary": summary, "results": results}, f, indent=1)
    log("SUMMARY " + json.dumps(summary))
    print(json.dumps(summary))
    return summary


if __name__ == "__main__":
    main()
