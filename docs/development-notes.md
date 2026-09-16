# Lessons from developing the harness

These notes describe failed Strongarm attempts through attempt 11 and subsequent
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
    The next full run tests both changes together. Neither aborted recording
    establishes a combat victory.
