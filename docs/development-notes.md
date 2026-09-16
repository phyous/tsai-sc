# Lessons from developing the harness

These notes describe development attempts through attempt 15 and subsequent
changes and probes. They do not establish a combat victory or a measured improvement in
win rate. Code checks, controlled game tests, and strategy hypotheses provide
different kinds of evidence.

1. **A flat command list can obscure the strategic choice.** Earlier requests
   compared every concrete command in one Choice question. Numerous exploration
   and squad variants could divide support differently from a few economy
   commands. That is a design concern, not a demonstrated cause of defeat.
   The current graph asks for an intent and independent recommendations within
   each available category in one API request. The selected intent determines
   which recommendation executes. Child questions do not see the intent answer;
   their probabilities are displayed separately, never multiplied into joint
   odds. A branch with one command has no invented second probability.
   See [graph routing](../tsai_sc/combat.py),
   [graph tests](../tests/test_combat.py), and
   [visualizer tests](../tests/test_render.py).

2. **Income and replacement production need explicit context.** Training SCVs
   does not assign idle workers to minerals. Existing mining and production can
   continue while the army receives another command. The prompt now states
   those mechanics, reports worker activity and production queues, and suggests
   assembling roughly eight to twelve Marines before an unsupported push.
   It distinguishes the largest selectable squad from a scattered total army.
   These are strategy hypotheses expressed as guidance: Jev still selects the
   command, and there is no prescribed route or hidden enemy location.
   See the [mission playbook and observations](../tsai_sc/combat.py).

3. **A missing action cannot be repaired by prompting.** The earlier menu could
   train Marines but could not replace destroyed Barracks. Losing production
   therefore removed a recovery path from the model's choices. The menu now
   offers ordinary SCV construction of a Barracks for 150 minerals, with at most
   three owned or underway and one unfinished at a time. Candidate sites use
   the building's footprint near an observed friendly base; the original game
   still decides whether placement is legal. This restores an available action,
   without proving it will be chosen effectively.
   See [placement and input translation](../tsai_sc/controller.py) and
   [economy candidate tests](../tests/test_combat.py).

4. **Successful dispatch was overstated.** An audit of attempt 09 found that
   **17 of 104 issued tactical commands selected only part of the requested
   squad; 16 of those 17 were marked accepted**. In every partial case, at least
   one omitted Marine was still alive, completed, and visible at the next
   observation. One six-Marine command reached only two Marines while the other
   four retained the previous destination. This was a verified actuator defect,
   not just weak strategy. It does not explain every loss: a fully selected
   six-Marine squad also died later. Order acceptance establishes neither
   arrival nor tactical success.

5. **Selection must be checked against the whole surviving squad.** The old
   adapter allowed a nonempty requested subset and could click positions that
   had become stale while units moved. The fix queues Shift and the selection
   click against a paused, freshly read position, checks selection after each
   click, skips already selected members, and refuses the command unless the
   final set matches all surviving selectable requested units. In controlled
   tests, a 100 ms mouse hold still missed a moving Marine; a 1 ms selection tap
   selected it. Subsequent live checks selected exactly eight idle Marines,
   eight moving Marines, and a four-Marine moving subset, with no retries.
   A later live test reproduced a stationary Marine hidden behind Barracks
   artwork: a point click missed it, but an 8×8-pixel Shift-drag recovered the
   missing member and yielded exactly the twelve requested units. The bounded
   second-pass selection-box fallback retains the full-set guard, including
   rejection of any extra selected unit.
   These checks validate those cases, not every possible overlap or battle.
   See [selection code](../tsai_sc/controller.py) and
   [selection regressions](../tests/test_controller.py).

6. **Unit pool IDs are reused.** Failed-run histories showed dead Marines' IDs
   later assigned to newly trained Marines. Matching history by ID alone could
   attribute an old movement order to a replacement. Original executable
   disassembly identified the five-bit generation counter used to validate unit
   handles. Observations now include it, and history matches both ID and
   generation. See the [decoder evidence comments](../tsai_sc/game.py),
   [decoder tests](../tests/test_game.py), and
   [history matching tests](../tests/test_combat.py).

7. **Observed buildings need explicit, uncertain memory.** Enemy structures
   seen earlier can leave current visibility without being destroyed. The
   harness retains up to thirty-two actual sightings and can offer attack-move
   toward their last observed positions. Stale sightings are labeled uncertain;
   focus fire still requires current visibility. Never-observed enemy positions
   are not supplied. Tests cover visibility filtering, recycled IDs, and memory
   bounds in [combat tests](../tests/test_combat.py). Whether this improves
   mission completion remains an experimental question.

8. **A running controller does not guarantee a usable recording.** A capture
   audit found all 1,190 saved frames from one attempt were entirely black,
   despite continuing model decisions. That attempt cannot serve as gameplay
   video evidence. After runtime capture repair, the recorder requires a valid
   640×480 PNG and a nonblack first frame before inference. It aborts if later
   captures stay entirely black for two seconds, allowing brief transitions.
   The verifier also rejects a black victory image; it does not recognize the
   victory text automatically. See the [recorder](../tsai_sc/run.py),
   [capture tests](../tests/test_run_capture.py), and
   [evidence verifier](../tsai_sc/verify.py).

9. **Assembly can become a repeated subgoal after it is already achieved.**
   Attempt 10 was stopped after 110 decisions and about 670 seconds, with 34
   living combat units at the last decision and no victory. Jev selected 67
   regroup commands and 10 exploration commands. At call 46, twelve Marines
   already formed a compact group with a maximum radius of 58 pixels, yet Jev
   chose to send them back to the Command Center, 520 pixels from their center.
   Earlier regroup choices moved coherent squads only 21–40 pixels toward the
   global army center. Total army size and maximum spread did not clearly tell
   the model that assembly was complete. The adapter refused 31 commands when
   it could not select the exact requested surviving squad; **zero issued
   tactical commands selected only part of that squad**. Refused commands and
   unproductive model choices are separate failure modes.

   The revised observation measures each selectable squad's largest core
   within 192 pixels of an actual member, reports its exact members and Marine
   count, and identifies cores of at least eight combat units. This is a
   formation heuristic, not a guarantee of combat strength or a passable route.
   Labels include current squad size because names can change as reinforcements
   arrive. Returning an already formed force to base is described as a
   withdrawal. The guidance allows that force to advance while income,
   replacements, and smaller reinforcement groups continue independently.
   Every retreat, regroup, exploration, and other existing command remains
   available; there is no forced action or probability override.

   Three real API probes reused frozen states from attempt 10 with the revised
   observations and guidance. No probe issued game input:

   | Source call | Original choice | Probe choice | Explore probability | Reposition probability |
   | --- | --- | --- | --- | --- |
   | 46 | Alpha: regroup at friendly base | Alpha: scout east | 0.70 | 0.00 |
   | 71 | Alpha: regroup with friendly force | Bravo: scout east | 0.74 | 0.03 |
   | 110 | Alpha: regroup at friendly base | Charlie: scout east | 0.67 | 0.01 |

   These are separate intent-node probabilities, not joint command odds or win
   predictions. The probes show a targeted choice change in three recorded
   states; they do not demonstrate sustained play, victory, or an improved win
   rate. See [formation and prompt code](../tsai_sc/combat.py) and
   [formed/scattered squad tests](../tests/test_combat.py).

10. **A pathfinding detour can initially look like movement away from the goal.**
    At attempt 10 call 77, twelve exactly selected Marines received an eastward
    attack-move toward (1388, 497). Tracking those same IDs and generations,
    their center moved from (1004, 497) at frame 7483 to (843, 474) at frame
    7629, then (767, 473) at frame 7701, before reaching (1315, 465) at frame
    8288. They first traveled west while retaining their attack-move orders.
    Increasing straight-line distance or temporary formation spread therefore
    cannot alone establish blockage. Guidance now explicitly allows detours;
    the current-order summary distinguishes attack-move, guard, and proximity
    to a past destination without claiming measured motion or arrival.

    Engine order 49 is Follow, a unit-target movement order. Some successful
    regroup commands clicked a friendly unit or the Command Center and
    produced Follow rather than Move. The earlier summary called this an
    unknown order and the verifier rejected it. Follow is now described
    explicitly, including that it may remain active near its target; the order
    itself is not proof of physical movement. See the
    [BWAPI order enumeration](https://github.com/bwapi/bwapi/blob/main/bwapi/include/BWAPI/Order.h)
    and [OpenBW's Follow implementation](https://github.com/OpenBW/openbw/blob/master/bwgame.h).

11. **The capture guard caught a recurring GPU failure.** Attempt 11 aborted
    after 30 model decisions when the recorded game canvas became persistently
    black. The original game continued running, but its Chrome GPU process had
    exited. The capture guard correctly stopped the unusable recording rather
    than allowing further inference without gameplay evidence.

    macOS kernel logs confirmed that both the attempt 07 and attempt 11 GPU
    exits were memory-pressure kills following a “no paging space” event.
    The browser parent and game renderer survived; runtime launch scripts and
    RPC timeouts did not cause these process exits. Source review then found
    a concrete allocation leak: palette presentation allocated four temporary
    buffers, totaling 1,537,080 bytes per 640×480 conversion, but pure DirectDraw
    frames could skip their cleanup. The [runtime patch](../engine/patches/)
    drains those buffers after all frame encoders have been submitted. Its
    three lifetime tests pass; this does not prove there are no other leaks.

    Recording now copies the original CPU pixels and attached palette instead
    of reading back the GPU canvas. It recovered complete game frames after
    the GPU failure; static terrain and HUD regions matched the last valid GPU
    screenshot pixel-for-pixel. Four successive live captures changed as the
    game advanced, taking 23–24 ms each, and five capture tests validate PNG
    integrity, colors, stride, source selection, and bounded input handling.
    During attempt 12, a 180-second active rendering soak produced 26,956
    additional 8-bit frame presentations. All four samples reported zero
    pending conversion buffers. The same GPU process remained alive, with
    resident memory increasing by 592 KiB (about 0.6 MiB) between the first and
    last samples. Resident memory is not total GPU allocation or compressed
    memory. This bounded test supports the repair; it does not establish
    indefinite stability or a combat victory.

12. **A formed army still needs to assess the defenders.** Attempt 12's first
    twelve-Marine assault lost its entire cohort between calls 68 and 71,
    spanning 244 game frames. Selection was complete: the first command focused
    the Academy, followed by an advance toward the Command Center. The Academy
    lost 55 HP while all eleven initially visible armed defenders remained
    observable; only one of those defenders had lost HP. This establishes a
    costly assault, but does not isolate target priority, approach geometry,
    or Firebat splash as the sole cause.

    Observations now distinguish armed defenders, attack-capable workers, and
    unarmed structures, and summarize local composition, HP, and known base
    ranges. Guidance accounts for Firebat splash and the difference between
    assembly and sufficient strength. [Battle memory](../tsai_sc/battle.py)
    preserves up to four recent encounters from exact ID/generation snapshots.
    It reports owned identities no longer observed near previously visible
    hostiles, without asserting why they disappeared. Defender composition
    comes from one actual sighting, not accumulated counts presented as a
    simultaneous army; current enemy positions and survival remain uncertain.
    See [battle-memory tests](../tests/test_battle.py) and
    [combat observation tests](../tests/test_combat.py).

    Separate API probes on frozen attempt 12 states changed call 68's Academy
    focus to a Marine focus, call 69's Command Center advance to retreat, and
    call 90's Barracks construction to regrouping. These probes issued no game
    input. They demonstrate different choices with revised observations and
    guidance, not successful execution, isolated causality, or improved win rate.

13. **Input semantics and recovery options need live checks.** Attack-move and
    exploration now use `A` plus a minimap destination. A viewport click can hit
    a building sprite instead of the intended ground. A separate live probe
    aimed at the friendly Command Center produced original attack-move order
    14 with no unit-target pointer, confirming ground-order semantics. Focus
    fire retains a visible target click.

    Economy actors now use the same paused 1 ms selection tap as combat units,
    followed by an exact-actor check before any hotkey. Construction search
    covers bounded rings and intermediate directions around the observed base;
    recently rejected sites receive a temporary cooldown. A live probe started
    a Barracks at an available northern site. This proves that placement, not
    that every candidate has legal terrain.

    The menu also offers infantry weapons level 1 at an Engineering Bay for
    100 minerals and 100 gas, at most once after an accepted order per run.
    A live probe observed the 100-gas deduction and original upgrade order 76.
    That verifies research initiation, not completed research. These isolated
    input checks are separate from Jev's recorded gameplay and establish no
    combat victory. See [input translation](../tsai_sc/controller.py) and
    [control regressions](../tests/test_controller.py).

14. **Exploration needs lasting spatial feedback.** Attempt 13 established
    mining, started the weapons upgrade, grew its army, and damaged armed
    defenders in small skirmishes. It did not find the rebel base before the
    development run was stopped. Through call 111, sampled owned combat
    positions occupied 44 of 96 cells on a 256-pixel grid. Calls 99–111 crossed
    already sampled cells without adding another cell. Units were moving;
    this was not evidence of stationary pathfinding failure. Revisiting ground
    can also be necessary transit.

    The previous observation retained only 64 history events and 24 coarse
    squad-center bins. Persistent visitation now uses owned combat snapshots
    throughout the run, with separate prior visits and current presence.
    Scout choices describe their endpoint's recorded visitation without
    removing any destination. These are sampled positions, not fog coverage,
    passability, cleared territory, or evidence of enemy absence. No route or
    unseen enemy coordinates are supplied.

    Frozen-state probes initially still preferred some revisits. Describing the
    endpoint first and explicitly prioritizing expanded sampled coverage over
    repeated traversal changed saved calls 110 and 123 to previously unsampled
    destination cells. These API probes issued no game inputs and do not
    establish a successful mission or improved win rate.

15. **Reaction time includes input execution.** In attempt 13, calls 80 and 82
    spent 75 and 64 original game frames selecting units before their focus
    targets were no longer visible. The fixed three-second wait then added
    another 55–56 frames. Call 82 lost two completed Marines during input and
    another during the following wait. An optional shorter observation interval
    during visible nearby combat reduces the waiting component; it does not
    choose commands or establish that those losses would have been prevented.
    Selection overhead remains a separate component of reaction time.

    Before attempt 15, selection acknowledgment changed from a fixed wait to
    a 20 ms running interval followed, if needed, by 40 ms retries, capped at
    220 ms per click. Capture and selection readback occur paused. Separate
    live checks retained exact selections while reducing eight-Marine selection
    from 55 to 27 game frames and twelve-Marine selection from 86 to 40; every
    click was acknowledged on its first poll. These measured cases do not
    establish latency under all battle conditions.

    Focus fire now arms `A` before its final target snapshot, rechecks visible
    hostile identity and generation, and queues a 1 ms click while paused.
    If the target is unavailable or outside the viewport, Escape cancels only
    that armed attack cursor. Regression tests cover this sequence; there was
    no visible enemy available for the separate live focus check. No claim of
    improved combat accuracy follows from the selection timing tests alone.
    See [input code](../tsai_sc/controller.py) and
    [controller tests](../tests/test_controller.py).

    A control-group experiment did not yield a working recall through this
    runtime: after Ctrl+digit assignment and selecting a Command Center,
    pressing the digit left the Command Center selected. A left-Ctrl variant
    also failed. Exact-selection checks rejected the result, but fallback
    selection added overhead (59 frames versus 53 in the measured case).
    The unproven cache was removed. Its failure does not establish whether the
    underlying cause is in the runtime or original demo's input handling.

16. **Obscured workers and pending construction need explicit handling.** Calls
    144–152 repeatedly tried to select SCV 1676 behind a Supply Depot. The
    exact-actor guard rejected the Depot each time, so no gather order reached
    the wrong unit. Earlier, separate workers received duplicate construction
    tasks while their predecessors were still approaching the site with build
    order 30. Counting only existing unfinished structures missed that period.
    Pending construction must require an active build order and matching
    queued building: failed builders can retain a stale queue while mining.

    A separate live recovery probe reproduced the obscured worker: the point
    click selected Depot 1589, then one ordinary 8×8-pixel selection drag selected
    exactly SCV 1676. A mineral right-click produced gather order 85 targeting
    mineral 1681. The fallback retains the exact-actor guard. Pending-build
    tests distinguish active order 30/33 from stale queues on mining workers.

17. **Broader exploration did not ensure mission completion or sustained production.**
    Attempt 14 was stopped without victory after 263 recorded decisions; it
    did not reach an original-engine defeat result either. Sampled owned Marine
    positions covered 74 of 96 cells on the 256-pixel grid, which is not fog
    coverage. The first Barracks disappeared by call 61 after visible damage.
    No construction or upgrade command was selected during the attempt,
    although construction candidates remained available. Its final recorded
    observation still had 1,918 minerals, 200 gas, ten mineral workers, fifteen
    completed Marines, and one surviving Barracks producing a Marine.

    This motivates an opt-in scheduling experiment rather than a claim that
    economy neglect alone caused the incomplete mission. `--separate-economy`
    alternates separate Economy and Army model calls, each with fresh observed
    state, filtered legal candidates, and a genuine Continue option. Economy
    handles workers, production, buildings, supply, and research; Army handles
    military orders. Only the selected command from each response executes.
    No additional observation wait follows Economy; the normal or combat wait
    follows Army. Inputs themselves still advance the game. A lane offering
    only Continue is skipped without fabricating a model response.

    Each actual call retains its original single-command graph and evidence
    record. The visualizer identifies the lane without changing probabilities.
    Regular economy opportunities do not force spending or guarantee a win.
    See [lane candidates](../tsai_sc/combat.py), [runner](../tsai_sc/run.py),
    [lane tests](../tests/test_combat.py), and
    [overlay tests](../tests/test_render.py).

18. **An original fatal dialog can be invisible to game-only capture.** Attempt
    15 stopped progressing at original game frame 1818 after 38 recorded model
    decisions. The final observation still contained all eight starting Marines,
    four additional completed Marines, and nine kill credits on those units.
    All twelve recorded Economy decisions had accepted orders. These are early
    observations, not evidence of a completed mission or a controlled comparison.

    Runtime inspection found the main guest thread awaiting `MessageBoxA`:
    the original game reported a critical `_CTRLNODE` error, code `0x8`, and
    instructed the user to terminate. This host-rendered dialog sits outside
    the captured DirectDraw pixels. The CPU and a background thread continued,
    while the main thread waited for the dialog; no victory or defeat outcome
    was recorded. Heap diagnostics showed about 54 MB allocated and substantial
    unused space, so total heap exhaustion is not established. The cause of the
    original allocation error remains unresolved; a fresh boot is isolation,
    not proof of a repair.

    The runner now stops after sixty accumulated seconds of resumed execution
    without game-clock progress. An observed original mission pause is excluded,
    and progress resets the counter. This bounds an otherwise indefinite wait
    and directs inspection toward a blocking dialog or runtime failure; it does
    not dismiss dialogs or synthesize an outcome. See the
    [runner guard](../tsai_sc/run.py).
