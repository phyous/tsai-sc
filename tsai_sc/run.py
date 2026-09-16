"""Play and record an original mission using genuine Jev decisions and game input.

Optional combat pacing shortens only the wait between observations. The game
still advances while ordinary selection and command inputs execute; a shorter
wait cannot remove that exposure or guarantee that a model reacts effectively.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time

from .controller import InputAdapter, candidates, request_for, verify_command
from . import battle, combat
from .engine import BottleShipBridge
from .game import GameStateError, read_state
from .typesafe import TypeSafeClient, TypeSafeError


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


def visible_structure_sightings(state):
    """Remember only structure positions the player actually saw this frame."""
    return [
        {key: unit[key] for key in ('id', 'type', 'type_id', 'x', 'y', 'generation') if key in unit} | {'last_seen_frame': state['frame']}
        for unit in state['units']
        if unit.get('visible') is True and unit.get('relationship') == 'enemy'
        and 106 <= unit['type_id'] <= 173
    ]


def validate_pacing(decision_seconds, combat_decision_seconds):
    def valid(value):
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and .2 <= value <= 10)
    if not valid(decision_seconds) or (combat_decision_seconds is not None
            and (not valid(combat_decision_seconds) or combat_decision_seconds > decision_seconds)):
        raise ValueError('Decision intervals must be 0.2–10 seconds; combat interval cannot exceed the baseline')


def visible_combat_contact(state):
    """Use current visible hostiles and living owned military, never fog memory."""
    if not state or state.get('mission_kind') != 'combat':
        return False
    player = state.get('player_id')
    enemies = state.get('enemy_players')
    allies = state.get('allied_players', [])
    units = state.get('units', [])
    own = [unit for unit in units if unit.get('owner') == player
           and unit.get('type_id') in combat.STRONGARM.combat_types
           and unit.get('completed') is True and unit.get('hp', 0) > 0]
    hostile = [unit for unit in units if unit.get('visible') is True
               and unit.get('hp', 0) > 0 and unit.get('owner') != player
               and unit.get('owner') not in allies
               and (unit.get('owner') in enemies if enemies is not None
                    else unit.get('relationship') == 'enemy')]
    return any((enemy['x'] - actor['x']) ** 2 + (enemy['y'] - actor['y']) ** 2 <= 512 ** 2
               for enemy in hostile for actor in own)


def observation_pacing(state, after=None, *, decision_seconds, combat_decision_seconds=None):
    """One fast interval after contact observed before OR after this command."""
    validate_pacing(decision_seconds, combat_decision_seconds)
    before_contact = visible_combat_contact(state)
    after_contact = visible_combat_contact(after)
    fast = combat_decision_seconds is not None and (before_contact or after_contact)
    return {'seconds': combat_decision_seconds if fast else decision_seconds,
            'reason': 'visible_hostile_within_512px_before_or_after_command' if fast else 'baseline',
            'contact_before_command': before_contact, 'contact_after_command': after_contact,
            'before_frame': state.get('frame') if state else None,
            'after_frame': after.get('frame') if after else None}


def lane_pacing(pacing, lane):
    if lane == 'economy':
        return {**pacing, 'seconds': 0, 'reason': 'army_decision_follows_without_an_extra_wait',
                'scheduled_interval_without_lane_override': pacing['seconds']}
    return pacing


class Recorder:
    def __init__(self, bridge, directory, fps=8):
        self.bridge = bridge
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.trace = (self.directory / 'trace.jsonl').open('w')
        self.start = time.monotonic()
        self.last = -1.0
        self.interval = 1 / fps
        self.index = 0
        self.state = {}
        self.decision = None
        self.action = {'label': 'Observing original StarCraft mission'}
        self.blank_since = None
        self.pacing = None

    def _check_capture(self, path, now):
        """Stop unusable gameplay recordings while allowing brief transitions."""
        from PIL import Image
        try:
            with Image.open(path) as captured:
                if captured.format != 'PNG' or captured.size != (640, 480):
                    raise RuntimeError('Recording requires an original 640x480 PNG game canvas')
                blank = captured.convert('RGB').getbbox() is None
        except (OSError, Image.DecompressionBombError):
            raise RuntimeError('Unable to decode the recorded game canvas') from None
        if not blank:
            self.blank_since = None
            return
        if self.index == 0:
            raise RuntimeError('The first gameplay capture is blank; repair capture before making model requests')
        if self.blank_since is None:
            self.blank_since = now
        elif now - self.blank_since >= 2:
            raise RuntimeError('Gameplay captures stayed blank for two seconds; recording stopped')

    def frame(self, force=False, status='running'):
        now = time.monotonic() - self.start
        if not force and now - self.last < self.interval:
            return
        filename = f'frames/{self.index:06d}.png'
        self.bridge.capture(self.directory / filename)
        self._check_capture(self.directory / filename, now)
        public_state = {k: self.state[k] for k in ('mission', 'mission_kind', 'objective_summary', 'combat', 'frame', 'minerals', 'gas', 'supply', 'objective_progress') if k in self.state}
        row = {'t': now, 'frame': filename, 'state': public_state, 'decision': self.decision,
               'action': self.action, 'status': status}
        if self.pacing is not None:
            row['pacing'] = self.pacing
        self.trace.write(json.dumps(row, separators=(',', ':')) + '\n')
        self.trace.flush()
        self.last, self.index = now, self.index + 1

    def close(self):
        self.trace.close()


def run(directory, *, env_file=None, max_requests=400, max_seconds=1200, decision_seconds=2,
        combat_decision_seconds=None, capture_fps=8, separate_economy=False):
    validate_pacing(decision_seconds, combat_decision_seconds)
    bridge = BottleShipBridge()
    bridge.pause()
    initial = read_state(bridge.read_memory)
    if initial['status'] != 'running':
        raise RuntimeError('Start a fresh running original mission before recording')
    client = TypeSafeClient(env_file=env_file, max_requests=max_requests)
    recorder = Recorder(bridge, directory, capture_fps)
    recorder.state = initial
    adapter = InputAdapter(bridge, recorder.frame)
    history = []
    start = time.monotonic()
    model_calls = 0
    final = None
    last_observed_frame = None
    stalled_running_seconds = 0.0
    next_lane = 'economy'
    unchanged_frame_expected = False
    write_json(recorder.directory / 'manifest.json', {
        'started_utc': datetime.now(timezone.utc).isoformat(), 'mission': initial['mission'],
        'game': 'Original StarCraft Shareware(ED) v4.00 executable on BottleShip',
        'model_requested': 'jev-latest', 'initial_state': initial,
        'pacing': 'Game paused for consistent memory snapshots and model inference; ordinary game input between snapshots.',
        'observation': 'Read-only own/visible unit state, resources and original mission outcome',
        'capture': 'Original 640x480 DirectDraw CPU pixels and attached palette; PNG encoding without GPU readback',
        'controller': 'Jev selects a command category and its action in parallel Choice questions; deterministic graph routing and mouse/keyboard adapter',
        'max_requests': max_requests, 'max_seconds': max_seconds, 'capture_fps': capture_fps,
        'decision_seconds': decision_seconds,
        'combat_decision_seconds': combat_decision_seconds,
        'separate_economy': separate_economy,
        'decision_schedule': ('Alternate Economy and Army decision opportunities, each based on a fresh paused observation and each permitting no command. Each opportunity with multiple candidates uses a separate API request; a sole Continue candidate needs no request. No additional wait after Economy; the configured interval follows Army.'
                              if separate_economy else 'One joint command graph per observation.'),
        'combat_pacing_rule': 'When enabled, use the combat interval for one wait after a visible hostile was within 512px of a living completed owned combat unit in the pre-command or post-command observation; otherwise use the baseline. Selection time is additional.',
        'source_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted(Path(__file__).parent.glob('*.py'))},
        'runtime_source_sha256': {
            path.relative_to(Path(__file__).parent.parent).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for pattern in ('engine/*.ts', 'engine/patches/*.patch', 'scripts/*-runtime.sh')
            for path in sorted(Path(__file__).parent.parent.glob(pattern))
        },
    })
    try:
        recorder.frame(force=True)
        with (recorder.directory / 'decisions.jsonl').open('w') as decisions:
            while time.monotonic() - start < max_seconds:
                bridge.pause()
                state = read_state(bridge.read_memory)
                observed_t = time.monotonic() - start
                recorder.state = state
                recorder.pacing = None
                if state['status'] != 'running':
                    final = state
                    recorder.action = {'label': 'Original game engine reports ' + state['status'] + '; waiting for its result screen'}
                    # The original executable sets its outcome before it paints
                    # the Victory dialog. Record that authentic transition too.
                    bridge.resume()
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        recorder.frame()
                        time.sleep(.04)
                    bridge.pause()
                    bridge.capture(recorder.directory / 'victory-screen.png')
                    recorder.action = {'label': 'Original game engine reports ' + state['status']}
                    recorder.frame(force=True, status=state['status'])
                    break
                stopped_clock = state['frame'] == last_observed_frame and not unchanged_frame_expected
                unchanged_frame_expected = False
                if state['frame'] != last_observed_frame or state.get('game_paused'):
                    stalled_running_seconds = 0.0
                last_observed_frame = state['frame']
                if state.get('game_paused') or stopped_clock:
                    # Original campaign transmissions can pause simulation and
                    # force the camera. Wait for gameplay to resume, recording
                    # the original presentation without issuing tactical input.
                    if stalled_running_seconds >= 60:
                        raise RuntimeError('Original game clock remained stopped after 60 seconds of resumed execution; inspect the runtime for a blocking dialog or failure')
                    recorder.action = {'label': ('Original game pause — waiting for mission transmission to finish'
                                                if state.get('game_paused') else
                                                'Game clock stopped — waiting for simulation progress')}
                    bridge.resume()
                    wait_started = time.monotonic()
                    deadline = wait_started + decision_seconds
                    while time.monotonic() < deadline:
                        recorder.frame()
                        time.sleep(.04)
                    bridge.pause()
                    if not state.get('game_paused'):
                        stalled_running_seconds += time.monotonic() - wait_started
                    continue
                is_combat = state.get('mission_kind') == 'combat'
                lane = next_lane if is_combat and separate_economy else None
                pacing = observation_pacing(state, decision_seconds=decision_seconds,
                                            combat_decision_seconds=combat_decision_seconds)
                pacing = lane_pacing(pacing, lane)
                actions = (combat.lane_candidates(state, lane, mission=combat.STRONGARM, history=history) if lane else
                           combat.candidates(state, combat.STRONGARM, history) if is_combat else candidates(state))
                if len(actions) > 1:
                    routing = None
                    if lane:
                        model_state, questions, routing = combat.lane_graph_request_for(state, actions, lane, history, combat.STRONGARM)
                    elif is_combat:
                        model_state, questions, routing = combat.graph_request_for(state, actions, history, combat.STRONGARM)
                    else:
                        model_state, questions = request_for(state, actions, history)
                    write_json(recorder.directory / 'pending-request.json', {'state': model_state, 'questions': questions})
                    request_started_t = time.monotonic() - start
                    response = client.evaluate(model_state, questions)
                    response_received_t = time.monotonic() - start
                    response['metadata']['observed_frame'] = state['frame']
                    if lane:
                        response['metadata']['control_lane'] = lane
                    choice, child_question = (combat.resolve_graph_choice(response, routing) if routing else
                                              (response['answers']['action']['choice'], 'action'))
                    if routing:
                        response['metadata']['decision_graph'] = {
                            'intent_question': routing['root_question'],
                            'selected_intent': response['answers'][routing['root_question']]['choice'],
                            'selected_action_question': child_question,
                            'selected_candidate': choice,
                            'semantics': 'Independent questions evaluated in one request; selected branch routes to its action. Values are not multiplied.',
                        }
                    selected = actions[choice]
                    recorder.decision, recorder.action = response, selected
                    model_calls += 1
                    recorder.frame(force=True)
                    command_started_t = time.monotonic() - start
                    issued = adapter.execute(state, selected)
                    command_finished_t = time.monotonic() - start
                    after = read_state(bridge.read_memory)
                    after_observed_t = time.monotonic() - start
                    pacing = observation_pacing(state, after, decision_seconds=decision_seconds,
                                                combat_decision_seconds=combat_decision_seconds)
                    pacing = lane_pacing(pacing, lane)
                    executed = ({**selected, 'units': issued['selected_units']} if 'selected_units' in issued else selected)
                    verified = (verify_command(state, after, executed) if issued.get('issued') else
                                {'accepted': False, 'evidence': issued.get('reason', 'Input was not issued')})
                    if issued.get('issued') and selected['kind'] == 'build' and not verified['accepted']:
                        # Clear only a failed placement cursor, never an active
                        # training queue or construction order before selection.
                        bridge.resume()
                        issued['inputs'].append({'command': 'keyHold', 'args': ['escape', 60]})
                        bridge.rpc('keyHold', 'escape', 60)
                        time.sleep(.15)
                        bridge.pause()
                    recorder.state = after
                    recorder.action = {**selected, **verified}
                    event = {'t': time.monotonic() - start, 'state': state, 'request': {'state': model_state, 'questions': questions},
                             'response': response, 'selected': choice, 'candidates': actions, 'action': selected,
                             'input_result': issued, 'command_verification': verified,
                             'pacing': pacing,
                             'timing': {'observed_t': observed_t, 'request_started_t': request_started_t,
                                        'response_received_t': response_received_t,
                                        'command_started_t': command_started_t, 'command_finished_t': command_finished_t,
                                        'after_observed_t': after_observed_t},
                             'after': {'frame': after['frame'], 'minerals': after['minerals'], 'gas': after['gas'],
                                       'workers': [u for u in after['units'] if u['type_id'] == 7 and u['owner'] == after['player_id']]}}
                    if routing:
                        event['routing'] = routing
                    if lane:
                        event['control_lane'] = lane
                    decisions.write(json.dumps(event, separators=(',', ':')) + '\n')
                    decisions.flush()
                    history.append({'command': selected['label'], 'kind': selected['kind'], 'squad': selected.get('squad'), 'point': selected.get('point'),
                                    'building': selected.get('building'), 'upgrade': selected.get('upgrade'),
                                    'issued': issued.get('issued') is True,
                                    'combat_snapshot': battle.history_snapshot(state, combat.STRONGARM.combat_types) if is_combat else {},
                                    'units': executed.get('units', [executed['unit']] if 'unit' in executed else []),
                                    'unit_generations': {str(unit['id']): unit['generation'] for unit in state['units']
                                                         if unit['id'] in executed.get('units', []) and 'generation' in unit},
                                    'squad_centers': [{'name': squad['name'], **squad['center']} for squad in model_state.get('squads', [])],
                                    'observed_enemy_structures': visible_structure_sightings(state),
                                    'frame': state['frame'], 'accepted': verified['accepted'], 'minerals_after': after['minerals'], 'gas_after': after['gas']})
                    if routing:
                        intent = response['answers'][routing['root_question']]
                        child = response['answers'].get(child_question) if child_question else None
                        probability = f'{child["probabilities"][choice]:.3f}' if child else 'single available action'
                        probability_text = f'intent={intent["choice"]} intent_probability={intent["probabilities"][intent["choice"]]:.3f} branch_probability={probability}'
                    else:
                        probability_text = f'probability={response["answers"]["action"]["probabilities"][choice]:.3f}'
                    print(f'call={model_calls} frame={state["frame"]} lane={lane or "joint"} minerals={state["minerals"]} gas={state["gas"]} choice={choice} {probability_text}', flush=True)
                else:
                    recorder.action = {'label': 'Current orders continue; no new command available'}
                if pacing['seconds'] > 0:
                    bridge.resume()
                wait_started = time.monotonic()
                deadline = wait_started + pacing['seconds']
                recorder.pacing = {**pacing, 'phase': 'between_decisions',
                                   'wait_started_t': wait_started - start,
                                   'wait_scheduled_until_t': deadline - start}
                while time.monotonic() < deadline:
                    recorder.frame()
                    time.sleep(.04)
                bridge.pause()
                if lane:
                    next_lane = 'army' if lane == 'economy' else 'economy'
                    # A no-op Economy decision can legitimately leave the same
                    # original frame for Army; this is not a mission pause.
                    unchanged_frame_expected = lane == 'economy'
        if final is None:
            raise RuntimeError('Run reached its time limit without an engine-confirmed victory')
        write_json(recorder.directory / 'result.json', {
            'status': final['status'], 'engine_evidence': final['evidence'],
            'final_state': final, 'elapsed_seconds': time.monotonic() - start,
            'model_calls': model_calls, 'captured_frames': recorder.index,
            'api_attempts': client.request_count,
            'accounted_input_tokens': client.input_tokens_total,
            'trace_sha256': hashlib.sha256((recorder.directory / 'trace.jsonl').read_bytes()).hexdigest(),
            'decisions_sha256': hashlib.sha256((recorder.directory / 'decisions.jsonl').read_bytes()).hexdigest(),
            'visible_victory_frame': 'victory-screen.png',
            'visible_victory_frame_sha256': hashlib.sha256((recorder.directory / 'victory-screen.png').read_bytes()).hexdigest(),
            'visual_verification': 'Captured original game result screen; inspect the PNG before publishing a victory claim.',
        })
        print(f'Original engine outcome: {final["status"]}; {model_calls} Jev decisions.', flush=True)
        return final['status']
    except BaseException as error:
        # These are local/sanitized errors; no headers, bearer keys or raw model
        # request exceptions are serialized.
        reason = ('Run interrupted' if isinstance(error, KeyboardInterrupt) else str(error)
                  if isinstance(error, (TypeSafeError, GameStateError, RuntimeError))
                  else 'Run stopped after a local ' + type(error).__name__)
        write_json(recorder.directory / 'incomplete.json', {'status': 'incomplete', 'reason': reason, 'model_calls': model_calls,
                                                          'api_validation': getattr(error, 'diagnostics', {})})
        raise
    finally:
        try:
            bridge.pause()
        finally:
            recorder.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--env-file')
    parser.add_argument('--max-requests', type=int, default=400)
    parser.add_argument('--max-seconds', type=float, default=1200)
    parser.add_argument('--decision-seconds', type=float, default=2)
    parser.add_argument('--combat-decision-seconds', type=float, default=None,
                        help='Optional shorter wait after observed nearby combat (0.2–10 seconds, at most --decision-seconds)')
    parser.add_argument('--capture-fps', type=int, default=8)
    parser.add_argument('--separate-economy', action='store_true',
                        help='Alternate separate model decisions for economy and army in combat missions')
    args = parser.parse_args()
    if not (1 <= args.max_requests <= 2000 and 1 <= args.max_seconds <= 3600 and .2 <= args.decision_seconds <= 10 and 1 <= args.capture_fps <= 30):
        parser.error('Requested limits are outside the supported range')
    try:
        validate_pacing(args.decision_seconds, args.combat_decision_seconds)
    except ValueError as error:
        parser.error(str(error))
    result = run(args.run_dir, env_file=args.env_file, max_requests=args.max_requests, max_seconds=args.max_seconds,
                 decision_seconds=args.decision_seconds, combat_decision_seconds=args.combat_decision_seconds,
                 capture_fps=args.capture_fps, separate_economy=args.separate_economy)
    raise SystemExit(0 if result == 'victory' else 2)


if __name__ == '__main__':
    main()
