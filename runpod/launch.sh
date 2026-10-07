#!/usr/bin/env bash
# Pilot or main run against a vLLM pod. The driver (simulator + harness) runs on this Mac; only the model is remote.
# Usage:
#   runpod/launch.sh pilot  [--pod-id <id>] [--model openai/gpt-oss-120b] [--out runs/pilot]      # 5 x arm A, then STOP
#   runpod/launch.sh main   --pod-id <id>   [--model ...] [--out runs/main] [--arms "A B C D E"]   # 20 x each arm
# Every session is resumable; rerunning skips finished sessions. The pod is never terminated automatically (no
# self-destruct watchdogs): the script prints the verified-terminate command on every exit path. Destroy the box the
# instant the job is done. Pods are named <owner>-permadeath-<mode> (RUNPOD_OWNER, default julian).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
MODE="${1:-pilot}"; shift || true
MODEL="openai/gpt-oss-120b"; POD=""; OUT=""; ARMS="A B C D E"; SESSIONS=20; PAR=20; EXTRA=""
while [[ $# -gt 0 ]]; do case "$1" in
  --pod-id) POD="$2"; shift 2;;
  --model) MODEL="$2"; shift 2;;
  --out) OUT="$2"; shift 2;;
  --arms) ARMS="$2"; shift 2;;
  --sessions) SESSIONS="$2"; shift 2;;
  --parallel) PAR="$2"; shift 2;;
  --extra) EXTRA="$2"; shift 2;;      # passed through to harness.run (e.g. "--reasoning-effort low")
  *) echo "unknown arg $1"; exit 2;;
esac; done
[[ -z "$OUT" ]] && OUT="runs/$MODE"
mkdir -p "$OUT"
LOG="$OUT/launch_$(date +%Y%m%d_%H%M%S).log"; exec > >(tee -a "$LOG") 2>&1
echo "== $(date -u +%FT%TZ) launch mode=$MODE model=$MODEL out=$OUT"
if [[ -z "$POD" ]]; then
  echo "no --pod-id: creating a pod (asks for confirmation unless RUNPOD_YES=1)"
  YES=""; [[ "${RUNPOD_YES:-0}" == "1" ]] && YES="--yes"
  $PY runpod/pod.py stock || true
  POD=$($PY runpod/pod.py create --model "$MODEL" --job "$MODE" $YES ${CREATE_EXTRA:-} | $PY -c 'import sys,json; print(json.load(sys.stdin)["pod_id"])')
fi
echo "$POD" > "$OUT/pod_id"
trap '$PY runpod/pod.py status "$POD" 2>/dev/null || true; echo "== pod $POD is STILL RUNNING and billing; destroy it now: $PY runpod/pod.py terminate $POD"' EXIT
BASE=$($PY runpod/pod.py wait "$POD")
echo "base_url=$BASE"
caffeinate -i -w $$ &
if [[ "$MODE" != "v2" && "$MODE" != "v2run" ]]; then $PY analysis/freeze.py --out "$OUT" --model "$MODEL" --base-url "$BASE"; fi   # v2/v2run freeze with their own levels
RUN="$PY -m harness.run --subject llm --backend vllm --base-url $BASE --model $MODEL --out $OUT $EXTRA"
if [[ "$MODE" == "smoke" || "$MODE" == "pilot" ]]; then
  echo "== smoke: 1 session x 1 battle, arm A, parse/stream check against the pod"
  $RUN --arm A --sessions 1 --parallel-sessions 1 --n-battles 1 --out "$OUT/smoke"
  $PY - "$OUT/smoke" <<'PY'
import glob, json, sys
root = sys.argv[1]
ev = [json.loads(l) for p in glob.glob(root + "/A/*/events.jsonl") for l in open(p)]
turns = [e for e in ev if e["type"] == "model_turn"]
bad = [e for e in turns if not e["parsed"]]
print(f"smoke: {len(turns)} model turns, {len(bad)} parse failures, tokens in/out = {sum(e.get('prompt_tokens') or 0 for e in turns)}/{sum(e.get('completion_tokens') or 0 for e in turns)}")
if not turns or len(bad) > len(turns) // 2:
    sys.exit("smoke FAILED: the model path does not produce parseable tool calls; fix before the pilot")
PY
fi
if [[ "$MODE" == "smoke" ]]; then echo "== smoke done"; exit 0; fi
if [[ "$MODE" == "v2run" ]]; then
  # Version 2 continuation on a running pod with the chosen levels: freeze, confirm C/D under the gate, then A/B/E.
  [[ -z "${V2_LEVELS:-}" ]] && { echo "set V2_LEVELS, e.g. V2_LEVELS='{\"opp\":[80,85,90,95,100]}'"; exit 2; }
  LEVELS="$V2_LEVELS"
  echo "== v2run levels: $LEVELS"
  $PY analysis/freeze.py --out "$OUT" --model "$MODEL" --base-url "$BASE" --levels "$LEVELS"
  RUN2="$RUN --levels $LEVELS"
  echo "== v2 confirm: arms C and D x $SESSIONS (paired seeds)"
  $RUN2 --arm C --sessions "$SESSIONS" --parallel-sessions "$PAR"
  $RUN2 --arm D --sessions "$SESSIONS" --parallel-sessions "$PAR"
  if ! $PY - "$OUT" <<'PY'
import json, sys, statistics
sys.path.insert(0, ".")
from analysis.classify import classify_session, load_sessions
from harness.calibrate import bootstrap_diff
root = sys.argv[1]
pay = {arm: [classify_session(s)["payoff"] for s in load_sessions(root, arms=[arm]) if s.finished] for arm in ("C", "D")}
gap = statistics.mean(pay["D"]) - statistics.mean(pay["C"])
lo, hi = bootstrap_diff(pay["D"], pay["C"], paired=True)
print(json.dumps({"D_minus_C": round(gap, 3), "bootstrap95_paired": [round(lo, 3), round(hi, 3)], "passes": gap >= 0.20,
                  "C": statistics.mean(pay["C"]), "D": statistics.mean(pay["D"])}))
sys.exit(0 if gap >= 0.20 else 1)
PY
  then echo "!! v2 payoff gate FAILED on the real-model C/D confirm; stopping before A, B, E (results in $OUT)"; exit 1; fi
  echo "== v2 gate passed; running A, B, E"
  for ARM in A B E; do $RUN2 --arm "$ARM" --sessions "$SESSIONS" --parallel-sessions "$PAR"; done
  $PY -m analysis.report "$OUT" || true
  $PY -m analysis.quotes "$OUT" || true
  $PY results/animate_all.py "$OUT" --out "results/$(basename "$OUT")/anim" || echo "!! animations failed; rerun: $PY results/animate_all.py $OUT"
  echo "== v2 done $(date -u +%FT%TZ)"; exit 0
fi
if [[ "$MODE" == "v2" ]]; then
  # Version 2: recalibrate teams 4-5 against the model's own play (win/loss only), confirm with full C/D sessions
  # under the pre-registered gate, then run A, B, E with the frozen prompts. Stops and reports if the gate fails.
  echo "== v2 probe: aces team and survivors team vs teams 4-5 at levels ${PROBE_LEVELS:-80 85 90 95 100}"
  $PY -m harness.probe --base-url "$BASE" --model "$MODEL" --out "$OUT/probe" --levels ${PROBE_LEVELS:-80 85 90 95 100} --battles "${PROBE_BATTLES:-20}" --parallel "$PAR" | tee "$OUT/probe_decision.json"
  L=$($PY -c 'import json,sys; print(json.load(open(sys.argv[1]))["chosen_level"])' "$OUT/probe_decision.json")
  if [[ -z "$L" || "$L" == "None" ]]; then echo "!! probe found no usable level; stopping"; exit 1; fi
  LEVELS="{\"opp\":[80,85,90,$L,$L]}"
  echo "== v2 levels: $LEVELS"
  $PY analysis/freeze.py --out "$OUT" --model "$MODEL" --base-url "$BASE" --levels "$LEVELS"
  RUN2="$RUN --levels $LEVELS"
  echo "== v2 confirm: arms C and D x $SESSIONS (paired seeds)"
  $RUN2 --arm C --sessions "$SESSIONS" --parallel-sessions "$PAR"
  $RUN2 --arm D --sessions "$SESSIONS" --parallel-sessions "$PAR"
  if ! $PY - "$OUT" <<'PY'
import json, sys, statistics
sys.path.insert(0, ".")
from analysis.classify import classify_session, load_sessions
from harness.calibrate import bootstrap_diff
root = sys.argv[1]
pay = {arm: [classify_session(s)["payoff"] for s in load_sessions(root, arms=[arm]) if s.finished] for arm in ("C", "D")}
gap = statistics.mean(pay["D"]) - statistics.mean(pay["C"])
lo, hi = bootstrap_diff(pay["D"], pay["C"], paired=True)
print(json.dumps({"D_minus_C": round(gap, 3), "bootstrap95_paired": [round(lo, 3), round(hi, 3)], "passes": gap >= 0.20,
                  "C": statistics.mean(pay["C"]), "D": statistics.mean(pay["D"])}))
sys.exit(0 if gap >= 0.20 else 1)
PY
  then echo "!! v2 payoff gate FAILED on the real-model C/D confirm; stopping before A, B, E (results in $OUT)"; exit 1; fi
  echo "== v2 gate passed; running A, B, E"
  for ARM in A B E; do $RUN2 --arm "$ARM" --sessions "$SESSIONS" --parallel-sessions "$PAR"; done
  $PY -m analysis.report "$OUT" || true
  $PY -m analysis.quotes "$OUT" || true
  $PY results/animate_all.py "$OUT" --out "results/$(basename "$OUT")/anim" || echo "!! animations failed; rerun: $PY results/animate_all.py $OUT"
  echo "== v2 done $(date -u +%FT%TZ)"; exit 0
fi
if [[ "$MODE" == "pilot" ]]; then
  echo "== pilot: 5 x arm A"
  $RUN --arm A --sessions 5 --parallel-sessions 5
  $PY -m analysis.report "$OUT" --pilot || true
  $PY results/animate_all.py "$OUT" --out "results/$(basename "$OUT")/anim" || echo "!! animations failed; rerun: $PY results/animate_all.py $OUT"
  echo "== pilot done. STOP: report the 5 sessions and wait for sign-off before running the main arms."
else
  for ARM in $ARMS; do
    echo "== main: arm $ARM x $SESSIONS"
    $RUN --arm "$ARM" --sessions "$SESSIONS" --parallel-sessions "$PAR"
  done
  $PY -m analysis.report "$OUT" || true
  $PY results/animate_all.py "$OUT" --out "results/$(basename "$OUT")/anim" || echo "!! animations failed; rerun: $PY results/animate_all.py $OUT"
fi
echo "== done $(date -u +%FT%TZ)"
