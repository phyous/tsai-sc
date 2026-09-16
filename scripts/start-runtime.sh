#!/usr/bin/env bash
# Runs only a dedicated emulator profile; never attaches to your logged-in browser.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
export PATH="$ROOT/.runtime/bun/node_modules/.bin:$PATH"
command -v bun >/dev/null || { echo 'Run scripts/setup-runtime.sh first.' >&2; exit 1; }
[ -f .runtime/bottleship/public/starcraft_demo.wgb ] || { echo 'Run scripts/setup-runtime.sh first.' >&2; exit 1; }
if curl --silent --fail http://127.0.0.1:9333/json/version >/dev/null 2>&1; then
  echo 'Port 9333 already belongs to a browser. Stop the prior game runtime before starting another.' >&2
  exit 1
fi
if curl --silent --fail http://127.0.0.1:5174/ >/dev/null 2>&1; then
  echo 'Port 5174 already serves an application. Stop it before starting the game runtime.' >&2
  exit 1
fi
case "$(uname -s)" in
  Darwin) CHROME="${TSAI_CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}" ;;
  *) CHROME="${TSAI_CHROME:-$(command -v google-chrome || command -v chromium || true)}" ;;
esac
[ -x "$CHROME" ] || { echo 'Set TSAI_CHROME to a WebGPU-capable Chrome/Chromium executable.' >&2; exit 1; }
mkdir -p .runtime/logs
PROFILE_DIR="$(mktemp -d "$ROOT/.runtime/chrome-XXXXXX")"
pids=()
cleanup() { for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM
(cd .runtime/bottleship && exec bun run dev --host 127.0.0.1 --strictPort) > .runtime/logs/vite.log 2>&1 &
pids+=("$!")
for _ in {1..100}; do
  if curl --silent --fail http://127.0.0.1:5174/ >/dev/null 2>&1; then break; fi
  sleep 0.2
done
"$CHROME" --headless=new --remote-debugging-port=9333 --user-data-dir="$PROFILE_DIR" \
  --no-first-run --no-default-browser-check --autoplay-policy=no-user-gesture-required \
  --enable-unsafe-webgpu --window-size=1400,1050 about:blank > .runtime/logs/chrome.log 2>&1 &
pids+=("$!")
for _ in {1..100}; do
  if curl --silent --fail http://127.0.0.1:9333/json/version >/dev/null 2>&1; then break; fi
  sleep 0.2
done
(cd .runtime/bottleship && bun tools/harness.ts up)
bun engine/bridge.ts &
pids+=("$!")
printf 'Game harness: http://127.0.0.1:3917\nLeave this terminal running. In another terminal: python -m tsai_sc.boot\n'
wait
