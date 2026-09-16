# Original StarCraft demo runtime

The runtime executes Blizzard's original StarCraft Shareware(ED) v4.00 executable
inside BottleShip's x86/Win32 emulator. No game-state memory is modified. Units,
resources, and victory are observed from guest memory; actions enter through the
emulator's normal mouse/keyboard input manager.

## Start

Requires Python 3, Node/npm (if Bun is absent), Git, and Chrome with WebGPU. On macOS
with an incomplete Xcode selection, set
`DEVELOPER_DIR=/Library/Developer/CommandLineTools` for setup.

```sh
scripts/setup-runtime.sh
scripts/start-runtime.sh
# In another terminal, from the repository root:
python -m tsai_sc.boot
```

Setup downloads BottleShip at commit
`a7c8543d75569d48890d48744897a0ffe3fb02f7` (Apache-2.0, with its third-party
notices), its v86 submodule, and the demo bundle hosted by that project. The bundle
SHA-256 is verified. Downloads, game files, browser profiles and logs stay under
the ignored `.runtime/` directory. The demo remains Blizzard's copyrighted
software and is not included in this repository. Read its bundled `License.txt`.

Startup creates a dedicated headless Chrome profile. It does not use browser
accounts or login sessions. The first boot navigates to Boot Camp using ordinary
input, dismisses the tips dialog, verifies the initial resources/units and parks
the emulator. A fresh profile is expected for that initial boot sequence. To restart an active
Boot Camp after stopping its controller, use `python -m tsai_sc.boot --restart`
or call `from tsai_sc.boot import restart; restart(bridge)`. This opens the game's
F10 / End Mission / Restart Mission / confirmation menus using ordinary input,
waits for the level to reload, then parks and verifies 150 minerals, zero gas,
17/18 supply, one SCV, one Command Center and one depot. The startup script
requires port 5174 and starts Vite with `--strictPort`; it never silently moves
the emulator to another development port.

## Python API

```python
from tsai_sc.engine import BottleShipBridge
b = BottleShipBridge()
b.capture('runs/frame.png')               # Exact 640×480 game canvas
b.read_memory(0x4EDF94, 4)                  # Guest bytes, read only
b.rpc('clickAt', 376, 145)                 # Guest-screen coordinates
b.rpc('clickHold', 480, 20, 100, 1)        # Right button: 1; left: 0
b.rpc('key', 'b')
b.step(24)                                # Resume then park after 24 presents
b.pause()
b.resume()
```

`step` counts **rendered presents**, not StarCraft logic frames. The original game
logic counter is at `0x4EDF94`. A controller requiring a game-time interval should
observe this counter while advancing. Rendering may present several times per
simulation update.

The persistent Bun bridge binds to `127.0.0.1:3917`. Routes: `GET /health`,
`GET /memory?addr=0x400000&len=64`, `GET /screenshot`, `POST /rpc` with
`{"cmd":"key","args":["b"]}`, and `POST /step` with `{"frames":24}`.
Memory reads are limited to 64 KiB per RPC; the Python client chunks larger reads.
The bridge permits an explicit set of game harness commands, rejects browser
Origin requests, and contains no model/API credentials. Screenshots use the same
canvas clipping approach as BottleShip's screenshot harness because its paused
DDraw presenter may return black frames through the worker capture method.

## Verification

```sh
python -m unittest discover -s tests -p test_engine.py -v
TSAI_TEST_BRIDGE_URL=http://127.0.0.1:3917 python -m unittest discover -s tests -p test_bridge_protocol.py -v
```

The client enforces loopback-only HTTP, disables proxies and redirects, checks
response sizes and read lengths, and excludes backend error bodies from logs.
The optional live checks send only invalid requests; they do not change the game.
A full fresh-profile boot has also been verified against the pinned executable,
including a 640×480 screenshot and the original mission's initial unit/resources.
