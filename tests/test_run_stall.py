"""Synthetic frozen-clock checks; no live game or model evidence."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from tsai_sc import run as runner
from tests.test_run_pacing import snapshot


class StalledClockTests(unittest.TestCase):
    def simulate(self, native_pause_until=0, advance_at=None):
        now = [0.0]
        labels = []
        def read(_):
            frame = 100 + int(advance_at is not None and now[0] >= advance_at)
            return {**snapshot(frame=frame), 'game_paused': now[0] < native_pause_until}
        class Recorder:
            def __init__(self, bridge, directory, fps):
                self.directory = Path(directory)
                self.directory.mkdir()
                self.action = {}
            def frame(self, *args, **kwargs):
                labels.append(self.action.get('label', ''))
            def close(self):
                pass
        def sleep(seconds):
            now[0] += seconds
        bridge, client, adapter = Mock(), Mock(), Mock()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / 'TEST-run'
            with patch.object(runner, 'BottleShipBridge', return_value=bridge), \
                    patch.object(runner, 'TypeSafeClient', return_value=client), \
                    patch.object(runner, 'InputAdapter', return_value=adapter), \
                    patch.object(runner, 'Recorder', Recorder), \
                    patch.object(runner, 'read_state', side_effect=read), \
                    patch.object(runner.time, 'monotonic', side_effect=lambda: now[0]), \
                    patch.object(runner.time, 'sleep', side_effect=sleep), \
                    patch.object(runner.combat, 'candidates', return_value={'Continue': {'kind': 'continue'}}):
                with self.assertRaisesRegex(RuntimeError, 'clock remained stopped'):
                    runner.run(directory, max_seconds=240, decision_seconds=3)
            incomplete = json.loads((directory / 'incomplete.json').read_text())
            self.assertEqual(incomplete['status'], 'incomplete')
            self.assertFalse((directory / 'result.json').exists())
        client.evaluate.assert_not_called()
        adapter.execute.assert_not_called()
        bridge.pause.assert_called()
        return now[0], labels

    def test_frozen_simulation_fails_closed_after_bounded_resumed_waits(self):
        elapsed, labels = self.simulate()
        self.assertGreaterEqual(elapsed, 60)
        self.assertLess(elapsed, 67)
        self.assertIn('Game clock stopped — waiting for simulation progress', labels)
        self.assertNotIn('Original game pause — waiting for mission transmission to finish', labels)

    def test_native_mission_pause_does_not_consume_stall_allowance(self):
        elapsed, labels = self.simulate(native_pause_until=80)
        self.assertGreaterEqual(elapsed, 140)
        self.assertLess(elapsed, 147)
        self.assertIn('Original game pause — waiting for mission transmission to finish', labels)

    def test_observed_progress_resets_stall_allowance(self):
        elapsed, _ = self.simulate(advance_at=45)
        self.assertGreaterEqual(elapsed, 105)
        self.assertLess(elapsed, 112)


if __name__ == '__main__':
    unittest.main()
