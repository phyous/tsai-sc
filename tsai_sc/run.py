"""Play and record an original mission using genuine Jev decisions and game input."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from .controller import InputAdapter, candidates, request_for, verify_command
from . import combat
from .engine import BottleShipBridge
from .game import GameStateError, read_state
from .typesafe import TypeSafeClient, TypeSafeError


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + '\n')


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

    def frame(self, force=False, status='running'):
        now = time.monotonic() - self.start
        if not force and now - self.last < self.interval:
            return
        filename = f'frames/{self.index:06d}.png'
        self.bridge.capture(self.directory / filename)
        public_state = {k: self.state[k] for k in ('mission', 'mission_kind', 'objective_summary', 'combat', 'frame', 'minerals', 'gas', 'supply', 'objective_progress') if k in self.state}
        row = {'t': now, 'frame': filename, 'state': public_state, 'decision': self.decision,
               'action': self.action, 'status': status}
        self.trace.write(json.dumps(row, separators=(',', ':')) + '\n')
        self.trace.flush()
        self.last, self.index = now, self.index + 1

    def close(self):
        self.trace.close()


def run(directory, *, env_file=None, max_requests=400, max_seconds=1200, decision_seconds=2, capture_fps=8):
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
    write_json(recorder.directory / 'manifest.json', {
        'started_utc': datetime.now(timezone.utc).isoformat(), 'mission': initial['mission'],
        'game': 'Original StarCraft Shareware(ED) v4.00 executable on BottleShip',
        'model_requested': 'jev-latest', 'initial_state': initial,
        'pacing': 'Game paused for consistent memory snapshots and model inference; ordinary game input between snapshots.',
        'observation': 'Read-only own/visible unit state, resources and original mission outcome',
        'controller': 'Jev selects one candidate; deterministic worker/target/placement/input adapter',
        'max_requests': max_requests, 'max_seconds': max_seconds, 'capture_fps': capture_fps,
        'decision_seconds': decision_seconds,
        'source_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in sorted(Path(__file__).parent.glob('*.py'))},
    })
    try:
        recorder.frame(force=True)
        with (recorder.directory / 'decisions.jsonl').open('w') as decisions:
            while time.monotonic() - start < max_seconds:
                bridge.pause()
                state = read_state(bridge.read_memory)
                recorder.state = state
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
                stopped_clock = state['frame'] == last_observed_frame
                last_observed_frame = state['frame']
                if state.get('game_paused') or stopped_clock:
                    # Original campaign transmissions can pause simulation and
                    # force the camera. Wait for gameplay to resume, recording
                    # the original presentation without issuing tactical input.
                    recorder.action = {'label': 'Original game pause — waiting for mission transmission to finish'}
                    bridge.resume()
                    deadline = time.monotonic() + decision_seconds
                    while time.monotonic() < deadline:
                        recorder.frame()
                        time.sleep(.04)
                    bridge.pause()
                    continue
                is_combat = state.get('mission_kind') == 'combat'
                actions = combat.candidates(state, combat.STRONGARM, history) if is_combat else candidates(state)
                if len(actions) > 1:
                    model_state, questions = (combat.request_for(state, actions, history, combat.STRONGARM)
                                              if is_combat else request_for(state, actions, history))
                    response = client.evaluate(model_state, questions)
                    choice = response['answers']['action']['choice']
                    selected = actions[choice]
                    recorder.decision, recorder.action = response, selected
                    model_calls += 1
                    recorder.frame(force=True)
                    issued = adapter.execute(state, selected)
                    after = read_state(bridge.read_memory)
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
                             'after': {'frame': after['frame'], 'minerals': after['minerals'], 'gas': after['gas'],
                                       'workers': [u for u in after['units'] if u['type_id'] == 7 and u['owner'] == after['player_id']]}}
                    decisions.write(json.dumps(event, separators=(',', ':')) + '\n')
                    decisions.flush()
                    history.append({'command': selected['label'], 'kind': selected['kind'], 'squad': selected.get('squad'), 'point': selected.get('point'),
                                    'units': executed.get('units', [executed['unit']] if 'unit' in executed else []),
                                    'squad_centers': [{'name': squad['name'], **squad['center']} for squad in model_state.get('squads', [])],
                                    'frame': state['frame'], 'accepted': verified['accepted'], 'minerals_after': after['minerals'], 'gas_after': after['gas']})
                    print(f'call={model_calls} frame={state["frame"]} minerals={state["minerals"]} gas={state["gas"]} choice={choice} probability={response["answers"]["action"]["probabilities"][choice]:.3f}', flush=True)
                else:
                    recorder.action = {'label': 'Current orders continue; no new command available'}
                bridge.resume()
                deadline = time.monotonic() + decision_seconds
                while time.monotonic() < deadline:
                    recorder.frame()
                    time.sleep(.04)
                bridge.pause()
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
        write_json(recorder.directory / 'incomplete.json', {'status': 'incomplete', 'reason': reason, 'model_calls': model_calls})
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
    parser.add_argument('--capture-fps', type=int, default=8)
    args = parser.parse_args()
    if not (1 <= args.max_requests <= 2000 and 1 <= args.max_seconds <= 3600 and .2 <= args.decision_seconds <= 10 and 1 <= args.capture_fps <= 30):
        parser.error('Requested limits are outside the supported range')
    result = run(args.run_dir, env_file=args.env_file, max_requests=args.max_requests, max_seconds=args.max_seconds,
                 decision_seconds=args.decision_seconds, capture_fps=args.capture_fps)
    raise SystemExit(0 if result == 'victory' else 2)


if __name__ == '__main__':
    main()
