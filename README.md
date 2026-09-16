# Jev plays StarCraft shareware

A TypeSafe System One harness for **Strongarm, the first combat mission in the
original StarCraft shareware campaign**, with a game recording and Jev's actual
action probabilities. Inspired by [TypeSafe's Doom demo](https://typesafe.ai/blog/introducing-system-one-models-and-jev).

**Combat-run verification is pending.** A published winning run must include the
original game's visible victory screen, matching engine outcome, decision log,
and recorded probability distributions. Run results and video links will be
added after those checks are complete.

The original 1998 Windows executable runs inside [BottleShip](https://github.com/jenissimo/bottleship).
The harness observes structured game state, asks `jev-latest` to choose a command,
and executes that choice through ordinary mouse and keyboard inputs. The game is
paused during state reads and inference. This independently implements the
structured-state decision pattern shown in the Doom demonstration; it does not
use TypeSafe's Doom harness code. Screenshots are recorded for viewers, while
the model receives structured observations. This is a bounded mission experiment,
not a benchmark of real-time competitive play.

## The mission

Strongarm's briefing orders the player to **destroy the rebel base** on Chau Sara.
The original 96×64-tile map contains enemy forces, combat encounters, and scripted
reinforcements. The player begins with eight Marines, four SCVs, two Barracks,
a Command Center, four Supply Depots, a Refinery, and an Engineering Bay.

Jev directs combat squads, exploration, mineral gathering, and reinforcement
production. Enemy locations enter its observations only when visible to the
player. The original executable runs the opposing forces and mission triggers.
Success requires its committed victory outcome **and a captured, visually checked
original victory dialog**. The recorder continues after the outcome changes so
the game's result screen can appear.

Boot Camp remains available as an optional economy/input-adapter test mission.
The default run target is Strongarm.

[Development notes](docs/development-notes.md) document failed attempts,
verified input defects, and the strategy changes being tested.

## Run it

Requires Python 3.11+, Git, Node/npm, Chrome with WebGPU, and FFmpeg. Setup installs
Bun locally if it is absent. Tested on macOS Apple Silicon.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
scripts/setup-runtime.sh
scripts/start-runtime.sh
```

Leave the runtime terminal open. In a second terminal:

```sh
source .venv/bin/activate
python -m tsai_sc.boot --mission strongarm

# Save your own key in a private file outside the repository; chmod 600 it.
# The file contains a single TYPESAFE_API_KEY=... assignment.
python -m tsai_sc.run \
  --env-file "$HOME/.config/tsai-sc/env" \
  --run-dir runs/my-attempt \
  --max-requests 400 --max-seconds 1200

python -m tsai_sc.render runs/my-attempt \
  --output recordings/strongarm.mp4 --speed 4 --fps 30
python -m tsai_sc.verify runs/my-attempt
```

Inspect `runs/my-attempt/victory-screen.png` for the original victory dialog
before publishing a winning-run claim. To restart a stopped controller's current
mission through the game menus, use `python -m tsai_sc.boot --restart`.

Alternatively, provide `TYPESAFE_API_KEY` in the environment. Requests go only to
TypeSafe's fixed HTTPS API endpoint. Request limits include retry attempts, and
there is no substitute policy or fabricated response if the API fails.

Setup downloads a pinned BottleShip revision and verifies the demo bundle's
SHA-256. It keeps the runtime, game data, logs, and an isolated browser profile
under ignored `.runtime/`. It does not attach to your personal browser. See
[runtime details](engine/README.md) for setup and the local bridge API.

## How decisions work

```text
Original game memory (read only)
          ↓
Own units + visible enemies/resources + economy + recent observed positions
          ↓
Bounded candidate commands and recent execution feedback
          ↓
TypeSafe Jev: category + per-category command distributions
          ↓
Deterministic selection / camera / mouse / hotkey adapter
          ↓
Original StarCraft simulation and mission triggers
```

Each combat request evaluates a small decision graph. An `intent` Choice selects
among the available **Economy, Engage, Explore, Reposition, and Continue**
categories. Independent `action_<category>` Choice questions choose concrete
commands within each category. The API evaluates those questions together from
the same observation; code routes the selected category to its selected command.
A category with one available command needs no second model question.

The visualizer shows the actual category distribution and the selected branch's
action distribution separately. It never multiplies them into a claimed API
probability. Other branch answers remain in the log. This avoids splitting one
category's support across many concrete options in a single flat comparison.
All questions in a request are independent: a judgment that depends on another
answer would instead require a later request, as described in the
[TypeSafe API documentation](https://docs.typesafe.ai/primitives#when-one-question-depends-on-another).

The prompt includes ordinary StarCraft guidance: establish mineral income,
produce affordable reinforcements while other orders continue, assemble roughly
eight to twelve Marines together before an unsupported push, rebuild after
losses, and explore for remaining enemies. A measured core of at least eight
combat units within 192 pixels of one member explicitly marks assembly complete;
the prompt distinguishes reinforcing small groups from withdrawing a formed
force, and allows pathfinding detours. These instructions
contain no enemy-base coordinates or predetermined mission route.

| Model choice | Candidate supplied by the harness | Original game controls |
| --- | --- | --- |
| Focus fire | A currently visible hostile unit or building | Select squad, `A`, click target |
| Attack toward enemies | A visible enemy group's position or a previously seen structure location | Select squad, `A`, click ground |
| Explore | A bounded north/east/south/west advance from the squad | Select squad, `A`, click ground |
| Retreat or regroup | A point away from a visible threat, an observed friendly base for assembling replacements, or the friendly force's center | Select squad, `M`, click ground |
| Gather minerals | An available SCV and an observed mineral field | Select SCV, right-click minerals |
| Train Marine or SCV | An idle compatible producer with sufficient minerals and supply | Select Barracks or Command Center, `M` or `S` |
| Build Supply Depot or Barracks | An available SCV and an open candidate site near the friendly base | Select SCV, `B`, then `S` or `B`, click placement |
| Continue current orders | Keep persistent orders in progress | No new input |

The deterministic adapter groups nearby selectable combat units into squads of
at most twelve, selects them with ordinary clicks, Shift-clicks, and a small
selection-box fallback for obscured units, pans the
camera, and requires the full surviving selectable squad to be selected before
issuing its order. Mouse clicks are queued against paused position snapshots;
each click's resulting selection is checked. It supplies up to eight
squads, four nearest visible focus targets per squad, known costs, prerequisites,
and tile-aligned construction candidates. Exploration geometry uses the squad's
observed position and map bounds; the game determines terrain passability.
Recent observed friendly positions help Jev track where it has already moved.
The harness also remembers up to thirty-two enemy structures actually seen in
earlier observations and offers up to four recent locations per squad as
attack-move destinations. Stale sightings are explicitly uncertain, and focus
fire still requires current visibility. Movement history distinguishes new
units from dead units whose slots the original game reuses.

Economy options cap the offered workforce at twelve SCVs and the Marine force
at seventy-two, with one queued unit per producer. Depots are offered near the
supply limit, with one unfinished depot at a time.
Barracks can be rebuilt or expanded to three, with one under construction at a
time, for the original cost of 150 minerals each. Workers inside refineries,
unfinished units, and workers already constructing cannot be retasked through
these options. This action-space design is part of the experiment: Jev selects
a category and its supplied command, and the adapter translates that selection into input.

The adapter records dispatch, actual selected units, and observed order acceptance
separately. Acceptance is evidence of an order in progress, not proof of arrival
or tactical success. Guest memory is read only; resources, units, damage, movement,
and victory remain under the original game's control. There is no substitute
policy that overrides Jev's selected command.

## Recording and evidence

Each attempt contains:

- `manifest.json`: runtime, model, limits, pacing, and initial game state.
- `decisions.jsonl`: actual requests, validated model responses, selected actions,
  normal input events, and observed command results.
- `trace.jsonl` and `frames/`: timestamped game-only frames and the last observed
  decision/state used by the visualizer.
- `victory-screen.png`: the original game's result presentation after the outcome
  transition, retained for visual inspection.
- `result.json`: original engine outcome, final state, counts, trace/decision
  hashes, and the retained result-screen image's path and hash.

Interrupted or failed runs instead receive `incomplete.json`.

The verifier checks the exact supported mission, committed outcome, trace and
decision integrity, agreement between selected commands and model choices, and
the ordinary-input allowlist. For Strongarm it also requires a result-screen
image confined to the run directory with a matching hash. It does **not** recognize
victory text in pixels; a person must inspect the original screen before the run
is presented as a visible victory.

The video shows **action probabilities**, not estimated chances of winning. It
labels playback speed and decision pauses. Model probabilities are preserved
unchanged. Frames contain only the 640×480 game canvas, with an independently
rendered dashboard. No desktop, login screen, microphone, or API key is recorded. The recorder rejects
a blank first gameplay frame and stops on sustained black captures, such as a
lost browser GPU device.

Observed API compatibility issue: some large Choice replies from `jev-1.13.0`
return every option but round values to whole percentage points totaling 99%.
The client permits only a 99% or 101% total when every value has that precision;
it preserves the returned values and the video labels the reported total.
Other malformed responses receive bounded retries and are never executed.

## Verify

```sh
python -m unittest discover -s tests -v
# Optional live protocol checks, with the runtime running:
TSAI_TEST_BRIDGE_URL=http://127.0.0.1:3917 \
  python -m unittest discover -s tests -p test_bridge_protocol.py -v

# Stage only intended source files, then check the exact staged content:
python scripts/audit_public.py
```

Tests cover malformed model responses, retry/request limits, credential handling,
original-game memory decoding, visibility and win detection, command prerequisites,
squad selection, modifier release, mission-specific camera geometry, placement
coordinates, evidence integrity, and real FFmpeg output timing/format. Runtime boot and
gameplay also require integration verification against the pinned demo executable.

## Credits and rights

This repository's harness code is MIT licensed. BottleShip is Apache-2.0 with its
own third-party notices. StarCraft and the shareware assets are Blizzard
Entertainment's property and are not included in this repository or relicensed.
See [references](docs/references.md) for the original Doom video and API docs.
