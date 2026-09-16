"""Observation scheduling tests use synthetic snapshots and mocked execution."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tsai_sc import run as runner


def snapshot(*, distance=100, visible=True, owner=0, mission='combat', frame=100):
    return {'mission': 'TEST mission', 'mission_kind': mission, 'frame': frame,
            'player_id': 6, 'enemy_players': [0, 3], 'allied_players': [2, 6],
            'status': 'running', 'minerals': 100, 'gas': 100, 'game_paused': False,
            'units': [
                {'id': 1, 'generation': 1, 'type_id': 0, 'owner': 6, 'x': 100, 'y': 100,
                 'hp': 40, 'completed': True, 'visible': True, 'relationship': 'owned'},
                {'id': 2, 'generation': 1, 'type_id': 0, 'owner': owner, 'x': 100 + distance, 'y': 100,
                 'hp': 40, 'completed': True, 'visible': visible, 'relationship': 'enemy'},
            ]}


class ContactPacingTests(unittest.TestCase):
    def pacing(self, state, after=None, enabled=.5):
        return runner.observation_pacing(state, after, decision_seconds=3, combat_decision_seconds=enabled)

    def test_inclusive_512_pixel_boundary_and_diagonal_distance(self):
        self.assertEqual(self.pacing(snapshot(distance=512))['seconds'], .5)
        self.assertEqual(self.pacing(snapshot(distance=513))['seconds'], 3)
        diagonal = snapshot(distance=400)
        diagonal['units'][1]['y'] += 400
        self.assertEqual(self.pacing(diagonal)['seconds'], 3)

    def test_hidden_friendly_allied_and_neutral_units_do_not_trigger(self):
        for state in (snapshot(visible=False), snapshot(owner=6), snapshot(owner=2), snapshot(owner=11)):
            with self.subTest(state=state):
                self.assertEqual(self.pacing(state)['seconds'], 3)

    def test_requires_living_completed_owned_combat_unit(self):
        for changes in ({'hp': 0}, {'completed': False}, {'owner': 2}, {'type_id': 7}, {'type_id': 111}):
            state = snapshot()
            state['units'][0].update(changes)
            with self.subTest(changes=changes):
                self.assertEqual(self.pacing(state)['seconds'], 3)
        state = snapshot()
        state['units'][1]['hp'] = 0
        self.assertEqual(self.pacing(state)['seconds'], 3)

    def test_contact_in_either_snapshot_triggers_only_one_interval(self):
        nearby = snapshot()
        clear = snapshot(distance=900, frame=110)
        before_only = self.pacing(nearby, clear)
        after_only = self.pacing(clear, nearby)
        self.assertEqual(before_only['seconds'], .5)
        self.assertTrue(before_only['contact_before_command'])
        self.assertFalse(before_only['contact_after_command'])
        self.assertEqual(after_only['seconds'], .5)
        self.assertFalse(after_only['contact_before_command'])
        self.assertTrue(after_only['contact_after_command'])
        self.assertEqual(self.pacing(clear, clear)['seconds'], 3)
        self.assertEqual(before_only['before_frame'], 100)
        self.assertEqual(before_only['after_frame'], 110)

    def test_disabled_and_noncombat_preserve_baseline(self):
        self.assertEqual(self.pacing(snapshot(), enabled=None)['seconds'], 3)
        self.assertEqual(self.pacing(snapshot(mission='tutorial'))['seconds'], 3)
        self.assertEqual(self.pacing(None)['seconds'], 3)

    def test_explicit_diplomacy_wins_over_conflicting_relationship(self):
        state = snapshot(owner=11)
        self.assertFalse(runner.visible_combat_contact(state))
        del state['enemy_players']
        self.assertTrue(runner.visible_combat_contact(state))
        state['units'][1]['owner'] = 2
        self.assertFalse(runner.visible_combat_contact(state))

    def test_visible_structure_contact_qualifies_without_guessing_weapons(self):
        state = snapshot()
        state['units'][1]['type_id'] = 112
        self.assertEqual(self.pacing(state)['seconds'], .5)

    def test_observation_snapshots_are_not_mutated(self):
        before, after = snapshot(), snapshot(distance=900)
        original = copy.deepcopy((before, after))
        self.pacing(before, after)
        self.assertEqual((before, after), original)


class PacingValidationTests(unittest.TestCase):
    def cli(self, *arguments):
        with patch('sys.argv', ['run', '--run-dir', 'TEST', *arguments]), \
                patch.object(runner, 'run', return_value='victory') as execute, \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                runner.main()
        return stopped.exception.code, execute

    def test_cli_default_preserves_disabled_pacing(self):
        status, execute = self.cli()
        self.assertEqual(status, 0)
        self.assertIsNone(execute.call_args.kwargs['combat_decision_seconds'])
        self.assertEqual(execute.call_args.kwargs['decision_seconds'], 2)

    def test_cli_opt_in_and_valid_endpoints(self):
        for baseline, combat in [('3', '.5'), ('.2', '.2'), ('10', '10')]:
            status, execute = self.cli('--decision-seconds', baseline, '--combat-decision-seconds', combat)
            self.assertEqual(status, 0)
            self.assertEqual(execute.call_args.kwargs['combat_decision_seconds'], float(combat))

    def test_cli_rejects_out_of_range_nonfinite_or_slower_combat_interval(self):
        for interval in ('.19', '0', '-1', '10.1', '3.1', 'nan', 'inf'):
            with self.subTest(interval=interval):
                status, execute = self.cli('--decision-seconds', '3', '--combat-decision-seconds', interval)
                self.assertEqual(status, 2)
                execute.assert_not_called()

    def test_invalid_direct_call_fails_before_any_runtime_contact(self):
        with patch.object(runner, 'BottleShipBridge') as bridge:
            for baseline, combat in [(3, 4), (3, float('nan')), (3, True), (.1, None)]:
                with self.subTest(baseline=baseline, combat=combat), self.assertRaises(ValueError):
                    runner.run('TEST', decision_seconds=baseline, combat_decision_seconds=combat)
            bridge.assert_not_called()


class PacingRunnerIntegrationTests(unittest.TestCase):
    def synthetic_run(self, combat_seconds):
        """Exercise the real loop with no bridge, model, images or external I/O."""
        now = [100.0]
        captures = []
        observed_times = []
        selected_actions = []
        before = snapshot()
        after = snapshot(distance=900, frame=116)
        victory = {**snapshot(distance=900, frame=125), 'status': 'victory', 'evidence': {'TEST': True}}
        states = iter([copy.deepcopy(before), before, after, victory])

        def read_state(_):
            state = next(states)
            observed_times.append((state['frame'], now[0]))
            return state

        def sleep(seconds):
            now[0] += seconds

        class FakeRecorder:
            def __init__(self, bridge, directory, fps):
                self.directory = Path(directory)
                self.directory.mkdir()
                (self.directory / 'trace.jsonl').write_text('TEST mocked capture\n')
                self.state, self.pacing, self.decision, self.action = {}, None, None, {}
                self.index = 0
            def frame(self, *args, **kwargs):
                captures.append(copy.deepcopy(self.pacing))
                self.index += 1
            def close(self):
                pass

        def execute(state, action):
            selected_actions.append(copy.deepcopy(action))
            sleep(.1)
            return {'issued': True, 'inputs': []}

        action = {'kind': 'continue', 'label': 'TEST model-selected command'}
        response = {'metadata': {}, 'answers': {'action': {'choice': 'selected', 'probabilities': {'selected': 1, 'other': 0}}}}
        client = Mock(request_count=1, input_tokens_total=0)
        client.evaluate.return_value = response
        adapter = Mock()
        adapter.execute.side_effect = execute
        bridge = Mock()
        bridge.capture.side_effect = lambda path: Path(path).write_bytes(b'TEST synthetic result placeholder')
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'TEST-run'
            with patch.object(runner, 'BottleShipBridge', return_value=bridge), \
                    patch.object(runner, 'TypeSafeClient', return_value=client), \
                    patch.object(runner, 'InputAdapter', return_value=adapter), \
                    patch.object(runner, 'Recorder', FakeRecorder), \
                    patch.object(runner, 'read_state', side_effect=read_state), \
                    patch.object(runner.time, 'monotonic', side_effect=lambda: now[0]), \
                    patch.object(runner.time, 'sleep', side_effect=sleep), \
                    patch.object(runner.combat, 'candidates', return_value={'selected': action, 'other': action}), \
                    patch.object(runner.combat, 'graph_request_for', return_value=({'squads': []}, {}, None)), \
                    patch.object(runner, 'verify_command', return_value={'accepted': True}), \
                    redirect_stdout(io.StringIO()):
                outcome = runner.run(target, decision_seconds=3, combat_decision_seconds=combat_seconds)
            manifest = json.loads((target / 'manifest.json').read_text())
            event = json.loads((target / 'decisions.jsonl').read_text())
        self.assertEqual(outcome, 'victory')  # Synthetic branch only, never game evidence.
        self.assertEqual(selected_actions, [action])
        self.assertEqual(client.evaluate.call_count, 1)
        return manifest, event, captures, observed_times

    def test_opt_in_changes_only_post_command_wait_and_records_reason(self):
        baseline = self.synthetic_run(None)
        adaptive = self.synthetic_run(.5)
        self.assertIsNone(baseline[0]['combat_decision_seconds'])
        self.assertEqual(adaptive[0]['decision_seconds'], 3)
        self.assertEqual(adaptive[0]['combat_decision_seconds'], .5)
        for result, delay in [(baseline, 3), (adaptive, .5)]:
            _, event, captures, observed = result
            self.assertEqual(event['pacing']['seconds'], delay)
            self.assertAlmostEqual(event['timing']['command_finished_t'] - event['timing']['command_started_t'], .1)
            self.assertEqual(event['timing']['after_observed_t'], event['timing']['command_finished_t'])
            wait = observed[-1][1] - observed[-2][1]
            self.assertGreaterEqual(wait, delay)
            self.assertLess(wait, delay + .041)
            waiting = [entry for entry in captures if entry and entry.get('phase') == 'between_decisions']
            self.assertTrue(waiting)
            self.assertAlmostEqual(waiting[0]['wait_scheduled_until_t'] - waiting[0]['wait_started_t'], delay)
        self.assertEqual(adaptive[1]['pacing']['reason'], 'visible_hostile_within_512px_before_or_after_command')
        self.assertTrue(adaptive[1]['pacing']['contact_before_command'])
        self.assertFalse(adaptive[1]['pacing']['contact_after_command'])


if __name__ == '__main__':
    unittest.main()
