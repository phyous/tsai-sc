#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
RUNTIME_COMMIT=a7c8543d75569d48890d48744897a0ffe3fb02f7
mkdir -p .runtime
if ! command -v bun >/dev/null; then
  npm install --prefix .runtime/bun bun@1.4.2
  export PATH="$PWD/.runtime/bun/node_modules/.bin:$PATH"
fi
if [ ! -d .runtime/bottleship/.git ]; then
  git clone https://github.com/jenissimo/bottleship .runtime/bottleship
fi
git -C .runtime/bottleship checkout "$RUNTIME_COMMIT"
git -C .runtime/bottleship submodule update --init --recursive
(cd .runtime/bottleship && bun install --frozen-lockfile)
if [ ! -f .runtime/bottleship/public/starcraft_demo.wgb ]; then
  curl --fail --location https://bottleship.pages.dev/apps/starcraft_demo.wgb -o .runtime/bottleship/public/starcraft_demo.wgb
fi
python3 - <<'PY'
from pathlib import Path
from hashlib import sha256
p=Path('.runtime/bottleship/public/starcraft_demo.wgb')
assert sha256(p.read_bytes()).hexdigest() == '8a32a481e17ef6e6125a3f439c92e3fa0ae566ff638393e8f991ae79c09f3f9a', 'Demo bundle checksum differs'
print('Verified StarCraft Shareware(ED) v4.00 bundle; kept local in .runtime/')
PY
