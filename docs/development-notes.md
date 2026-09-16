# Lessons from developing the harness

These notes describe failed Strongarm attempts and the changes made before
attempt 10. They do not establish a combat victory or a measured improvement in
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
