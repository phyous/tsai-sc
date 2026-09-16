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
python -m tsai_sc.boot --mission strongarm
```

Setup downloads BottleShip at commit
`a7c8543d75569d48890d48744897a0ffe3fb02f7` (Apache-2.0, with its third-party
notices), its v86 submodule, and the demo bundle hosted by that project. The bundle
SHA-256 is verified. Downloads, game files, browser profiles and logs stay under
the ignored `.runtime/` directory. The demo remains Blizzard's copyrighted
software and is not included in this repository. Read its bundled `License.txt`.

Startup creates a dedicated headless Chrome profile. It does not use browser
accounts or login sessions. `--mission strongarm` enters the first combat mission
through the original **Skip Tutorial** button, then verifies the starting state
and parks the game. The objective shown by the game is **Destroy the rebel base**.
Use `--mission boot_camp` for the optional economy tutorial. A fresh profile is
expected for either initial boot sequence.

After stopping the controller, restart the current mission with
`python -m tsai_sc.boot --restart`, or call
`from tsai_sc.boot import restart; restart(bridge)`. The helper identifies the
active mission, uses its F10 / End Mission / Restart Mission / confirmation menus,
and verifies the untouched starting state after reload:

| Mission | Minerals / gas | Supply | SCVs / Marines | Starting buildings |
| --- | --- | --- | --- | --- |
| Strongarm | 250 / 200 | 12 / 42 | 4 / 8 | 1 Command Center, 4 Depots, 1 Refinery, 2 Barracks, 1 Engineering Bay |
| Boot Camp | 150 / 0 | 17 / 18 | 1 / 16 | 1 Command Center, 1 Depot |

Restarting does not change scenarios. To select another scenario, start a fresh
runtime profile. The startup script requires port 5174 and uses Vite
`--strictPort`; it never silently moves the emulator to another port.

The normal campaign progression has also been verified: the tutorial's original
**Victory** dialog leads to a **Victory!** score screen. Its **OK** button continues
to the **Strongarm** chapter title and mission. Within the mission, F10 → Mission
Objectives displays the original objective. These screens are ordinary game UI;
the harness never changes campaign progress through memory writes.

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
Origin requests, and contains no model/API credentials. Screenshots copy the
original 640×480 DirectDraw CPU surface and its attached 256-color palette in one
synchronous, read-only worker snapshot. The bridge encodes those original pixels
as PNG without GPU readback. It requires an unambiguous 8-bit CPU surface and
fails if that source is unavailable. No arbitrary evaluation or framebuffer
address is accepted from a caller. See [capture implementation](cpu-capture.ts).

Setup also applies the checked-in [renderer patch](patches/) to the pinned
BottleShip revision. StarCraft's CPU-drawn frames create temporary palette-upload
buffers in the GPU presenter; the patch releases them after the frame's command
encoders have been submitted. Earlier unpatched runs exhausted paging space and
lost the Chrome GPU process. CPU recording and GPU buffer cleanup address the
capture dependency and allocation lifetime separately.

## Verification

```sh
python -m unittest discover -s tests -p test_engine.py -v
python -m unittest discover -s tests -p test_boot.py -v
bun test engine/cpu-capture.test.ts
TSAI_TEST_BRIDGE_URL=http://127.0.0.1:3917 python -m unittest discover -s tests -p test_bridge_protocol.py -v
```

The client enforces loopback-only HTTP, disables proxies and redirects, checks
response sizes and read lengths, and excludes backend error bodies from logs.
The optional live checks send only invalid requests; they do not change the game.
Runtime verification covers a fresh Boot Camp profile, its original victory
dialog and score screen, and normal progression into Strongarm with the initial
roster above. The direct Skip Tutorial startup has also passed end-to-end in a separate fresh
headless browser profile. It checks whether the chapter title's Enter already
started gameplay before clicking the Strongarm briefing's Start button, then
verifies the original in-game identity/resources/roster. Restarting Strongarm has
also passed these live checks. If a screen transition or emulator error prevents
loading the expected state, startup stops instead of accepting another mission. The game is paused before these multi-read state checks.

If BottleShip reports a guest crash during startup, stop the dedicated runtime,
start it again with a fresh profile, and repeat the boot command. Startup does
not repair a crashed guest or alter mission state to pass its checks.
