"""Evidence verification with temporary frames visibly labeled TEST.

No original game assets, real credentials, or model calls are used here.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw

from tsai_sc.verify import verify


class VerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'frames').mkdir()
        image = Image.new('RGB', (640, 480), '#253b31')
        ImageDraw.Draw(image).text((170, 225), 'TEST - SYNTHETIC FRAME', fill='white')
        image.save(self.root / 'frames/TEST.png')
        response = {
            'model': 'jev-latest',
            'answers': {'action': {
                'type': 'choice', 'choice': 'mine',
                'probabilities': {'mine': 0.8, 'wait': 0.2}, 'confidence': 0.6,
            }},
            'usage': {'input_tokens': 300, 'output_tokens': 40},
            'metadata': {'latency_ms': 120},
        }
        self.decisions = [{
            'request': {'questions': {'action': {
                'type': 'choice', 'instructions': 'Choose one legal command.',
                'criteria': {'mine': 'Gather minerals', 'wait': 'Continue orders'},
            }}},
            'response': response, 'selected': 'mine',
            'input_result': {'issued': True, 'inputs': [
                {'command': 'clickHold', 'args': [320, 220, 100, 1]},
                {'command': 'keyHold', 'args': ['s', 60]},
                {'command': 'move', 'args': [320, 280]},
            ]},
        }]
        self.frames = [{
            't': t, 'frame': 'frames/TEST.png', 'test': True,
            'state': {'mission': 'Boot Camp'}, 'decision': response,
            'action': {'label': 'TEST: Gather minerals'}, 'status': status,
        } for t, status in ((0, 'running'), (2, 'victory'))]
        self.result = {
            'status': 'victory', 'engine_evidence': {
                'victory_value': 3, 'map_path': 'campaign\\terranED\\tutorial',
                'source': 'TEST fixture',
            },
            'model_calls': 1, 'elapsed_seconds': 2.12345,
            'final_state': {'objective_progress': {
                'supply_depots': {'current': 3, 'target': 3},
                'refineries': {'current': 1, 'target': 1},
                'gas': {'current': 104, 'target': 100},
            }},
        }
        self.write_logs()

    def write_result(self):
        (self.root / 'result.json').write_text(json.dumps(self.result))

    def write_logs(self):
        for filename, records, key in (
            ('trace.jsonl', self.frames, 'trace_sha256'),
            ('decisions.jsonl', self.decisions, 'decisions_sha256'),
        ):
            data = ('\n'.join(json.dumps(record) for record in records) + '\n').encode()
            (self.root / filename).write_bytes(data)
            self.result[key] = hashlib.sha256(data).hexdigest()
        self.write_result()

    def test_valid_evidence_summarizes_actual_recorded_values(self):
        summary = verify(self.root)
        self.assertEqual(summary['outcome'], 'victory')
        self.assertEqual(summary['models'], ['jev-latest'])
        self.assertEqual(summary['model_calls'], 1)
        self.assertEqual(summary['captured_frames'], 2)
        self.assertEqual(summary['input_tokens'], 300)
        self.assertEqual(summary['median_api_latency_ms'], 120)
        self.assertEqual(summary['elapsed_wall_seconds'], 2.123)
        self.assertEqual(summary['final_objectives']['gas']['current'], 104)
        self.assertEqual(summary['trace_sha256'], self.result['trace_sha256'])
        self.assertIn('not win probabilities', summary['probabilities'])

    def test_malformed_or_nonvictorious_result_is_rejected(self):
        for contents in ('not json', '{}', '{"status":"victory"}'):
            with self.subTest(contents=contents):
                (self.root / 'result.json').write_text(contents)
                with self.assertRaises(ValueError):
                    verify(self.root)
        for status, value in (('running', 3), ('defeat', 2), ('victory', 0)):
            with self.subTest(status=status, value=value):
                self.result['status'] = status
                self.result['engine_evidence']['victory_value'] = value
                self.write_result()
                with self.assertRaisesRegex(ValueError, 'No original-engine victory'):
                    verify(self.root)

    def test_wrong_mission_is_rejected(self):
        self.result['engine_evidence']['map_path'] = 'campaign\\terranED\\terran01'
        self.write_result()
        with self.assertRaisesRegex(ValueError, 'original Boot Camp mission'):
            verify(self.root)

    def test_mission_path_matching_allows_case_and_slashes(self):
        self.result['engine_evidence']['map_path'] = 'CAMPAIGN/TERRANED/TUTORIAL'
        self.write_result()
        self.assertEqual(verify(self.root)['outcome'], 'victory')

    def test_any_evidence_byte_change_breaks_recorded_hash(self):
        for filename in ('trace.jsonl', 'decisions.jsonl'):
            with self.subTest(filename=filename):
                path = self.root / filename
                original = path.read_bytes()
                path.write_bytes(original + b'\n')
                with self.assertRaisesRegex(ValueError, 'evidence hash differs'):
                    verify(self.root)
                path.write_bytes(original)

    def test_recording_must_end_at_victory_even_with_matching_hash(self):
        self.frames[-1]['status'] = 'running'
        self.write_logs()
        with self.assertRaisesRegex(ValueError, 'does not end at victory'):
            verify(self.root)

    def test_selected_action_must_match_model_choice(self):
        self.decisions[0]['selected'] = 'wait'
        self.write_logs()
        with self.assertRaisesRegex(ValueError, 'selection differs'):
            verify(self.root)

    def test_only_ordinary_game_input_commands_are_allowed(self):
        for command in ('writeMemory', 'eval', 'loadProgram', 'setVictory', 'resume'):
            with self.subTest(command=command):
                self.decisions[0]['input_result']['inputs'] = [{'command': command, 'args': []}]
                self.write_logs()
                with self.assertRaisesRegex(ValueError, 'outside ordinary game inputs'):
                    verify(self.root)

    def test_model_call_count_must_match_nonempty_decision_log(self):
        self.result['model_calls'] = 2
        self.write_result()
        with self.assertRaisesRegex(ValueError, 'Model-call count'):
            verify(self.root)
        self.decisions = []
        self.result['model_calls'] = 0
        self.write_logs()
        with self.assertRaisesRegex(ValueError, 'Model-call count'):
            verify(self.root)


if __name__ == '__main__':
    unittest.main()
