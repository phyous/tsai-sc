"""Synthetic scheduling checks; no live model, game, or victory evidence."""
import copy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tsai_sc import run as runner
from tests.test_run_pacing import snapshot


class SeparateLaneRunnerTests(unittest.TestCase):
    def test_fresh_requests_alternate_with_no_extra_wait_or_false_pause_after_economy(self):
        now = [100.0]
        observed = []
        executions = []
        frames = [100, 100, 100, 100, 104, 160, 160, 160, 164, 220]
        states = [snapshot(frame=frame) for frame in frames]
        states[-1] = {**states[-1], 'status': 'victory', 'evidence': {'TEST': True}}
        pending = iter(states)

        def read_state(_):
            state = next(pending)
            observed.append((state['frame'], now[0]))
            return state

        def sleep(seconds):
            now[0] += seconds

        class FakeRecorder:
            def __init__(self, bridge, directory, fps):
                self.directory = Path(directory)
                self.directory.mkdir()
                (self.directory / 'trace.jsonl').write_text('TEST synthetic capture\n')
                self.state, self.pacing, self.decision, self.action = {}, None, None, {}
                self.index = 0
            def frame(self, *args, **kwargs):
                self.index += 1
            def close(self):
                pass

        def candidates(state, lane, **kwargs):
            return {lane: {'kind': 'continue', 'label': 'TEST ' + lane, 'units': []},
                    'other': {'kind': 'continue', 'label': 'TEST alternative', 'units': []}}

        def request(state, actions, lane, history, mission):
            return {'control_lane': lane, 'squads': [], 'frame': state['frame']}, {}, None

        def evaluate(state, questions):
            lane = state['control_lane']
            return {'metadata': {}, 'answers': {'action': {
                'choice': lane, 'probabilities': {lane: 1, 'other': 0}}}}

        def execute(state, action):
            executions.append(action['label'])
            # Economy no-op leaves both simulation and wall time unchanged.
            if action['label'] == 'TEST army':
                sleep(.1)
            return {'issued': True, 'inputs': []}

        client = Mock(request_count=4, input_tokens_total=0)
        client.evaluate.side_effect = evaluate
        adapter = Mock()
        adapter.execute.side_effect = execute
        bridge = Mock()
        bridge.capture.side_effect = lambda path: Path(path).write_bytes(b'TEST synthetic placeholder')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'TEST-run'
            with patch.object(runner, 'BottleShipBridge', return_value=bridge), \
                    patch.object(runner, 'TypeSafeClient', return_value=client), \
                    patch.object(runner, 'InputAdapter', return_value=adapter), \
                    patch.object(runner, 'Recorder', FakeRecorder), \
                    patch.object(runner, 'read_state', side_effect=read_state), \
                    patch.object(runner.time, 'monotonic', side_effect=lambda: now[0]), \
                    patch.object(runner.time, 'sleep', side_effect=sleep), \
                    patch.object(runner.combat, 'lane_candidates', side_effect=candidates), \
                    patch.object(runner.combat, 'lane_graph_request_for', side_effect=request), \
                    patch.object(runner, 'verify_command', return_value={'accepted': True}), \
                    redirect_stdout(io.StringIO()):
                runner.run(target, decision_seconds=3, combat_decision_seconds=.5, separate_economy=True)
            events = [json.loads(line) for line in (target / 'decisions.jsonl').read_text().splitlines()]
            manifest = json.loads((target / 'manifest.json').read_text())
            result = json.loads((target / 'result.json').read_text())

        self.assertEqual(executions, ['TEST economy', 'TEST army', 'TEST economy', 'TEST army'])
        self.assertEqual(client.evaluate.call_count, 4)
        self.assertEqual(result['model_calls'], 4)
        self.assertTrue(manifest['separate_economy'])
        self.assertEqual([e['control_lane'] for e in events], ['economy', 'army', 'economy', 'army'])
        self.assertEqual([e['pacing']['seconds'] for e in events], [0, .5, 0, .5])
        for event in events:
            self.assertEqual(event['response']['metadata']['control_lane'], event['control_lane'])
            self.assertEqual(event['selected'], event['control_lane'])
        # The second independent request sees the same current game frame
        # immediately after a genuine economy no-op, rather than waiting 3s.
        self.assertEqual(observed[2], observed[3])
        self.assertEqual(observed[6], observed[7])
        self.assertGreaterEqual(observed[5][1] - observed[4][1], .5)
        self.assertLess(observed[5][1] - observed[4][1], .541)


if __name__ == '__main__':
    unittest.main()
