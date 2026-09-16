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
from tsai_sc.combat import graph_routing


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

    def test_usage_includes_rejected_response_attempts_without_counting_them_as_decisions(self):
        self.decisions[0]['response']['metadata'].update(attempts=2, rejected_response_attempts=1,
                                                       rejected_input_tokens=280, rejected_usage_unavailable_attempts=0)
        self.result.update(api_attempts=2, accounted_input_tokens=580)
        self.write_logs()
        summary = verify(self.root)
        self.assertEqual((summary['model_calls'], summary['api_attempts'], summary['input_tokens']), (1, 2, 580))
        self.assertEqual(summary['rejected_response_attempts'], 1)
        self.result['accounted_input_tokens'] = 300
        self.write_result()
        with self.assertRaisesRegex(ValueError, 'Accounted input usage'):
            verify(self.root)

    def test_malformed_or_nonvictorious_result_is_rejected(self):
        for contents in ('not json', '{}', '{"status":"victory"}', '[]', 'null', '{"engine_evidence":[]}'):
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
        self.result['engine_evidence']['map_path'] = 'campaign\\terranED\\terran02'
        self.write_result()
        with self.assertRaisesRegex(ValueError, 'supported original demo mission'):
            verify(self.root)

    def strongarm(self):
        self.result['engine_evidence']['map_path'] = 'campaign\\terranED\\terran01'
        self.result['visible_victory_frame'] = 'frames/TEST.png'
        self.result['visible_victory_frame_sha256'] = hashlib.sha256((self.root / 'frames/TEST.png').read_bytes()).hexdigest()
        self.decisions[0]['candidates'] = {'mine': {'kind': 'gather', 'unit': 1, 'target': 2}, 'wait': {'kind': 'wait'}}
        self.decisions[0]['action'] = dict(self.decisions[0]['candidates']['mine'])
        self.write_logs()

    def test_combat_execution_is_bound_to_the_model_selected_candidate(self):
        self.strongarm()
        self.decisions[0]['action']['target'] = 999
        self.write_logs()
        with self.assertRaisesRegex(ValueError, 'model-selected candidate'):
            verify(self.root)

    def test_strongarm_requires_retained_original_screen_for_human_review(self):
        self.result['engine_evidence']['map_path'] = 'campaign\\terranED\\terran01'
        self.write_result()
        with self.assertRaisesRegex(ValueError, 'victory-screen image'):
            verify(self.root)
        self.strongarm()
        summary = verify(self.root)
        self.assertIn('Strongarm combat mission', summary['mission'])
        self.assertEqual(summary['visible_victory_frame'], 'frames/TEST.png')
        self.assertTrue(summary['victory_screen_integrity_checked'])
        # The synthetic fixture is deliberately not mistaken for pixel proof.
        self.assertIn('does not recognize screen text', summary['visual_review'])

    def test_victory_screen_path_cannot_escape_run_directory(self):
        self.strongarm()
        for value in ('../outside.png', '/tmp/outside.png', 'missing.png', ''):
            with self.subTest(path=value):
                self.result['visible_victory_frame'] = value
                self.write_result()
                with self.assertRaises(ValueError):
                    verify(self.root)

    def test_victory_screen_hash_is_required_and_detects_changes(self):
        self.strongarm()
        self.result['visible_victory_frame_sha256'] = '0' * 64
        self.write_result()
        with self.assertRaisesRegex(ValueError, 'image hash differs'):
            verify(self.root)
        self.result.pop('visible_victory_frame_sha256')
        self.write_result()
        with self.assertRaisesRegex(ValueError, 'image hash differs'):
            verify(self.root)

    def test_black_victory_image_is_rejected_even_with_matching_hash(self):
        self.strongarm()
        path = self.root / 'frames/TEST.png'
        Image.new('RGB', (640, 480), 'black').save(path)
        self.result['visible_victory_frame_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.write_result()
        with self.assertRaisesRegex(ValueError, 'nonblank'):
            verify(self.root)

    def graph_fixture(self):
        self.strongarm()
        decision = self.decisions[0]
        decision['candidates'] = {'mine': {'kind': 'gather'}, 'east': {'kind': 'explore'},
                                  'south': {'kind': 'explore'}, 'wait': {'kind': 'continue'}}
        routing = graph_routing(decision['candidates'])
        decision['routing'] = routing
        questions = {'intent': {'type': 'choice', 'instructions': 'TEST: choose category.',
                               'criteria': {key: key for key in routing['branches']}},
                     'action_explore': {'type': 'choice', 'instructions': 'TEST: choose direction.',
                                        'criteria': {'east': 'Go east', 'south': 'Go south'}}}
        decision['request']['questions'] = questions
        decision['response']['answers'] = {
            'intent': {'type': 'choice', 'choice': 'Explore', 'confidence': .7,
                       'probabilities': {'Economy': .2, 'Explore': .7, 'Continue': .1}},
            'action_explore': {'type': 'choice', 'choice': 'south', 'confidence': .8,
                               'probabilities': {'east': .1, 'south': .9}},
        }
        decision['response']['metadata']['decision_graph'] = {
            'intent_question': 'intent', 'selected_intent': 'Explore',
            'selected_action_question': 'action_explore', 'selected_candidate': 'south',
        }
        decision['selected'] = 'south'
        decision['action'] = decision['candidates']['south'].copy()
        self.write_logs()
        return decision

    def test_graph_selected_branch_binds_execution_and_display(self):
        decision = self.graph_fixture()
        self.assertEqual(verify(self.root)['model_calls'], 1)
        decision['selected'] = 'mine'
        self.write_logs()
        with self.assertRaisesRegex(ValueError, 'selection differs'):
            verify(self.root)
        decision['selected'] = 'south'
        decision['response']['metadata']['decision_graph']['selected_intent'] = 'Economy'
        self.write_logs()
        with self.assertRaisesRegex(ValueError, 'display does not match'):
            verify(self.root)

    def test_graph_cannot_reroute_candidates_or_replace_question_keys(self):
        decision = self.graph_fixture()
        decision['routing']['branches']['Economy']['candidate_ids'] = ['south']
        self.write_logs()
        with self.assertRaisesRegex(ValueError, 'routing differs'):
            verify(self.root)
        decision = self.graph_fixture()
        decision['request']['questions']['intent']['criteria']['Explore'] = 'Explore'
        # A matching forged API reply still cannot silently drop a candidate.
        decision['request']['questions']['action_explore']['criteria'] = {'west': 'West', 'south': 'South'}
        decision['response']['answers']['action_explore']['probabilities'] = {'west': .1, 'south': .9}
        self.write_logs()
        with self.assertRaisesRegex(ValueError, 'branch differs'):
            verify(self.root)

    def test_ordinary_modifier_key_input_is_allowed(self):
        self.decisions[0]['input_result']['inputs'].append({'command': 'key', 'args': ['shift', {'down': True}]})
        self.write_logs()
        self.assertEqual(verify(self.root)['outcome'], 'victory')

    def test_mouse_selection_drag_is_allowed_only_inside_canvas(self):
        self.decisions[0]['input_result']['inputs'].append({'command': 'drag', 'args': [96, 96, 104, 104, 0]})
        self.write_logs()
        self.assertEqual(verify(self.root)['outcome'], 'victory')
        for args in ([96, 96, 104, 104], [-1, 96, 104, 104, 0],
                     [96, 96, 640, 104, 0], [96, 96, 104, 480, 0],
                     [96, 96, 104, 104, 1], [True, 96, 104, 104, 0]):
            with self.subTest(args=args):
                self.decisions[0]['input_result']['inputs'][-1]['args'] = args
                self.write_logs()
                with self.assertRaisesRegex(ValueError, 'mouse selection drag'):
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
