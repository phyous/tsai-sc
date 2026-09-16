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
for runtime_patch in "$PWD"/engine/patches/*.patch; do
  if git -C .runtime/bottleship apply --check "$runtime_patch" 2>/dev/null; then
    git -C .runtime/bottleship apply "$runtime_patch"
  elif git -C .runtime/bottleship apply --reverse --check "$runtime_patch" 2>/dev/null; then
    echo "Runtime patch already applied: $(basename "$runtime_patch")"
  else
    echo "Runtime patch does not match the pinned source: $runtime_patch" >&2
    exit 1
  fi
done
(cd .runtime/bottleship && bun install --frozen-lockfile)
(cd .runtime/bottleship && bun test tools/tests/ddraw-present-buffer-lifetime.test.ts)
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
