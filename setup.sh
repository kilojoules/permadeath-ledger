#!/usr/bin/env bash
# Reinstall the pinned toolchain: project-local Node 24.21.0, pokemon-showdown 0.11.11, Python 3.12 venv (uv).
set -euo pipefail
cd "$(dirname "$0")"
NODE_VER=v24.21.0
ARCH=$(uname -m); OS=$(uname -s | tr A-Z a-z)
case "$OS-$ARCH" in darwin-arm64) PLAT=darwin-arm64;; darwin-x86_64) PLAT=darwin-x64;; linux-x86_64) PLAT=linux-x64;; linux-aarch64) PLAT=linux-arm64;; *) echo "unsupported $OS-$ARCH"; exit 1;; esac
mkdir -p .tools
if [ ! -x .tools/node/bin/node ]; then
  curl -fsSL -o .tools/node.tar.gz "https://nodejs.org/dist/$NODE_VER/node-$NODE_VER-$PLAT.tar.gz"
  tar xzf .tools/node.tar.gz -C .tools && rm .tools/node.tar.gz && mv ".tools/node-$NODE_VER-$PLAT" .tools/node
fi
export PATH="$PWD/.tools/node/bin:$PATH"
(cd showdown && npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund)
command -v uv >/dev/null || { echo "install uv first: https://docs.astral.sh/uv/"; exit 1; }
[ -d .venv ] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m pytest tests -q -m "not slow"
echo "setup complete"
