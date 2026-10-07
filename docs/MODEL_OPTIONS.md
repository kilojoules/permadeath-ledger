# Model / deployment options for the vLLM-on-RunPod eval harness

All facts below were checked on **2026-10-07** unless a different "checked" date is noted. Nothing was deployed and no money was spent; everything comes from public docs, release pages, model cards, GitHub issues/source, and RunPod's public pricing endpoints.

Workload assumed: ~8,000 chat completions, ~2k prompt tokens, ~300 visible output tokens plus hidden reasoning, 20 sessions in flight, `response_format={"type":"json_schema",...}`, per-request `seed`, a `--reasoning-parser`.

---

## 0a. Correction from the first launch (2026-10-07, after this note was written)

`vllm/vllm-openai:v0.31.0-cu129` crashes at import on an H100 host with driver 580.126.09 (CUDA 13.0): its torchcodec is linked
against `libnvrtc.so.13` (`OSError: libnvrtc.so.13: cannot open shared object file`), so `vllm serve` exits before parsing
arguments. The CUDA 13 default image `vllm/vllm-openai:v0.31.0` with `allowed_cuda_versions=["13.0"]` starts correctly on the
same host class. Two pods were lost to this before the log wrapper (runpod/pod.py `start_script`) made the container log readable.

## 0. Recommendation

### Default: `openai/gpt-oss-120b` on 1x H100 SXM, vLLM `v0.31.0`

| Item | Value |
|---|---|
| Image | `vllm/vllm-openai:v0.31.0-cu129` (CUDA 12.9; pass `allowed_cuda_versions=["12.9","13.0"]`) — or `vllm/vllm-openai:v0.31.0` (CUDA 13.0 default; requires host driver >= 580, so pass `allowed_cuda_versions=["13.0"]`) |
| GPU | `gpu_type_id="NVIDIA H100 80GB HBM3"` (H100 SXM 80GB). `cloud_type="SECURE"` $3.49/h, `"COMMUNITY"` $2.69/h |
| docker_args | `--model openai/gpt-oss-120b --served-model-name openai/gpt-oss-120b --host 0.0.0.0 --port 8000 --max-model-len 32768 --enable-prefix-caching --gpu-memory-utilization 0.92 --max-num-seqs 64 --max-num-batched-tokens 4096 --reasoning-parser openai_gptoss --dtype auto` |
| Per request | `reasoning_effort="low"|"medium"|"high"` (top-level field; harmony rejects any other value), `temperature=1.0, top_p=1.0`, `seed=<int>`, `max_tokens` >= 4096, **do not** send `include_reasoning=false`, **do not** send `stop` |
| Read | `choices[0].message.content` (JSON) and `choices[0].message.reasoning` (the field is `reasoning`, not `reasoning_content`) |
| env | optional `VLLM_SYSTEM_START_DATE=2026-10-07` to pin the date vLLM injects into the harmony system message (otherwise the prompt changes day to day) |
| Why | Fits one 80GB GPU (MXFP4 MoE, 5.1B active), ~3-4k output tok/s on one H100 at ~20 users, native `reasoning_effort`, and vLLM wraps `json_schema` in a harmony-aware grammar so the schema constrains only the `final` channel. Estimated run: ~0.5-2 h, ~$2-8 depending on effort (section 4). |

The gpt-oss recipe warns that on H100 with TP=1 the *defaults* for `--gpu-memory-utilization` and `--max-num-batched-tokens` can OOM; it suggests raising utilization or lowering batched tokens (e.g. `--gpu-memory-utilization 0.95 --max-num-batched-tokens 1024`). The args above lower batched tokens to 4096; if the pod OOMs at start, drop to 2048 or 1024.

### Alternative: `Qwen/Qwen3-32B-FP8` on 1x H100 SXM, vLLM `v0.31.0`

| Item | Value |
|---|---|
| docker_args | `--model Qwen/Qwen3-32B-FP8 --served-model-name Qwen/Qwen3-32B --host 0.0.0.0 --port 8000 --max-model-len 32768 --enable-prefix-caching --gpu-memory-utilization 0.92 --max-num-seqs 64 --reasoning-parser qwen3 --dtype auto` |
| Per request | thinking on (default): `temperature=0.6, top_p=0.95, top_k=20, min_p=0`; thinking off: `chat_template_kwargs={"enable_thinking": false}` and `temperature=0.7, top_p=0.8, top_k=20, min_p=0`; optional `presence_penalty` 0-2; never greedy; `max_tokens` >= 4096 with thinking on |
| Why FP8 | Official FP8 checkpoint is 32.8 GB vs 65.5 GB BF16, leaving ~35 GB of KV cache for 20 concurrent 32k-context sessions; BF16 leaves only ~5 GB |
| Cost/time | dense 32B is ~4-5x slower per token than gpt-oss-120b: ~2.5-10 h, ~$7-35 (section 4) |

A 2026-vintage drop-in for the alternative is `Qwen/Qwen3.6-27B-FP8` (27.8 GB, April 2026, same `qwen3` parser, vLLM >= 0.19) or the much faster MoE `Qwen/Qwen3.6-35B-A3B-FP8`; see section 1.5 for their sampling settings. They are newer and less battle-tested with vLLM's parser edge cases (two of the open parser bugs below were reported against Qwen3.6).

---

## 1. vLLM image tag and structured outputs + reasoning parsers

### 1.1 Current stable image tag

- Latest stable release is **v0.31.0**, published 2026-10-05 (GitHub API: `published_at=2026-10-05T06:44:55Z`, `prerelease=false`). Previous: v0.30.0 (2026-09-22), v0.29.0 (2026-09-09), v0.28.0 (2026-08-26), v0.27.1 (2026-08-11); cadence is roughly two weeks. Source: https://github.com/vllm-project/vllm/releases (checked 2026-10-07 via `gh api`).
- Docker Hub tags pushed 2026-10-04: `vllm/vllm-openai:v0.31.0`, `v0.31.0-cu129`, `v0.31.0-ubuntu2404`, `v0.31.0-cu129-ubuntu2404`, `v0.31.0-x86_64`, `v0.31.0-aarch64`, `v0.31.0-x86_64-zstd`. Source: https://hub.docker.com/v2/repositories/vllm/vllm-openai/tags?name=v0.31 (checked 2026-10-07).
- The v0.31.0 release notes state: "CUDA 13.0 (Default) `docker pull vllm/vllm-openai:v0.31.0`; CUDA 12.9 `docker pull vllm/vllm-openai:v0.31.0-cu129`". Source: https://github.com/vllm-project/vllm/releases/tag/v0.31.0 (checked 2026-10-07).
- Driver requirements (NVIDIA CUDA release notes, checked 2026-10-07): CUDA 13.0 needs Linux driver >= 580; CUDA 12.9 Update 1 needs >= 575.57.08; CUDA 12.8 Update 1 needs >= 570.124.06. Source: https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html
- RunPod lets you filter hosts with `allowed_cuda_versions` (SDK) / `allowedCudaVersions` (REST); enum: `13.0, 12.9, 12.8, 12.7, 12.6, 12.5, 12.4, 12.3, 12.2, 12.1, 12.0, 11.8`. Source: https://docs.runpod.io/api-reference/pods/POST/pods.md (checked 2026-10-07).
- v0.31.0 breaking changes worth knowing: `--enable-mamba-fine-grained-prefix-cache` renamed; `tokenizer_mode="slow"` removed; `--enforce-eager` now disables JIT warmup; per-request multimodal kwargs gated behind `--trust-request-mm-kwargs`; `transformers` has an upper bound. Source: v0.31.0 release notes.

### 1.2 Structured outputs in v0.31.0 (general)

- `response_format={"type":"json_schema","json_schema":{"name":..., "schema":...}}` is the supported way to request JSON schema on `/v1/chat/completions`. Backends: `xgrammar`, `guidance`, `outlines`, `lm-format-enforcer`; default `"auto"`. Backend is selected with `--structured-outputs-config.backend`; the old `--guided-decoding-backend` flag is gone. Source: https://docs.vllm.ai/en/stable/features/structured_outputs.html and raw https://raw.githubusercontent.com/vllm-project/vllm/main/docs/features/structured_outputs.md (checked 2026-10-07).
- `StructuredOutputsConfig` fields and defaults in v0.31.0 (`vllm/config/structured_outputs.py`): `backend="auto"`, `disable_fallback=False`, `disable_any_whitespace=False` (xgrammar only), `disable_additional_properties=False` (guidance only), `reasoning_parser=""`, `reasoning_parser_plugin=""`, `enable_in_reasoning=False`. (checked 2026-10-07 via `gh api`, tag v0.31.0)
- How reasoning and the grammar interact (v0.31.0 `vllm/v1/structured_output/__init__.py`): unless `enable_in_reasoning=True`, the grammar bitmask is **not applied until the reasoning parser reports the end of reasoning** (`is_reasoning_end_streaming` / `find_reasoning_end_offset`); once it ends, `reasoning_ended` is latched and the schema constrains everything after it. So the schema does not constrain the thinking text, by design. The docs table lists `json, regex` structured-output support for every current reasoning parser (Qwen3, Gemma 4, GLM-4.5, DeepSeek R1, etc.). Source: https://raw.githubusercontent.com/vllm-project/vllm/main/docs/features/reasoning_outputs.md (checked 2026-10-07).
- Reasoning parsers registered in v0.31.0 (`vllm/reasoning/__init__.py`): `deepseek_r1, deepseek_v3, deepseek_v4, deepseek_v41, poolside_v1, cohere_command3, cohere_command4, ernie45, gemma4, glm45, glm47, ling3, openai_gptoss, granite, holo2, hunyuan_a13b, hy_v3, hy_v4, kimi_k2, kimi_k3, k2_horizon, mimo, minimax_m2, minimax_m2_append_think, minimax_m3, mistral, nemotron_v3, olmo3, muse_glimmer, qwen3, seed_oss, step3, step3p5, inkling`. (checked 2026-10-07 via `gh api`, tag v0.31.0)
- Request fields in v0.31.0 `ChatCompletionRequest` (`vllm/entrypoints/openai/chat_completion/protocol.py`): `reasoning_effort: Literal["none","minimal","low","medium","high","xhigh","max"] | None`, `include_reasoning: bool = True`, `chat_template_kwargs: dict | None`, `seed: int | None` (int64 range), `response_format: AnyResponseFormat | None`, `structured_outputs: StructuredOutputsParams | None`. Setting `reasoning_effort` auto-injects `enable_thinking=True` into `chat_template_kwargs` for templates that need explicit opt-in (e.g. Gemma 4) unless you set `enable_thinking` yourself. (checked 2026-10-07)
- **Response field name:** the reasoning text is `choices[].message.reasoning`. `reasoning_content` was deprecated/removed in stages: v0.16.0 removed the deprecated message field (#33402) and added back-compat on *input* messages (#33635); v0.19.0 "reasoning_content message field removed" (#37480); v0.22.0 "normalize reasoning_content -> reasoning" (#42664); v0.28.0 documents "reasoning_content output removal" as a breaking client change (#50624). The v0.31.0 `ChatMessage` class has only `reasoning: str | None`. A harness that reads `reasoning_content` will silently get nothing. Sources: release notes for those tags; docs: "`reasoning` used to be called `reasoning_content`. To migrate, directly replace `reasoning_content` with `reasoning`." (checked 2026-10-07)

### 1.3 `openai/gpt-oss-120b` and `gpt-oss-20b`

- Parser name: `openai_gptoss` (registered in v0.31.0). The chat server auto-enables the harmony path when `hf_config.model_type == "gpt_oss"` (`is_harmony=...` in `chat_completion/serving.py`), so harmony parsing happens with or without the flag; `--reasoning-parser openai_gptoss` is what the prior study used and is still valid.
- **Structured outputs with harmony work in chat completions** and are implemented as a reasoning-aware grammar: `vllm/parser/harmony.py::_adjust_output_format` converts a `json_schema` request into an xgrammar `StructuralTag` that allows the `analysis` channel freely and constrains the `final` channel's message to the schema (`_assemble_tag(allow_analysis=True, allow_commentary=False, content=<schema>)`), then clears `response_format`. The gpt-oss recipe's support matrix marks Structured Output as supported for Chat Completion and Chat Completion (Streaming) (function calling: `tool_choice="auto"` only). Sources: v0.31.0 source via `gh api`; https://raw.githubusercontent.com/vllm-project/recipes/main/OpenAI/GPT-OSS.md (checked 2026-10-07).
- **reasoning_effort per request:** send the top-level `reasoning_effort` field. vLLM builds the harmony system message itself (`get_system_message(reasoning_effort=...)` in `vllm/entrypoints/openai/parser/harmony_utils.py`) and only accepts `"low" | "medium" | "high"` (`REASONING_EFFORT` map); other values from the generic enum (`none`, `minimal`, `xhigh`, `max`) raise `VLLMValidationError`. If omitted, the harmony default applies (medium). The model card's alternative is `Reasoning: high` in the system prompt, but with vLLM use the API field. Sources: v0.31.0 source; https://huggingface.co/openai/gpt-oss-120b (checked 2026-10-07).
- System prompt handling: by default vLLM puts your system message into the harmony *developer* message; `VLLM_GPT_OSS_HARMONY_SYSTEM_INSTRUCTIONS=1` injects it into the harmony *system* message instead. `VLLM_SYSTEM_START_DATE` pins the "current date" vLLM writes into the harmony system message (otherwise it changes daily, which the env docstring notes "introduces non-determinism"). Source: `vllm/envs.py` at v0.31.0.
- Memory: the card says the 120b model "fits on a single 80GB GPU (like NVIDIA H100 or AMD MI300X)" thanks to MXFP4 MoE weights (116.8B params total, 5.1B active); the recipe says "gpt-oss-120b will fit on a single A100 (80GB)". KV cache is cheap (36 layers, 8 KV heads, head_dim 64, half the layers sliding-window). gpt-oss-20b fits in 16 GB. Sources: model card; recipe; https://huggingface.co/api/models/openai/gpt-oss-120b (checked 2026-10-07).
- Known issues (gpt-oss-specific), all checked 2026-10-07:
  - #32791 multi-turn + `json_object` returned `content: null` / invalid JSON / loops — **fixed** by PR #34454 (merged 2026-02-13, so in v0.16.0+). https://github.com/vllm-project/vllm/issues/32791
  - #37897 structured output + reasoning "grinds to a halt at long context" (parser rescanned the whole sequence every step) — **fixed** by PR #35745 (merged 2026-04-21, so in v0.20.0+). https://github.com/vllm-project/vllm/issues/37897
  - #37359 guidance backend + `openai_gptoss` never activates the grammar — **offline `LLM.generate()` only**; the online server path is unaffected; closed stale. Keep backend `auto` (xgrammar). https://github.com/vllm-project/vllm/issues/37359
  - #23837 (vLLM 0.10.1.1) intermittent HTTP 500 with empty body when a strict-JSON "router" system prompt was used; closed stale. Not reproduced on recent versions; using real `json_schema` instead of prompt-only JSON avoids the pattern. https://github.com/vllm-project/vllm/issues/23837
  - Recipe "Known Limitations": H100 TP=1 with default `--gpu-memory-utilization` and `--max-num-batched-tokens` can OOM (suggested `--gpu-memory-utilization 0.95 --max-num-batched-tokens 1024`); TP=2 on H100 must stay below 0.95; Responses API usage accounting returns zeros; streaming tool-call batching incomplete. https://raw.githubusercontent.com/vllm-project/recipes/main/OpenAI/GPT-OSS.md
  - Recipe Hopper tuning config (`GPT-OSS_Hopper.yaml`): `max-num-batched-tokens: 8192`, `max-cudagraph-capture-size: 2048`, `stream-interval: 20`, `no-enable-prefix-caching: true` (the last one is for benchmark consistency; prefix caching is beneficial for this harness's shared system prompt). Recipe says vLLM 0.12.0+ with `--config GPT-OSS_Hopper.yaml --tensor-parallel-size 1`.

### 1.4 `Qwen/Qwen3-32B` and `Qwen/Qwen3-30B-A3B`

- Parser: `qwen3` (docs table: "Qwen3 series | `qwen3` | json, regex | tool calling yes"). The model cards still print `vllm serve ... --enable-reasoning --reasoning-parser deepseek_r1`; that is stale — there is no `enable_reasoning` option anywhere in the v0.31.0 code base (only an issue-autolabel rule mentions it), while `deepseek_r1` is still registered and works for templates that pre-fill `<think>` (e.g. the Thinking-2507 variants). Sources: docs table; `gh search code` on vllm-project/vllm; https://huggingface.co/Qwen/Qwen3-32B (checked 2026-10-07).
- Thinking toggle: `extra_body={"chat_template_kwargs": {"enable_thinking": false}}`; default is thinking on. Sending `reasoning_effort` also injects `enable_thinking=True`.
- Memory: Qwen3-32B is 32.8B params, ~65.5 GB in BF16; the official `Qwen/Qwen3-32B-FP8` (created 2025-04-28) is 32.8 GB. Native context 32,768 (131,072 with YaRN). Qwen3-30B-A3B: 30.5B total / 3.3B active, 128 experts (8 active), same contexts. Sources: model cards; https://huggingface.co/api/models/Qwen/Qwen3-32B-FP8 (checked 2026-10-07).
- Newer same-size variants: `Qwen3-30B-A3B-Thinking-2507` (July 2025, thinking-only, `<think>` pre-filled by the template, card recommends `--reasoning-parser deepseek_r1`, recommended output length 32,768) and `Qwen3-30B-A3B-Instruct-2507` (non-thinking). https://huggingface.co/Qwen/Qwen3-30B-A3B-Thinking-2507 (checked 2026-10-07)
- Known issues (Qwen3 / `qwen3` parser), all checked 2026-10-07:
  - **#48863 (OPEN, filed 2026-07-16):** `include_reasoning=False` + `json_schema` returns `content: null` for every response, introduced in v0.23.1. Root cause: with `include_reasoning=False` the server sets `reasoning_ended=True` up front so the grammar forces JSON from token one, the model emits no `<think>` block, and the parser classifies the JSON as reasoning (then nulls it). Mitigation: never send `include_reasoning=false` together with `response_format`; keep the default and drop `reasoning` client-side. https://github.com/vllm-project/vllm/issues/48863
  - **#53284 (OPEN, 2026-08-21):** `--reasoning-parser qwen3` returns the whole answer as `reasoning` (content null) when the rendered prompt already closes the think block but `enable_thinking` was not passed via `chat_template_kwargs` (custom templates with other switches). Mitigation: disable thinking only via `chat_template_kwargs.enable_thinking=false`; do not use community templates with `reasoning_effort: "none"`/`<|think_off|>` switches. https://github.com/vllm-project/vllm/issues/53284
  - **#50948 (OPEN, 2026-08-04):** with `--structured-outputs-config.enable_in_reasoning=true` the Qwen parser routes the grammar-constrained JSON into `reasoning`. Mitigation: leave `enable_in_reasoning` at its default `False`. https://github.com/vllm-project/vllm/issues/50948
  - #35221 truncated (no `</think>`) output returned as `content` instead of `reasoning` — **fixed** by PR #35230 (merged 2026-02-26, v0.17.0+). https://github.com/vllm-project/vllm/issues/35221
  - #59515 (closed as duplicate, Sept 2026): `stop` strings are matched inside the reasoning text, ending the request with `content=null`, `finish_reason="stop"`. Mitigation: do not pass `stop`. https://github.com/vllm-project/vllm/issues/59515

### 1.5 `meta-llama/Llama-3.3-70B-Instruct`

- BF16 is ~141 GB, so one 80 GB GPU needs **FP8** (or INT4). The vLLM recipe targets Hopper with `nvidia/Llama-3.3-70B-Instruct-FP8` and `--tensor-parallel-size 1`, config `Llama3.3_Hopper.yaml` = `kv-cache-dtype: fp8`, `async-scheduling: true`, `max-num-batched-tokens: 8192` (plus `no-enable-prefix-caching` for benchmarking), driver >= 575. `RedHatAI/Llama-3.3-70B-Instruct-FP8-dynamic` is the other common FP8 checkpoint (~50% of BF16, 99.8-100.6% accuracy recovery on OpenLLM v1/v2). Sources: https://docs.vllm.ai/projects/recipes/en/latest/Llama/Llama3.3-70B.html and the recipe YAML; https://huggingface.co/RedHatAI/Llama-3.3-70B-Instruct-FP8-dynamic (checked 2026-10-07).
- Caveat for this harness: FP8 weights are ~70 GB, leaving only ~3-5 GB for KV cache at 0.92 utilization (80 layers x 8 KV heads x 128 dims ~ 0.33 MB/token in BF16 KV, ~0.16 MB with `--kv-cache-dtype fp8`), i.e. roughly 20-30k cached tokens total. Twenty concurrent 2k-prompt sessions plus outputs will be KV-limited and queue. Use `--kv-cache-dtype fp8 --max-model-len 16384`, or use the 94 GB `NVIDIA H100 NVL` ($2.59/h community) / 141 GB `NVIDIA H200` ($3.59/h community). It is not a reasoning model: no `--reasoning-parser`, the schema applies from the first token.
- Sampling: `generation_config.json` has `temperature: 0.6, top_p: 0.9` (read from the RedHatAI mirror of Meta's config, checked 2026-10-07).

### 1.6 Newer well-known open-weight models (2026) that fit one 80 GB GPU

| Model | Released | Size / fits 80 GB? | vLLM parser | Notes / source (checked 2026-10-07) |
|---|---|---|---|---|
| `google/gemma-4-31B-it` | 2026-04-02 (tech report arXiv 2607.02770) | 30.7B dense, ~62 GB BF16, 256K ctx; fits | `gemma4` (needs `enable_thinking=true` or `reasoning_effort`) | Apache 2.0; sampling T=1.0, top_p=0.95, top_k=64. https://huggingface.co/google/gemma-4-31B-it ; https://datanorth.ai/news/google-releases-gemma-4-open-models |
| `google/gemma-4-26B-A4B-it` | 2026-04-02 | 26B MoE, 3.8B active, ~52 GB BF16; fits | `gemma4` | same family; much faster decode |
| `Qwen/Qwen3.6-27B` / `-FP8` | 2026-04-23 (FP8 repo created 2026-04-21) | 27B dense, ~54 GB BF16 / 27.8 GB FP8, 262K ctx; fits | `qwen3` (vLLM >= 0.19) | thinking by default, `enable_thinking` toggle, `preserve_thinking` option; `--tool-call-parser qwen3_coder`. https://huggingface.co/Qwen/Qwen3.6-27B ; https://huggingface.co/api/models/Qwen/Qwen3.6-27B-FP8 |
| `Qwen/Qwen3.6-35B-A3B` / `-FP8` | 2026-04-17 | 35B MoE, 3B active; recipe: FP8 fits a single H100/H200, BF16 needs 1x H200 or 2x H100 | `qwen3` (vLLM >= 0.19) | https://recipes.vllm.ai/Qwen/Qwen3.6-35B-A3B ; https://huggingface.co/Qwen/Qwen3.6-35B-A3B |
| `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16` | 2025-12-15 | 31B hybrid Mamba2-MoE, 3B active; fits (TP=1) | registry has `nemotron_v3`; the Dec-2025 vLLM blog used `--reasoning-parser nano_v3` — verify on v0.31.0 | https://blog.vllm.ai/2025/12/15/run-nvidia-nemotron-3-nano.html ; https://recipes.vllm.ai/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-BF16 |
| Not a fit on one 80 GB GPU | | | | Mistral Small 4 (2026-03-16, 119B-A6B, Apache 2.0, has `reasoning_effort`; ~120 GB even in FP8) https://mistral.ai/news/mistral-small-4/ ; GLM-5.3-Flash (320B-A18B) and DeepSeek-V4.1-Flash (552B) need 8-GPU nodes https://www.yottalabs.ai/post/deepseek-v4-1-flash-vs-glm-5-3-flash-2026 ; Llama 4 Scout (109B-A17B, FP8 ~110 GB); "Llama 5" reports (600B+ MoE, 2026-04-08) are inconsistent across sources and in any case multi-GPU |

Unverified but visible in vLLM's own tracker: a "Qwen3.8" line (`Qwen/Qwen3.8-27B-FP8` in issue #53284, "Qwen3.8-Flash-Next" in the v0.29.0 notes). Not researched further here.

---

## 2. Model-card recommended sampling (checked 2026-10-07)

| Model | Mode | temperature | top_p | top_k | min_p | presence / repetition penalty | Source |
|---|---|---|---|---|---|---|---|
| gpt-oss-120b / 20b | any effort | 1.0 | 1.0 | n/a | n/a | none stated; `generation_config.json` only sets `do_sample: true` | https://github.com/openai/gpt-oss ("We recommend sampling with temperature=1.0 and top_p=1.0"); https://huggingface.co/openai/gpt-oss-120b/raw/main/generation_config.json |
| Qwen3-32B, Qwen3-30B-A3B | thinking | 0.6 | 0.95 | 20 | 0 | presence 0-2 optional; "DO NOT use greedy decoding" | https://huggingface.co/Qwen/Qwen3-32B ; https://huggingface.co/Qwen/Qwen3-30B-A3B |
| Qwen3-32B, Qwen3-30B-A3B | non-thinking | 0.7 | 0.8 | 20 | 0 | presence 0-2 optional | same |
| Qwen3-30B-A3B-Thinking-2507 | thinking only | 0.6 | 0.95 | 20 | 0 | presence 0-2 optional; output length 32,768 (81,920 for hard math/code) | https://huggingface.co/Qwen/Qwen3-30B-A3B-Thinking-2507 |
| Qwen3.6-27B | thinking (general) | 1.0 | 0.95 | 20 | 0.0 | presence 0.0 | https://huggingface.co/Qwen/Qwen3.6-27B |
| Qwen3.6-27B | thinking (coding) | 0.6 | 0.95 | 20 | 0.0 | presence 0.0 | same |
| Qwen3.6-27B | non-thinking | 0.7 | 0.80 | 20 | 0.0 | presence 1.5 | same |
| Qwen3.6-35B-A3B | thinking (general) | 1.0 | 0.95 | 20 | 0.0 | presence 1.5, repetition 1.0 | https://huggingface.co/Qwen/Qwen3.6-35B-A3B |
| Qwen3.6-35B-A3B | thinking (coding) / non-thinking | 0.6 / 0.7 | 0.95 / 0.80 | 20 | 0.0 | presence 0.0 / 1.5 | same |
| Llama-3.3-70B-Instruct | n/a | 0.6 | 0.9 | n/a | n/a | none | `generation_config.json` (via RedHatAI mirror) |
| Gemma 4 31B-it | thinking or not | 1.0 | 0.95 | 64 | n/a | none | https://huggingface.co/google/gemma-4-31B-it |

vLLM accepts `top_k`, `min_p`, `repetition_penalty` as extra (non-OpenAI) body fields on chat completions.

---

## 3. RunPod: prices, GPU IDs, SDK

### 3.1 On-demand prices per GPU-hour (1 GPU), checked 2026-10-07

Pricing page states "last updated September 27, 2026"; the same numbers were returned by RunPod's public GraphQL `gpuTypes { securePrice communityPrice }` query on 2026-10-07 (no API key needed).

| GPU (`gpu_type_id`) | Display name | VRAM | Community | Secure |
|---|---|---|---|---|
| `NVIDIA H100 80GB HBM3` | H100 SXM | 80 GB | $2.69 | $3.49 |
| `NVIDIA H100 PCIe` | H100 PCIe | 80 GB | $1.99 | $2.89 |
| `NVIDIA H100 NVL` | H100 NVL | 94 GB | $2.59 | $3.19 |
| `NVIDIA A100-SXM4-80GB` | A100 SXM | 80 GB | $1.39 | $1.59 |
| `NVIDIA A100 80GB PCIe` | A100 PCIe | 80 GB | $1.19 | $1.59 |
| `NVIDIA L40S` | L40S | 48 GB | $0.79 | $1.09 |
| `NVIDIA GeForce RTX 4090` | RTX 4090 | 24 GB | $0.34 | $0.74 |
| `NVIDIA GeForce RTX 5090` | RTX 5090 | 32 GB | $0.69 | $0.99 |
| `NVIDIA RTX PRO 6000 Blackwell Server Edition` | RTX PRO 6000 | 96 GB | $1.69 | $2.09 |
| `NVIDIA H200` | H200 SXM | 141 GB | $3.59 | $4.59 |
| `NVIDIA B200` | B200 | 180 GB | $5.98 | $6.79 |

Storage: container disk $0.10/GB-month; volume $0.10/GB-month running ($0.20 idle); network volume $0.07/GB-month. Billing is per second. Sources: https://www.runpod.io/pricing ; https://docs.runpod.io/references/gpu-types ; `POST https://api.runpod.io/graphql` query `{ gpuTypes { id displayName memoryInGb secureCloud communityCloud securePrice communityPrice } }`.

Notes: only the 80 GB+ cards can host gpt-oss-120b / 32B-class models at 32k context with 20 sessions; L40S/4090/5090 are out (48/24/32 GB). The RTX PRO 6000 (96 GB, $1.69 community) is the cheapest 80 GB+ card but is Blackwell (sm_120): the gpt-oss recipe marks RTX 5090/Blackwell support as "upcoming" for some kernels and needs `VLLM_USE_FLASHINFER_MOE_MXFP4_MXFP8=1`; treat as experimental. H100 PCIe is ~25% cheaper than SXM but has lower memory bandwidth (2.0 vs 3.35 TB/s), which directly slows decode.

### 3.2 Python SDK `runpod` 1.12.0

- Latest PyPI version is **1.12.0**, released 2026-08-10 (GitHub release; also PyPI `info.version`), `requires_python >= 3.10` (1.10.0 dropped 3.8/3.9). Sources: https://pypi.org/pypi/runpod/json ; https://github.com/runpod/runpod-python/releases (checked 2026-10-07).
- `create_pod` signature on `main` (`runpod/api/ctl_commands.py`): `create_pod(name, image_name="", gpu_type_id=None, cloud_type="ALL", support_public_ip=False, start_ssh=True, data_center_id=None, country_code=None, gpu_count=1, volume_in_gb=0, container_disk_in_gb=None, min_vcpu_count=1, min_memory_in_gb=1, docker_args=None, ports=None, volume_mount_path=None, env=None, template_id=None, network_volume_id=None, allowed_cuda_versions=None, min_download=None, min_upload=None, instance_id=None) -> dict`. `cloud_type` must be `"ALL"`, `"COMMUNITY"` or `"SECURE"`; `docker_args` is sent as the pod's `args`; `ports` is a comma-separated `"<port>/http,<port>/tcp"` string; `volume_mount_path` defaults to `/runpod-volume`. Also present: `get_pod(pod_id)`, `get_pods()`, `stop_pod(pod_id)`, `resume_pod(pod_id, gpu_count=None)`, `terminate_pod(pod_id) -> None`, `get_gpu(gpu_id)`, `get_gpus()`. Source: https://raw.githubusercontent.com/runpod/runpod-python/main/runpod/api/ctl_commands.py (checked 2026-10-07). The 1.10-1.12 release notes list no breaking changes to these functions (changes were serverless-side: VolumeCache, fitness checks, SSRF fix).
- API key: `runpod.api_key = "..."` works (README example), or set `RUNPOD_API_KEY` — `runpod/__init__.py` does `api_key = _load_api_key()` (env first, then stored credentials) and `runpod/api/graphql.py` imports `api_key` from the `runpod` module at call time, raising `AuthenticationError("No API key provided")` if unset. Sources: https://raw.githubusercontent.com/runpod/runpod-python/main/README.md ; `.../runpod/__init__.py` ; `.../runpod/api/graphql.py` (checked 2026-10-07). The SDK talks to `https://api.runpod.io/graphql`; RunPod's docs separately say REST API v1 (`rest.runpod.io/v1`) retires 2026-11-15, which does not affect the GraphQL-based SDK.
- Proxy URL format is `https://[POD_ID]-[INTERNAL_PORT].proxy.runpod.net`, i.e. `https://<pod_id>-8000.proxy.runpod.net` for port `8000/http`. **Cloudflare enforces a 100-second maximum connection time on the proxy; a response not started within 100 s fails with HTTP 524.** For long non-streaming reasoning requests either (a) use `stream=True` and reassemble (the first bytes arrive quickly, which keeps the connection alive), or (b) expose `8000/tcp` and call `http://<public_ip>:<mapped_port>` directly (no proxy, no 100 s cap; public IPs are stable on Secure Cloud, may change on Community restarts). Sources: https://docs.runpod.io/pods/configuration/expose-ports ; https://www.runpod.io/blog/runpod-proxy-guide (checked 2026-10-07).

Example (unchanged API from the prior study):
```python
import runpod
runpod.api_key = os.environ["RUNPOD_API_KEY"]
pod = runpod.create_pod(
    name="eval-gptoss120b",
    image_name="vllm/vllm-openai:v0.31.0-cu129",
    gpu_type_id="NVIDIA H100 80GB HBM3",
    cloud_type="SECURE",            # or "COMMUNITY"
    gpu_count=1,
    volume_in_gb=120, container_disk_in_gb=40, volume_mount_path="/root/.cache/huggingface",
    ports="8000/http,22/tcp",
    allowed_cuda_versions=["12.9", "13.0"],
    docker_args="--model openai/gpt-oss-120b --served-model-name openai/gpt-oss-120b --host 0.0.0.0 --port 8000 --max-model-len 32768 --enable-prefix-caching --gpu-memory-utilization 0.92 --max-num-seqs 64 --max-num-batched-tokens 4096 --reasoning-parser openai_gptoss --dtype auto",
    env={"HF_TOKEN": os.environ["HF_TOKEN"], "VLLM_SYSTEM_START_DATE": "2026-10-07"},
)
```

---

## 4. Throughput ballpark and run-time / cost estimate

Published numbers (checked 2026-10-07):

- **gpt-oss-120b, 1x H100, vLLM, MXFP4, 8k-in / 1k-out chat workload** (SemiAnalysis InferenceX, runs 2026-03-27..2026-05-17): 8,973 output tok/s per GPU at 50 tok/s per user; 7,136 at 75; 5,863 at 100; 3,931 at 150 (~26 concurrent users); 2,558 at 200 (~13 users); peak 10,284 tok/s. Our prompts are 4x shorter, so these are conservative. https://inferencex.semianalysis.com/run/gptoss-120b-on-h100
- **32B dense (DeepSeek-R1-Distill-Qwen-32B / QwQ-32B, same architecture as Qwen3-32B), BF16, vLLM**: A100 80GB at 50 concurrent, 100-in/600-out: 472-522 output tok/s, TPOT 53-60 ms (~17-19 tok/s per stream); H100 80GB at 300 requests (default scheduler): 1,214 output tok/s. https://www.databasemart.com/blog/vllm-gpu-benchmark-a100-80gb ; https://databasemart.com/blog/vllm-gpu-benchmark-h100 (both "last updated 08/25/2026"). Qwen3-32B on H100 with vLLM v0.11.0 at 1,000 concurrent ShareGPT requests: 1,131 output tok/s, TPOT 88 ms. https://markaicode.com/benchmarks/groq-qwen-3-h100-cold-start-benchmark/
- Single-stream reference: Qwen3.6-35B-A3B decodes 97.7 tok/s on a DGX Spark (GB10) per the vLLM recipe, i.e. a 3B-active MoE is several times faster than a 32B dense model on any card.

Working estimates for this harness (20 in flight, 2k prompt, 300 visible + R hidden reasoning tokens per call; the R values are assumptions, not measurements):

| Model on 1x H100 SXM | Aggregate decode at 20 streams | R=500 (low effort) | R=1,500 (medium) | R=3,000 (high / Qwen thinking) |
|---|---|---|---|---|
| gpt-oss-120b (MXFP4) | ~3,000-4,000 tok/s (150-200 tok/s per stream) | 6.4M tok -> ~30 min, ~$1.5-2 | 14.4M tok -> ~70 min, ~$3-4 | 26.4M tok -> ~2.1 h, ~$6-8 |
| Qwen3-32B-FP8 | ~600-900 tok/s (30-45 tok/s per stream; FP8 ~1.3-1.5x faster than the BF16 figures above) | ~2.5 h, ~$7-9 | ~5.5 h, ~$15-19 | ~10 h, ~$27-35 |
| Qwen3.6-35B-A3B-FP8 | ~2,500-4,000 tok/s (MoE, 3B active) | ~35 min | ~1.3 h | ~2.3 h |

Add ~10-20 min of pod start + weight download (gpt-oss-120b ~61 GB; Qwen3-32B-FP8 ~33 GB) and ~5-10 min of prefill for 16M prompt tokens (prefix caching of the shared system prompt reduces this). Costs use $2.69-3.49/h for H100 SXM. The 100-second proxy cap matters for the dense model: 3,000 reasoning tokens at 35 tok/s is ~86 s before queueing, so stream or use the TCP port.

---

## 5. Known problems with `json_schema` on reasoning models, and mitigations

1. **`include_reasoning=false` + `response_format` -> `content: null`** (vLLM #48863, open, introduced v0.23.1; reproduced on Qwen3-32B and, by a downstream project on vLLM 0.30 with Qwen3.6-35B-A3B-FP8, https://github.com/intrafind/ihub-apps/issues/2631). Mitigation: keep `include_reasoning` at its default `true` and ignore `message.reasoning` client-side; to suppress thinking use `chat_template_kwargs={"enable_thinking": false}` instead.
2. **Reasoning exhausts `max_tokens` -> `finish_reason="length"`, `content` null or truncated, reasoning only.** The grammar is only engaged after the parser sees the end of reasoning (section 1.2), so a run that never reaches it yields no JSON at all; there is no per-request thinking budget in v0.31.0's `ChatCompletionRequest`. Mitigations: set `max_tokens` well above the expected reasoning length (4,096-8,192 for medium effort; the Qwen 2507 cards recommend 32,768 for thinking models); prefer `reasoning_effort="low"`/`"medium"` for gpt-oss or `enable_thinking=false` for Qwen when the task does not need deliberation; treat `finish_reason == "length"` or `content is None` as a retry (new `seed`, lower effort) rather than a parse error; log `usage.completion_tokens` per call.
3. **The schema does not constrain the reasoning channel** (by design; `enable_in_reasoning` default False). Do not turn `enable_in_reasoning` on: with the Qwen parser it routes the constrained JSON into `reasoning` (#50948, open).
4. **Parser/template disagreement -> answer in `reasoning`, `content` null** (#53284, open; #35221 fixed in v0.17.0). Mitigation: use the stock chat template; pass `enable_thinking` explicitly whenever you disable it; do not rely on community templates with `reasoning_effort: "none"` switches for Qwen.
5. **`stop` strings match inside reasoning** (#59515) -> `content=null`, `finish_reason="stop"`. Mitigation: do not send `stop`; the schema's closing brace ends generation.
6. **gpt-oss specifics**: multi-turn + `json_object` bug fixed in v0.16.0 (#32791); long-context slowdown of structured output + reasoning fixed in v0.20.0 (#37897); only `low|medium|high` are valid `reasoning_effort` values for harmony; the `guidance` backend problem (#37359) affects offline `LLM.generate()` only, so keep backend `auto`; prefer `json_schema` over `json_object`; pin `VLLM_SYSTEM_START_DATE` for day-to-day reproducibility.
7. **Field rename**: read `message.reasoning`; `reasoning_content` is gone from responses (v0.16-v0.28 changes, section 1.2).
8. **Determinism**: per-request `seed` is honoured, but vLLM only promises reproducibility "on the same hardware and the same vLLM version", and outputs can change with batch composition unless batch-invariant mode is enabled (which costs throughput). Source: https://docs.vllm.ai/en/stable/usage/reproducibility.html (checked 2026-10-07). Record seed + image tag + GPU type with every run.
9. **Schema features**: xgrammar is the default and rejects unsupported JSON-schema features; an over-deep schema now returns HTTP 400 instead of 500 (PR #60036, Oct 2026). Keep schemas flat, avoid `$ref` recursion and `pattern`-heavy strings; `--structured-outputs-config.disable_any_whitespace=true` yields compact JSON if whitespace bloat matters. Prompts should still describe the schema in words ("can improve the results notably", docs).
10. **Tools + `response_format`** suppress tool calls under `tool_choice="auto"` (#39929) — irrelevant unless the harness passes `tools`.

---

## Sources (all checked 2026-10-07)

- vLLM releases: https://github.com/vllm-project/vllm/releases ; https://github.com/vllm-project/vllm/releases/tag/v0.31.0
- Docker Hub tags: https://hub.docker.com/v2/repositories/vllm/vllm-openai/tags?name=v0.31
- vLLM docs: https://docs.vllm.ai/en/stable/features/reasoning_outputs.html ; https://docs.vllm.ai/en/stable/features/structured_outputs.html ; https://docs.vllm.ai/en/stable/usage/reproducibility.html ; https://docs.vllm.ai/en/stable/api/vllm/reasoning/
- vLLM v0.31.0 source (via GitHub API): `vllm/entrypoints/openai/chat_completion/protocol.py`, `vllm/entrypoints/openai/chat_completion/serving.py`, `vllm/entrypoints/openai/parser/harmony_utils.py`, `vllm/parser/harmony.py`, `vllm/reasoning/__init__.py`, `vllm/reasoning/gptoss_reasoning_parser.py`, `vllm/v1/structured_output/__init__.py`, `vllm/config/structured_outputs.py`, `vllm/envs.py`
- vLLM recipes: https://docs.vllm.ai/projects/recipes/en/latest/OpenAI/GPT-OSS.html (raw: https://raw.githubusercontent.com/vllm-project/recipes/main/OpenAI/GPT-OSS.md, `OpenAI/GPT-OSS_Hopper.yaml`) ; https://docs.vllm.ai/projects/recipes/en/latest/Llama/Llama3.3-70B.html (`Llama/Llama3.3_Hopper.yaml`) ; https://recipes.vllm.ai/Qwen/Qwen3.6-35B-A3B ; https://recipes.vllm.ai/Qwen/Qwen3-32B
- vLLM issues/PRs: #48863, #53284, #50948, #35221 / PR #35230, #59515, #32791 / PR #34454, #37897 / PR #35745, #37359, #23837, #39929, PR #60036
- Model cards: https://huggingface.co/openai/gpt-oss-120b ; https://github.com/openai/gpt-oss ; https://huggingface.co/Qwen/Qwen3-32B ; https://huggingface.co/Qwen/Qwen3-30B-A3B ; https://huggingface.co/Qwen/Qwen3-30B-A3B-Thinking-2507 ; https://huggingface.co/Qwen/Qwen3.6-27B ; https://huggingface.co/Qwen/Qwen3.6-35B-A3B ; https://huggingface.co/google/gemma-4-31B-it ; https://huggingface.co/RedHatAI/Llama-3.3-70B-Instruct-FP8-dynamic ; HF API: `api/models/Qwen/Qwen3-32B-FP8`, `api/models/Qwen/Qwen3.6-27B-FP8`, `api/models/openai/gpt-oss-120b`
- 2026 model news: https://datanorth.ai/news/google-releases-gemma-4-open-models ; https://mistral.ai/news/mistral-small-4/ ; https://www.yottalabs.ai/post/deepseek-v4-1-flash-vs-glm-5-3-flash-2026 ; https://blog.vllm.ai/2025/12/15/run-nvidia-nemotron-3-nano.html
- RunPod: https://www.runpod.io/pricing ; https://docs.runpod.io/references/gpu-types ; https://docs.runpod.io/api-reference/pods/POST/pods.md ; https://docs.runpod.io/pods/configuration/expose-ports ; https://www.runpod.io/blog/runpod-proxy-guide ; https://pypi.org/pypi/runpod/json ; https://github.com/runpod/runpod-python/releases ; https://raw.githubusercontent.com/runpod/runpod-python/main/runpod/api/ctl_commands.py ; `.../runpod/__init__.py` ; `.../runpod/api/graphql.py` ; `.../README.md` ; public GraphQL `https://api.runpod.io/graphql` (`gpuTypes`)
- Throughput: https://inferencex.semianalysis.com/run/gptoss-120b-on-h100 ; https://www.databasemart.com/blog/vllm-gpu-benchmark-a100-80gb ; https://databasemart.com/blog/vllm-gpu-benchmark-h100 ; https://markaicode.com/benchmarks/groq-qwen-3-h100-cold-start-benchmark/
- NVIDIA driver table: https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html
