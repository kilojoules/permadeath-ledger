"""RunPod pod manager for the vLLM subject server, following the lab's RunPod conventions.

  python runpod/pod.py stock                         # GPU stock + prices (read-only; check FIRST)
  python runpod/pod.py plan   [--model ...]          # prints exactly what `create` would do; no spend
  python runpod/pod.py create [--model ...] --yes    # creates the pod (refuses without --yes / an interactive yes)
  python runpod/pod.py wait <pod_id>                 # prints the base URL on stdout when /v1/models answers
  python runpod/pod.py status <pod_id>               # desired status, uptime, GPU utilization % (always reported)
  python runpod/pod.py list                          # my pods (name prefix) and everyone else's names, never touched
  python runpod/pod.py terminate <pod_id>            # verified destroy: retry x5, confirm gone via the API listing

Conventions: the key file may belong to a shared account, so every pod is named
`julian-permadeath-<job>` with env OWNER=julian PROJECT=permadeath; only pods with that prefix are ever
terminated; the GraphQL API sits behind Cloudflare and rejects Python's default user agent, so every call sends a
browser UA; an idle box is destroyed the instant its job is done (the launch script prints the command on every exit
path and never auto-destroys). The key is loaded into memory and never printed, logged or written anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import requests

GRAPHQL = "https://api.runpod.io/graphql"
REST = "https://rest.runpod.io/v1"
LOG_DIR = "/root/.cache/huggingface/permadeath"   # on the persistent volume; served on port 8080 by python -m http.server
UA = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
# v0.31.0-cu129 is broken at import (its torchcodec links libnvrtc.so.13; seen 2026-10-07 on an H100 host, driver 580):
# use the CUDA 13 default image on a CUDA 13.0 host.
VLLM_IMAGE = os.environ.get("VLLM_IMAGE", "vllm/vllm-openai:v0.31.0")
KEY_FILES = [os.path.expanduser("~/.super_lab_run.pod"), os.path.expanduser("~/.run.pod")]
OWNER = os.environ.get("RUNPOD_OWNER", "julian")
PROJECT = "permadeath"
NAME_PREFIX = f"{OWNER}-{PROJECT}-"


def api_key() -> str:
    k = os.environ.get("RUNPOD_API_KEY", "").strip()
    if k:
        return k
    for p in KEY_FILES:
        if os.path.exists(p):
            m = re.search(r"rpa_[A-Za-z0-9]+", open(p).read())
            if m:
                return m.group(0)
    sys.exit("no RunPod API key: set RUNPOD_API_KEY or put the key in ~/.super_lab_run.pod")


def gql_raw(query: str, variables: dict | None = None) -> tuple[dict | None, list]:
    r = requests.post(GRAPHQL, params={"api_key": api_key()}, headers=UA,
                      json={"query": query, "variables": variables or {}}, timeout=60)
    if r.status_code != 200:
        return None, [{"message": f"HTTP {r.status_code}: {r.text[:300]}"}]
    data = r.json()
    return data.get("data"), data.get("errors") or []


def gql(query: str, variables: dict | None = None) -> dict:
    data, errors = gql_raw(query, variables)
    if errors:
        sys.exit(f"GraphQL error: {json.dumps(errors)[:600]}")
    return data


def reasoning_parser(model: str) -> str | None:
    m = model.lower()
    if "gpt-oss" in m:
        return "openai_gptoss"
    if "deepseek-r1" in m or "deepseek_r1" in m:
        return "deepseek_r1"
    if "qwen3" in m:
        return "qwen3"
    return None


def docker_args(model: str, served_name: str | None, max_len: int, max_num_seqs: int, batched_tokens: int | None,
                quantization: str | None, gpu_mem: float, minimal: bool = False) -> str:
    """minimal=True keeps only what the experiment needs (model, port, context length, reasoning parser) and leaves
    every performance knob at the vLLM default; use it when a pod loops at startup and no logs are reachable."""
    # The official image's ENTRYPOINT is `vllm serve` (docker/Dockerfile, v0.31.0), which takes the model as a
    # positional argument; the old `--model` option form belongs to the python -m ...api_server entrypoint.
    args = [model, "--served-model-name", served_name or model, "--host", "0.0.0.0", "--port", "8000",
            "--max-model-len", str(max_len)]
    if not minimal:
        args += ["--enable-prefix-caching", "--gpu-memory-utilization", str(gpu_mem), "--max-num-seqs", str(max_num_seqs), "--dtype", "auto"]
        if batched_tokens:
            args += ["--max-num-batched-tokens", str(batched_tokens)]
    if quantization:
        args += ["--quantization", quantization]
    rp = reasoning_parser(model)
    if rp:
        args += ["--reasoning-parser", rp]
    return " ".join(args)


def hf_token() -> str | None:
    for p in (os.path.expanduser("~/.hf_token"), os.path.expanduser("~/.cache/huggingface/token")):
        if os.path.exists(p):
            t = open(p).read().strip()
            if t:
                return t
    return os.environ.get("HF_TOKEN") or None


def plan_dict(a) -> dict:
    env = {"OWNER": OWNER, "PROJECT": PROJECT, "VLLM_LOGGING_LEVEL": "INFO", "VLLM_SYSTEM_START_DATE": a.start_date}
    if hf_token():
        env["HF_TOKEN"] = "<from ~/.hf_token or ~/.cache/huggingface/token>"
    return {"name": NAME_PREFIX + a.job, "image": VLLM_IMAGE, "gpu_type_id": a.gpu, "cloud_type": a.cloud, "gpu_count": 1,
            "volume_in_gb": a.volume_gb, "container_disk_in_gb": a.disk_gb, "volume_mount_path": "/root/.cache/huggingface",
            "ports": "8000/http,8080/http,22/tcp", "allowed_cuda_versions": a.cuda, "min_vcpu": 8, "min_memory_gb": 60,
            "docker_args": docker_args(a.model, a.served_name, a.max_len, a.max_num_seqs, a.batched_tokens, a.quantization, a.gpu_mem, a.minimal),
            "env": env, "base_url_after_start": "https://<pod_id>-8000.proxy.runpod.net",
            "note": "billing is per second while the pod runs; destroy the instant the job is done: python runpod/pod.py terminate <pod_id>"}


def stock(a):
    q = """query { gpuTypes { id displayName memoryInGb secureCloud communityCloud
             lowestPrice(input:{gpuCount:1}) { uninterruptablePrice stockStatus } } }"""
    rows = gql(q)["gpuTypes"]
    rows = [g for g in rows if (g.get("memoryInGb") or 0) >= 48]
    rows.sort(key=lambda g: -(g.get("memoryInGb") or 0))
    print(f"{'display':34s} {'gpu_type_id':44s} {'GB':>4s} {'stock':8s} {'$/h':>6s}")
    for g in rows:
        lp = g.get("lowestPrice") or {}
        print(f"{g['displayName']:34s} {g['id']:44s} {g['memoryInGb']:>4d} {str(lp.get('stockStatus')):8s} {str(lp.get('uninterruptablePrice')):>6s}")


def plan(a):
    print(json.dumps(plan_dict(a), indent=1))


FALLBACK_GPUS = ["NVIDIA H100 80GB HBM3", "NVIDIA H200"]


def rest(method: str, path: str, payload: dict | None = None) -> tuple[int, dict | list | str]:
    headers = dict(UA, Authorization=f"Bearer {api_key()}")
    r = requests.request(method, REST + path, headers=headers, json=payload, timeout=120)
    try:
        body = r.json()
    except ValueError:
        body = r.text[:500]
    return r.status_code, body


def start_script(docker_args_text: str) -> str:
    """bash -lc script: file server on 8080 over the log dir (persistent volume), vllm serve teed to vllm.log,
    and sleep forever if vllm exits so the log stays readable instead of a restart loop."""
    return (f"set -o pipefail; mkdir -p {LOG_DIR} && cd {LOG_DIR} && "
            f"(nohup python3 -m http.server 8080 --directory {LOG_DIR} >/dev/null 2>&1 &) && "
            f"echo \"boot $(date -u +%FT%TZ)\" >> boot.log && (nvidia-smi >> boot.log 2>&1 || true) && "
            f"(python3 -c 'import vllm,sys; print(\"vllm\", vllm.__version__, sys.version)' >> boot.log 2>&1 || true) && "
            f"echo \"args: {docker_args_text}\" >> boot.log && "
            f"(vllm serve {docker_args_text} 2>&1 | tee -a vllm.log); "
            f"echo \"vllm exited rc=$? $(date -u +%FT%TZ)\" >> boot.log; sleep infinity")


def create(a):
    p = plan_dict(a)
    print(json.dumps(p, indent=1), file=sys.stderr)
    if not a.yes:
        ans = input("Create this pod now? Type yes to proceed: ") if sys.stdin.isatty() else "no"
        if ans.strip().lower() != "yes":
            sys.exit("not created (pass --yes to skip the prompt)")
    env = {k: v for k, v in p["env"].items() if k != "HF_TOKEN"}
    if hf_token():
        env["HF_TOKEN"] = hf_token()
    gpus = [p["gpu_type_id"]] + ([] if a.no_fallback else [g for g in FALLBACK_GPUS if g != p["gpu_type_id"]])
    body = {"name": p["name"], "imageName": p["image"], "cloudType": p["cloud_type"], "computeType": "GPU", "gpuCount": 1,
            "gpuTypeIds": gpus, "gpuTypePriority": "custom", "containerDiskInGb": p["container_disk_in_gb"],
            "volumeInGb": p["volume_in_gb"], "volumeMountPath": p["volume_mount_path"], "ports": p["ports"].split(","),
            "env": env, "allowedCudaVersions": p["allowed_cuda_versions"], "supportPublicIp": True,
            "minRAMPerGPU": p["min_memory_gb"], "minVCPUPerGPU": p["min_vcpu"],
            "dockerEntrypoint": ["/bin/bash", "-lc"], "dockerStartCmd": [start_script(p["docker_args"])]}
    code, resp = rest("POST", "/pods", body)
    if code != 201 and code != 200:
        sys.stderr.write(f"REST create failed ({code}): {json.dumps(resp)[:600]}\n")
        if p["cloud_type"] == "SECURE" and not a.no_fallback:
            body["cloudType"] = "COMMUNITY"
            code, resp = rest("POST", "/pods", body)
            if code not in (200, 201):
                sys.exit(f"create failed on COMMUNITY too ({code}): {json.dumps(resp)[:600]}")
        else:
            sys.exit("create failed")
    pod = resp if isinstance(resp, dict) else {}
    out = {"pod_id": pod.get("id"), "name": pod.get("name"), "gpu": (pod.get("machine") or {}).get("gpuDisplayName") or pod.get("gpuTypeId"),
           "cost_per_hr": pod.get("costPerHr"), **{k: v for k, v in p.items() if k not in ("env",)}}
    print(json.dumps(out))
    return pod.get("id")


def log_url(pod_id: str, name: str = "vllm.log") -> str:
    return f"https://{pod_id}-8080.proxy.runpod.net/{name}"


def fetch_log(pod_id: str, name: str = "vllm.log", tail_bytes: int = 4000) -> str | None:
    try:
        r = requests.get(log_url(pod_id, name), headers={"Range": f"bytes=-{tail_bytes}"}, timeout=15)
        if r.status_code in (200, 206):
            return r.text
    except Exception:  # noqa: BLE001
        return None
    return None


def logs(a):
    for name in ("boot.log", "vllm.log"):
        text = fetch_log(a.pod_id, name, a.tail)
        print(f"===== {name} ({'unreachable' if text is None else str(len(text)) + ' bytes'}) =====")
        if text:
            print(text[-a.tail:])


def base_url(pod_id: str) -> str:
    return f"https://{pod_id}-8000.proxy.runpod.net"


def pod_info(pod_id: str) -> dict:
    q = """query($id: String!) { pod(input:{podId:$id}) { id name desiredStatus costPerHr
             runtime { uptimeInSeconds gpus { id gpuUtilPercent memoryUtilPercent } ports { ip isIpPublic publicPort privatePort type } } } }"""
    return gql(q, {"id": pod_id})["pod"] or {}


def gpu_line(info: dict) -> str:
    rt = info.get("runtime") or {}
    gpus = rt.get("gpus") or []
    if not gpus:
        return "GPU: no runtime yet (starting or dead host)"
    return "GPU: " + ", ".join(f"util {g.get('gpuUtilPercent')}% mem {g.get('memoryUtilPercent')}%" for g in gpus) + f"; uptime {rt.get('uptimeInSeconds')}s"


def wait(a):
    url = base_url(a.pod_id)
    t0 = time.time()
    last = ""
    no_runtime_since = t0
    while time.time() - t0 < a.timeout:
        info = pod_info(a.pod_id)
        st = info.get("desiredStatus")
        if info.get("runtime"):
            no_runtime_since = None
        elif no_runtime_since and time.time() - no_runtime_since > 240:
            sys.stderr.write("\nno runtime after 4 min: dead host per the skill; terminate and re-provision on another GPU/cloud\n")
        try:
            r = requests.get(url + "/v1/models", timeout=10)
            if r.status_code == 200 and "data" in r.json():
                sys.stderr.write(f"\nready after {time.time() - t0:.0f}s: {r.json()['data'][0]['id']} | {gpu_line(info)}\n")
                print(url)
                return
            last = f"http {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = type(e).__name__
        tail = fetch_log(a.pod_id, "vllm.log", 1500) or ""
        progress = [l for l in tail.splitlines() if l.strip()][-1:] if tail else []
        if "exited" in (fetch_log(a.pod_id, "boot.log", 300) or ""):
            sys.exit(f"\nvLLM exited inside the pod; read the log: python runpod/pod.py logs {a.pod_id}  (then terminate it)")
        sys.stderr.write(f"\nwaiting ({time.time() - t0:.0f}s) pod={st} endpoint={last} {gpu_line(info)} | {progress[0][:160] if progress else 'no log yet'}")
        time.sleep(15)
    sys.exit(f"\ntimeout waiting for {url}; the pod is still running: python runpod/pod.py terminate {a.pod_id}")


def status(a):
    info = pod_info(a.pod_id)
    print(json.dumps({"id": info.get("id"), "name": info.get("name"), "desiredStatus": info.get("desiredStatus"),
                      "costPerHr": info.get("costPerHr"), "gpu": gpu_line(info), "base_url": base_url(a.pod_id)}, indent=1))


def my_pods() -> tuple[list[dict], list[dict]]:
    pods = gql("query { myself { pods { id name desiredStatus costPerHr runtime { uptimeInSeconds gpus { gpuUtilPercent } } } } }")["myself"]["pods"]
    mine = [p for p in pods if (p.get("name") or "").startswith(NAME_PREFIX)]
    others = [p for p in pods if p not in mine]
    return mine, others


def list_pods(a):
    mine, others = my_pods()
    print("MINE (prefix %s):" % NAME_PREFIX)
    for p in mine:
        print(" ", p["id"], p["name"], p.get("desiredStatus"), f"${p.get('costPerHr')}/h", gpu_line(p))
    if not mine:
        print("  none")
    print("OTHERS on this account (never touched):", [p.get("name") for p in others] or "none")


def terminate(a):
    info = pod_info(a.pod_id)
    name = info.get("name") or ""
    if not name.startswith(NAME_PREFIX) and not a.force_name:
        sys.exit(f"refusing to terminate {a.pod_id} ({name!r}): not a {NAME_PREFIX}* pod (shared account; pass --force-name only if you own it)")
    for attempt in range(1, 6):
        try:
            gql("mutation($id: String!) { podTerminate(input:{podId:$id}) }", {"id": a.pod_id})
        except SystemExit as e:
            sys.stderr.write(f"attempt {attempt}: {e}\n")
        time.sleep(3)
        mine, _ = my_pods()
        if all(p["id"] != a.pod_id for p in mine):
            print(json.dumps({"terminated": a.pod_id, "name": name, "verified_gone": True, "attempt": attempt}))
            list_pods(a)
            return
        sys.stderr.write(f"attempt {attempt}: STILL listed\n")
    sys.exit(f"pod {a.pod_id} still listed after 5 attempts; check the console")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stock").set_defaults(fn=stock)
    sub.add_parser("list").set_defaults(fn=list_pods)
    for name, fn in (("plan", plan), ("create", create)):
        c = sub.add_parser(name)
        c.add_argument("--model", default="openai/gpt-oss-120b")
        c.add_argument("--served-name", default=None)
        c.add_argument("--job", default="vllm", help="pod name suffix: julian-permadeath-<job>")
        c.add_argument("--gpu", default="NVIDIA H100 80GB HBM3")
        c.add_argument("--cloud", default="SECURE", choices=["COMMUNITY", "SECURE"])
        c.add_argument("--max-len", type=int, default=32768)
        c.add_argument("--max-num-seqs", type=int, default=64)
        c.add_argument("--batched-tokens", type=int, default=4096)
        c.add_argument("--gpu-mem", type=float, default=0.92)
        c.add_argument("--quantization", default=None)
        c.add_argument("--volume-gb", type=int, default=120)
        c.add_argument("--disk-gb", type=int, default=80)
        c.add_argument("--cuda", nargs="*", default=["13.0"])
        c.add_argument("--start-date", default="2026-10-07", help="VLLM_SYSTEM_START_DATE pin (gpt-oss harmony system message)")
        c.add_argument("--yes", action="store_true")
        c.add_argument("--no-fallback", action="store_true", help="do not try other GPU/cloud combinations when the first has no stock")
        c.add_argument("--minimal", action="store_true", help="minimal vLLM args (model, port, max-model-len, reasoning parser only)")
        c.set_defaults(fn=fn)
    for name, fn in (("wait", wait), ("status", status), ("terminate", terminate), ("logs", logs)):
        c = sub.add_parser(name); c.add_argument("pod_id"); c.set_defaults(fn=fn)
        if name == "logs":
            c.add_argument("--tail", type=int, default=4000)
        if name == "wait":
            c.add_argument("--timeout", type=int, default=2400)
        if name == "terminate":
            c.add_argument("--force-name", action="store_true")
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
