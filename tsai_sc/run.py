"""Play and record Boot Camp using genuine Jev decisions and original game input."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from .controller import InputAdapter, candidates, request_for, verify_command
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
        self.action = {'label': 'Observing original Boot Camp mission'}

    def frame(self, force=False, status='running'):
        now = time.monotonic() - self.start
        if not force and now - self.last < self.interval:
            return
        filename = f'frames/{self.index:06d}.png'
        self.bridge.capture(self.directory / filename)
        public_state = {k: self.state[k] for k in ('mission', 'frame', 'minerals', 'gas', 'supply', 'objective_progress') if k in self.state}
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
        raise RuntimeError('Start a fresh running Boot Camp mission before recording')
    client = TypeSafeClient(env_file=env_file, max_requests=max_requests)
    recorder = Recorder(bridge, directory, capture_fps)
    recorder.state = initial
    adapter = InputAdapter(bridge, recorder.frame)
    history = []
    start = time.monotonic()
    model_calls = 0
    final = None
    write_json(recorder.directory / 'manifest.json', {
        'started_utc': datetime.now(timezone.utc).isoformat(), 'mission': 'Boot Camp',
        'game': 'Original StarCraft Shareware(ED) v4.00 executable on BottleShip',
        'model_requested': 'jev-latest', 'initial_state': initial,
        'pacing': 'Game paused for consistent memory snapshots and model inference; ordinary game input between snapshots.',
        'observation': 'Read-only own/visible unit state, resources and original mission outcome',
        'controller': 'Jev selects one candidate; deterministic worker/target/placement/input adapter',
        'max_requests': max_requests, 'max_seconds': max_seconds, 'capture_fps': capture_fps,
        'decision_seconds': decision_seconds,
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
                    recorder.action = {'label': 'Original game engine reports ' + state['status']}
                    recorder.frame(force=True, status=state['status'])
                    break
                actions = candidates(state)
                if len(actions) > 1:
                    model_state, questions = request_for(state, actions, history)
                    response = client.evaluate(model_state, questions)
                    choice = response['answers']['action']['choice']
                    selected = actions[choice]
                    recorder.decision, recorder.action = response, selected
                    model_calls += 1
                    recorder.frame(force=True)
                    issued = adapter.execute(state, selected)
                    after = read_state(bridge.read_memory)
                    verified = verify_command(state, after, selected)
                    if issued.get('issued') and selected['kind'] == 'build' and not verified['accepted']:
                        # Clear only a failed placement cursor, never an active
                        # training queue or construction order before selection.
                        bridge.resume()
                        bridge.rpc('keyHold', 'escape', 60)
                        time.sleep(.15)
                        bridge.pause()
                    recorder.state = after
                    recorder.action = {**selected, **verified}
                    event = {'t': time.monotonic() - start, 'state': state, 'request': {'state': model_state, 'questions': questions},
                             'response': response, 'selected': choice, 'action': selected, 'input_result': issued, 'command_verification': verified,
                             'after': {'frame': after['frame'], 'minerals': after['minerals'], 'gas': after['gas'],
                                       'workers': [u for u in after['units'] if u['type_id'] == 7 and u['owner'] == after['player_id']]}}
                    decisions.write(json.dumps(event, separators=(',', ':')) + '\n')
                    decisions.flush()
                    history.append({'command': selected['label'], 'frame': state['frame'], 'accepted': verified['accepted'], 'minerals_after': after['minerals'], 'gas_after': after['gas']})
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
            'trace_sha256': hashlib.sha256((recorder.directory / 'trace.jsonl').read_bytes()).hexdigest(),
            'decisions_sha256': hashlib.sha256((recorder.directory / 'decisions.jsonl').read_bytes()).hexdigest(),
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
