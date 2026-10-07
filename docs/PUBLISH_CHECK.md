# Publish check — hygiene scan before making this repository public (2026-10-07)

Scope: every path that `git add .` would pick up with the current `.gitignore` (excluded: `runs/`, `.tools/`, `.venv/`,
`showdown/node_modules/`, `*.pod`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.DS_Store`, `.claude/hive/` which ignores itself).
State at scan time: `git init` done on branch `main`, zero commits, no remote; `git ls-files` is empty, so sizes are estimated
with `find`/`stat`. Nothing in the tree was modified by this scan; this file is the only addition.

## Severity-ordered findings

### 1. HIGH — absolute home path in code (`harness/showdown.py`)

| File:line | Text | Why it matters |
|---|---|---|
| `harness/showdown.py:30` | `NODE_PATH = os.environ.get("PERMADEATH_NODE", "./.tools/node/bin/node")` | default breaks on every other machine unless the env var is set; exposes the username |
| `harness/showdown.py:31` | `SIM_SCRIPT = os.environ.get("PERMADEATH_SIM", "./showdown/sim_stdio.js")` | same |

Fix (keeps the env overrides):

```python
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE_PATH: str = os.environ.get("PERMADEATH_NODE", os.path.join(_ROOT, ".tools", "node", "bin", "node"))
SIM_SCRIPT: str = os.environ.get("PERMADEATH_SIM", os.path.join(_ROOT, "showdown", "sim_stdio.js"))
```

`harness/showdown.py:32` (`DEX_PATH`) and `harness/dex.py:15` already use the `__file__`-relative form. No other code file
contains `<home>`.

### 2. MEDIUM — repository size: 221 files, 94.6 MB, 82 MB of it rendered animations

No file exceeds 8 MB (largest: `results/pilot/anim_final/battle_4_replay.gif`, 7.32 MB) and nothing approaches GitHub's
100 MB per-file limit, but the first push would carry ~82 MB of GIF/MP4 in four near-duplicate sets:

| Directory | Size (du) | Contents |
|---|---|---|
| `results/main/anim_final/` | 27 MB | 3 GIF + 3 MP4 + 3 contact PNG |
| `results/v3/anim/` | 27 MB | same |
| `results/v2/anim/` | 24 MB | same |
| `results/pilot/anim_final/` | 24 MB | same |
| `results/sprites/` | 4.3 MB | 98 sprite images + README |
| `harness/data/dex_gen9.json` | 0.9 MB | dex dump |
| everything else (code, docs, md, json, fig PNGs, lockfile) | ~2 MB | |

Options, in order of preference: (a) commit only the final study's animations (`results/v3/anim/`) plus the one GIF the README
embeds, and keep the rest as a GitHub Release asset; (b) track `results/**/*.gif` and `results/**/*.mp4` with Git LFS; (c) commit as
is (allowed, but clones pull ~95 MB and GitHub warns above 50 MB per file only, so nothing blocks it).

### 3. MEDIUM — RunPod pod hostnames in `FREEZE.json`

| File:line | Value |
|---|---|
| `results/pilot/FREEZE.json:6` | `"base_url": "https://ljux39xtdiqy1n-8000.proxy.runpod.net"` |
| `results/main/FREEZE.json:6` | `"base_url": "https://m40tpk8p4xgkkb-8000.proxy.runpod.net"` |
| `results/v2/FREEZE.json:6` | `"base_url": "https://x1g0n9eauml3k7-8000.proxy.runpod.net"` |
| `results/v3/FREEZE.json:6` | `"base_url": "https://rmcurau1aben5j-8000.proxy.runpod.net"` |

Not credentials, and the pods are destroyed (`MAIN_NOTES.md`, `V2_NOTES.md`), but they are account-linked pod ids with no
reproducibility value. Replace the four values with the template `https://<pod_id>-8000.proxy.runpod.net` (the form already used in
`runpod/pod.py:117`, `harness/run.py:3`, `docs/APPROVAL.md:10`), and change `analysis/freeze.py` (around line 28-31, which records
`args.base_url`) to store the template or the engine info only.

### 4. MEDIUM/cosmetic — username in generated results (data and docs, not code)

| File:line | Text |
|---|---|
| `results/{pilot,main,v2,v3}/numbers.json:2` | `"root": "./runs/<study>"` |
| `results/{pilot,main,v2,v3}/report.md:3` | ``Root: `./runs/<study>`.`` |

Source: `analysis/report.py:77` (`"root": os.path.abspath(root)`) and `:185` (prints it). Nothing reads `root` back, so this is
cosmetic; it still exposes the username eight times. Fix `report.py` to write `os.path.relpath(root, <project root>)` and either
re-run `analysis.report` for the four studies or patch the eight lines in place:

```
sed -i '' 's#./#./#g' results/*/numbers.json results/*/report.md
```

### 5. LOW — first name, shared-account wording and a personal tooling path

The `julian-permadeath-` pod-name prefix in `runpod/pod.py` is deliberate (recorded here for completeness, no action implied).
The same first name and the account wording also appear outside `pod.py`; decide once whether the linkage first name + handle
(`@kilojoules`, `buid_spec.md:3`) + commit email (item 6) is acceptable for a public repo.

| File:line | Text | Note |
|---|---|---|
| `runpod/pod.py:36` | `OWNER = os.environ.get("RUNPOD_OWNER", "julian")` | deliberate; a neutral default (`"owner"`) would remove the name from code |
| `runpod/pod.py:1` | `following ~/.skills/runpod-ops/SKILL.md` | path to a private file nobody else has; dangling reference |
| `runpod/pod.py:11-12`, `:286`, `:293` | "the key in ~/.super_lab_run.pod is a SHARED account", "OTHERS on the shared account" | reveals the key-file name and that the account is shared |
| `runpod/launch.sh:8` | "Pods are named julian-permadeath-<mode> on the shared account (OWNER=julian)" | same |
| `docs/APPROVAL.md:13`, `:17`, `:18` | "per `~/.skills/runpod-ops/SKILL.md`", "shared `super_lab` key from `~/.super_lab_run.pod`", pod name row | same |
| `results/main/MAIN_NOTES.md:4` | "pod `julian-permadeath-main`" | run record; fine to keep if the name stays |

Suggested wording if you want it neutral: "the RunPod key file (`~/.super_lab_run.pod`) or `RUNPOD_API_KEY`"; drop the
`~/.skills/...` references.

### 6. LOW — commit author identity (not in the tree, but in every commit)

`.git/config` sets `user.name = kilojoules` and `user.email = quectojoules@gmail.com`. The config file is never published, but the
email goes into the author field of every commit and is visible on GitHub (commit `.patch` views). No commits exist yet, so if you
want it hidden: `git config user.email "<id>+kilojoules@users.noreply.github.com"` before the first commit, and enable "Keep my email
addresses private" on GitHub.

### 7. LOW/cosmetic

- `showdown/package.json`: `"license": "ISC"`, empty `author`, no `"private": true`. Set `"license": "MIT"` (or
  `"SEE LICENSE IN ../LICENSE"`) and `"private": true` so it matches the root license and is never published to npm.
- `buid_spec.md` (filename typo for "build"); referenced from `README.md`, `docs/DESIGN.md`, `docs/APPROVAL.md`. Rename only if you
  also update those three references.
- `requirements.txt` is a full `pip freeze` (fastapi, boto3, paramiko, sentry-sdk, opentelemetry, ... pulled in by the `runpod` SDK).
  Harmless; a trimmed direct-dependency list (`requests`, `scipy`, `matplotlib`, `numpy`, `pillow`, `imageio-ffmpeg`, `pytest`,
  `runpod`) would be friendlier, optional.
- Animation folders are named `anim_final/` in `results/main` and `results/pilot` but `anim/` in `results/v2` and `results/v3`.

## Clean checks (no action)

### (1) Secrets, tokens, keys

None found. Patterns searched over all to-be-committed files: `rpa_`, `hf_`, `sk-`, `AKIA`, `ghp_`, `github_pat_`, `npm_`,
`glpat-`, `xox[baprs]-`, `AIza`, JWT (`eyJ...`), `-----BEGIN ... PRIVATE KEY`, `RUNPOD_API_KEY=`, `HF_TOKEN=`, `x-api-key`, and
case-insensitive `api_key|bearer|password|passwd|secret|token`. Every hit is code that reads a key from the environment or a home
file and never prints it:

- `runpod/pod.py:35` (`KEY_FILES = ~/.super_lab_run.pod, ~/.run.pod`), `:41-50` (`api_key()`), `:54` and `:141` (sent as a query
  param / `Authorization: Bearer` header), `:100-106` (`hf_token()` from `~/.hf_token`, `~/.cache/huggingface/token`, `HF_TOKEN`),
  `:112` (the `plan` output masks it as `<from ~/.hf_token ...>`), `:169-171` (passed only into the pod's env at create time).
- `harness/backends.py:64-69` (`api_key` parameter → header), `harness/run.py:68,98` (`--api-key-env`, default `VLLM_API_KEY`).
- `docs/MODEL_OPTIONS.md:162-179` quotes SDK usage with `os.environ[...]` placeholders only.
- `harness/showdown.py:309-322` and `results/animate_battle.py:165`: "secret"/"public" are Showdown protocol terms for `|split|` lines.

32+ hex strings: exactly 11 distinct 64-hex values, all in `results/{pilot,main,v2,v3}/FREEZE.json` (5 per file: the per-arm
system-prompt SHA-256s, e.g. `e36b2740ba5a...` cited in `MAIN_NOTES.md:4`). Plus three 12-hex `harness_hash` values. None are secrets.

`.gitignore` already excludes `*.pod`, and no `.env*`, `*.pem`, `*.key`, `id_rsa*`, `.npmrc`, `.netrc`, `*.log`, `pod_id` or
`*.jsonl` file exists outside the ignored paths (`launch.sh` writes `pod_id` and `launch_*.log` under `runs/`, which is ignored).

### (4) Personal data

- Emails in the tree: only `i@izs.me` at `showdown/package-lock.json:1056` and `:2481` (an npm package maintainer's address inside
  the lockfile; standard, leave it). No other `@domain` address anywhere, including all results markdown.
- Real names / handles: `julian` (item 5), `@kilojoules` in `buid_spec.md:3`. `github.com/sponsors/...` URLs in the lockfile are
  upstream package metadata.
- No IP addresses (only `0.0.0.0` in `runpod/pod.py:86`, a bind address), no phone numbers, no 7+ digit numbers other than the seed
  `20261007` inside run ids.

### (5) Third-party content

| Item | Status | Needed |
|---|---|---|
| `pokemon-showdown@0.11.11` | not vendored: pinned in `showdown/package.json` + `showdown/package-lock.json` (93 KB, all `resolved` URLs on registry.npmjs.org), installed into the ignored `showdown/node_modules/` | nothing beyond a mention; it is MIT, (c) 2011-2026 Guangcong Luo and other contributors |
| `showdown/sim_stdio.js`, `showdown/ps_tool.js` | own code calling the package (12 and ~65 lines; `sim_stdio.js` reproduces the `simulate-battle` CLI contract, no copied source) | none |
| `harness/data/dex_gen9.json` (0.9 MB) | a data dump produced from the package by `ps_tool.js dex` (`harness/dex.py:3`) | carries the package's MIT notice: add a `THIRD_PARTY.md` (or a paragraph in LICENSE/README) naming pokemon-showdown, its MIT license and copyright line |
| `results/sprites/` (50 PNG + 48 GIF, 4.3 MB) | Pokémon artwork (c) Nintendo / Creatures Inc. / GAME FREAK inc., downloaded from play.pokemonshowdown.com; `results/sprites/README.md` already says so | exclude from the code license (item 7 below); keep the README; optional: drop the 48 animated GIFs if unused by the README figure |
| `results/*/anim*/` (60 GIF... of which 12 are animations, 12 MP4, 12 contact PNG) | rendered from those sprites, so they embed the same artwork | same exclusion as the sprites |
| `results/*/fig_*.png` | matplotlib charts, no sprites | none |
| Node v24.21.0 | downloaded by `setup.sh` from nodejs.org into the ignored `.tools/` | none |
| Python deps | `requirements.txt` pins only; nothing vendored | none |

### (6) Model outputs in `results/*/quotes.md`, `featured_*.md`, `session_*.md`, `probe.md`

25 markdown files (346 KB; largest `results/main/quotes.md` 91 KB). They are verbatim `thoughts` and hidden-reasoning text from
`openai/gpt-oss-120b` (an open-weights model) about the game, plus tool logs. Scanned for URLs, emails, phone numbers, long digit
runs, home paths, "password/address/phone/ssn/credit card/my name is", and vendor/person names: no private data of any kind; the only
identifiers are run ids such as `A__openai_gpt-oss-120b__s20261007__005` (arm, model, seed date, index) and the model name. Nothing
to redact. The four `report.md` files are the only results files with a home path (item 4).

### Other

- `.claude/hive/` contains a `.gitignore` of `*`, so `git status` already hides it; there is no `.claude/settings*.json`.
- `.git/hooks/` holds only the stock `*.sample` hooks.
- `results/v3/numbers.json` and `results/v3/report.md` exist (the task's fallback regeneration was not needed).
- No `LICENSE`, `NOTICE`, `CITATION.cff` or `pyproject.toml` exists yet.

## (7) Suggested `.gitignore` additions

Append to the existing file (everything currently present is correct and should stay):

```
# editor / agent state
.claude/
.vscode/
.idea/
# local env files, logs, pod bookkeeping (launch.sh writes these under runs/, which is ignored; belt and braces)
.env
.env.*
*.log
pod_id
# python build cruft
*.egg-info/
build/
dist/
.ruff_cache/
.mypy_cache/
# optional, if the rendered media moves to LFS or Releases:
# results/**/*.mp4
# results/**/anim*/
```

If `.claude/` is ignored wholesale and you later want to share project settings, carve out `!.claude/settings.json`.

## (7) Suggested LICENSE

Use **MIT** for the code and documentation (root `LICENSE`, "Copyright (c) 2026 kilojoules" or your legal name), with an explicit
carve-out for the third-party artwork. Suggested `LICENSE` tail or `README.md` "License" section:

```
## License

The harness, analysis code, tests, documentation and result tables in this repository are released under the MIT License (see
LICENSE).

Not covered by that license:

- `results/sprites/` and the rendered animations and contact sheets under `results/*/anim*/` contain Pokémon sprite artwork
  (c) Nintendo / Creatures Inc. / GAME FREAK inc., obtained from the Pokémon Showdown sprite CDN. They are included only to
  illustrate research results and are not licensed for any other use. Pokémon is a trademark of Nintendo.
- `harness/data/dex_gen9.json` is a data dump generated from pokemon-showdown 0.11.11, MIT License,
  (c) 2011-2026 Guangcong Luo and other contributors (https://pokemonshowdown.com/). pokemon-showdown itself is not vendored;
  `setup.sh` installs it from npm.
- The transcripts under `results/` are outputs of `openai/gpt-oss-120b` recorded during this study and are published as research
  data.
```

Add a `THIRD_PARTY.md` (or `NOTICE`) with the pokemon-showdown MIT text if you prefer to keep `LICENSE` pristine MIT, and a
`CITATION.cff` so the GitHub "Cite this repository" button works. If you want the result tables and notes to be reusable with
attribution, CC BY 4.0 for `docs/` and `results/*.md` is the usual pairing; otherwise MIT for everything non-sprite is fine.

## Checklist before `git add .`

1. Patch `harness/showdown.py:30-31` (item 1).
2. Decide on animations: trim to `results/v3/anim/` + the README GIF, or set up LFS (item 2).
3. Scrub the four `base_url` values in `results/*/FREEZE.json` and fix `analysis/freeze.py` (item 3).
4. Replace the eight `./` strings in `results/*/numbers.json` and `report.md`, and fix `analysis/report.py:77` (item 4).
5. Optional wording pass on `runpod/pod.py`, `runpod/launch.sh`, `docs/APPROVAL.md` (item 5); set the commit email (item 6).
6. Add `LICENSE`, the license section, `THIRD_PARTY.md`, the `.gitignore` additions, and `"private": true` in `showdown/package.json`.
7. `git add . && git status` and confirm no `runs/`, `.tools/`, `.venv/`, `node_modules/`, `__pycache__/`, `.DS_Store` or `*.pod` path appears.
