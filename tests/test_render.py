"""Recording tests use synthetic images visibly marked TEST, never real gameplay."""

import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from tsai_sc.render import (
    RenderError, choice_groups, compose_frame, load_trace, render,
    resolve_frame, safe_text, timeline, _objective, _model_order, _supply,
)


def record(t=0, status="running"):
    return {
        "t": t, "frame": "frames/000001.png", "test": True,
        "state": {"minerals": 150, "gas": 40, "mission": "Boot Camp", "frame": 1200,
                  "supply": {"used": 17, "available": 18},
                  "objective_progress": {"supply_depots": {"current": 2, "target": 3},
                                         "refineries": {"current": 1, "target": 1},
                                         "gas": {"current": 40, "target": 100}}},
        "decision": {
            "model": "jev-latest",
            "answers": {"action": {"type": "choice", "choice": "train_marine",
                "probabilities": {"train_marine": 0.73, "gather_minerals": 0.19, "wait": 0.08}, "confidence": 0.64}},
            "usage": {"input_tokens": 500, "output_tokens": 60},
            "metadata": {"latency_ms": 118, "request_count": 7},
        },
        "action": {"label": "TEST: Train a marine at Barracks 12"},
        "status": status,
    }


def make_run(root, records):
    (root / "frames").mkdir()
    game = Image.new("RGB", (640, 480), "#253b31")
    draw = ImageDraw.Draw(game)
    draw.text((160, 220), "TEST — SYNTHETIC GAME FRAME", fill="white", font_size=20)
    game.save(root / "frames/000001.png")
    (root / "trace.jsonl").write_text("\n".join(json.dumps(item) for item in records) + "\n")
    return game


def graph_record():
    item = record()
    item['state'].update(mission='Strongarm', objective_summary='TEST: Destroy the rebel base')
    item['decision']['answers'] = {
        # Unselected answers deliberately precede the selected path and have
        # larger probabilities; neither insertion order nor argmax may route it.
        'action_economy': {'type': 'choice', 'choice': 'Train Marine',
                           'probabilities': {'Train Marine': 0.99, 'Gather minerals': 0.01}, 'confidence': 0.98},
        'action_explore': {'type': 'choice', 'choice': 'Scout east',
                           'probabilities': {'Scout east': 0.6, 'Scout west': 0.4}, 'confidence': 0.2},
        'intent': {'type': 'choice', 'choice': 'Explore',
                   'probabilities': {'Explore': 0.7, 'Economy': 0.2, 'Continue': 0.1}, 'confidence': 0.55},
    }
    item['decision']['metadata']['decision_graph'] = {
        'intent_question': 'intent', 'selected_intent': 'Explore',
        'selected_action_question': 'action_explore', 'selected_candidate': 'Scout east',
    }
    item['action'] = {'label': 'TEST: scout east'}
    return item


class GraphOverlayTests(unittest.TestCase):
    def draw_labels(self, item):
        import tsai_sc.render as module
        game = Image.new('RGB', (640, 480), '#253b31')
        ImageDraw.Draw(game).text((160, 220), 'TEST graph fixture', fill='white')
        with patch('tsai_sc.render._text', wraps=module._text) as draw_text:
            image = compose_frame(item, game, test_only=True)
        self.assertEqual(image.size, (1600, 900))
        return [str(call.args[2]) for call in draw_text.call_args_list]

    def test_selected_intent_and_matching_child_are_displayed_separately(self):
        item = graph_record()
        original = copy.deepcopy(item)
        labels = self.draw_labels(item)
        self.assertIn('1. COMMAND CATEGORY', labels)
        self.assertIn('2. EXPLORE ACTION', labels)
        self.assertIn('Scout east', labels)
        self.assertIn('70%', labels)
        self.assertIn('60%', labels)
        self.assertNotIn('42%', labels)  # Never multiply independent judgments.
        self.assertIn('TEST: scout east', labels)
        self.assertEqual(item, original)

    def test_control_lane_adds_only_its_caption_without_changing_graph_or_probabilities(self):
        for lane, caption, item in (("army", "Army decision", graph_record()),
                                    ("economy", "Economy decision", record())):
            with self.subTest(lane=lane):
                baseline_labels = self.draw_labels(item)
                item['decision']['metadata']['control_lane'] = lane
                original = copy.deepcopy(item)
                labels = self.draw_labels(item)
                self.assertEqual(labels.count(caption), 1)
                self.assertEqual([label for label in labels if label != caption], baseline_labels)
                self.assertEqual(item, original)

    def test_joint_records_have_no_lane_caption_and_unknown_lanes_fail(self):
        labels = self.draw_labels(graph_record())
        self.assertNotIn('Army decision', labels)
        self.assertNotIn('Economy decision', labels)
        for invalid in ('joint', 'unknown', 1, ['army']):
            item = graph_record()
            item['decision']['metadata']['control_lane'] = invalid
            with self.subTest(lane=invalid), self.assertRaises(RenderError):
                self.draw_labels(item)

    def test_unselected_branch_is_retained_but_not_displayed_as_an_order(self):
        item = graph_record()
        labels = self.draw_labels(item)
        self.assertNotIn('2. ECONOMY ACTION', labels)
        self.assertNotIn('Train Marine', labels)
        self.assertNotIn('99%', labels)
        self.assertIn('1 unselected branch question(s) retained in trace', labels)
        self.assertIn('action_economy', item['decision']['answers'])
        self.assertNotIn('EXECUTED ORDER', labels)

    def test_singleton_path_has_only_actual_intent_probabilities(self):
        item = graph_record()
        item['decision']['answers']['intent'].update(
            choice='Continue', probabilities={'Continue': 0.8, 'Explore': 0.15, 'Economy': 0.05})
        item['decision']['metadata']['decision_graph'].update(
            selected_intent='Continue', selected_action_question=None, selected_candidate='Continue current orders')
        item['action'] = {'label': 'TEST: continue current orders'}
        original = copy.deepcopy(item)
        labels = self.draw_labels(item)
        self.assertIn('1. COMMAND CATEGORY', labels)
        self.assertFalse(any(label.startswith('2. ') for label in labels))
        self.assertIn('80%', labels)
        self.assertNotIn('100%', labels)
        self.assertNotIn('60%', labels)
        self.assertNotIn('99%', labels)
        self.assertIn('TEST: continue current orders', labels)
        self.assertEqual(item, original)

    def test_inconsistent_path_metadata_is_rejected(self):
        mutations = {
            'wrong intent': {'selected_intent': 'Economy'},
            'missing root': {'intent_question': 'missing'},
            'missing child': {'selected_action_question': 'missing'},
            'wrong candidate': {'selected_candidate': 'Scout west'},
            'coherent wrong branch': {'selected_action_question': 'action_economy', 'selected_candidate': 'Train Marine'},
            'omitted nonsingleton child': {'selected_action_question': None},
        }
        for name, changes in mutations.items():
            item = graph_record()
            item['decision']['metadata']['decision_graph'].update(changes)
            with self.subTest(name=name), self.assertRaises(RenderError):
                self.draw_labels(item)

    def test_singleton_path_requires_a_concrete_candidate_label(self):
        for candidate in ('', None):
            item = graph_record()
            item['decision']['answers']['intent'].update(
                choice='Continue', probabilities={'Continue': 0.8, 'Explore': 0.15, 'Economy': 0.05})
            item['decision']['metadata']['decision_graph'].update(
                selected_intent='Continue', selected_action_question=None, selected_candidate=candidate)
            with self.subTest(candidate=candidate), self.assertRaises(RenderError):
                self.draw_labels(item)


class TimelineTests(unittest.TestCase):
    def test_timing_uses_elapsed_differences_and_speed(self):
        records = [record(2), record(6), record(14, "victory")]
        segments = timeline(records, speed=4, final_hold=3)
        self.assertEqual([duration for _, duration in segments], [1, 2, 3])
        self.assertEqual(sum(duration for _, duration in segments), 6)

    def test_equal_timestamps_show_most_recent_record(self):
        first, second, third = record(0), record(0), record(4, "victory")
        segments = timeline([first, second, third], 4)
        self.assertIs(segments[0][0], second)
        self.assertEqual(len(segments), 2)

    def test_bad_timestamps_or_speed_fail(self):
        for speed in (0, -1, float("nan"), True, 101):
            with self.subTest(speed=speed), self.assertRaises(RenderError):
                timeline([record()], speed)
        for records in ([], [record(1), record(0)], [record(0), record("bad")]):
            with self.subTest(records=records), self.assertRaises(RenderError):
                timeline(records, 4)


class ValidationTests(unittest.TestCase):
    def test_frame_paths_cannot_escape_or_use_symlink_outside_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "run"
            root.mkdir()
            make_run(root, [record()])
            Image.new("RGB", (640, 480)).save(base / "outside.png")
            (root / "frames/linked.png").symlink_to(base / "outside.png")
            for path in ("../outside.png", str(base / "outside.png"), "frames/linked.png", "missing.png"):
                with self.subTest(path=path), self.assertRaises(RenderError):
                    resolve_frame(root, path)
            self.assertEqual(resolve_frame(root, "frames/000001.png"), (root / "frames/000001.png").resolve())

    def test_empty_missing_malformed_or_premature_terminal_trace_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(RenderError):
                load_trace(root)
            make_run(root, [record()])
            for contents in ("", "not json", '{"t":1,"t":2}', json.dumps(record(1)) + "\n" + json.dumps(record(0)),
                             json.dumps(record(0, "victory")) + "\n" + json.dumps(record(1))):
                (root / "trace.jsonl").write_text(contents)
                with self.subTest(contents=contents), self.assertRaises(RenderError):
                    load_trace(root)

    def test_probabilities_are_real_validated_values(self):
        actual = record()
        self.assertEqual(choice_groups(actual)[0][1]["probabilities"]["train_marine"], 0.73)
        for modification in (
            {"probabilities": {"a": 0.8, "b": 0.7}},
            {"probabilities": {"train_marine": float("nan"), "wait": 1}},
            {"choice": "nonexistent"}, {"choice": "wait"}, {"confidence": True},
        ):
            invalid = copy.deepcopy(actual)
            invalid["decision"]["answers"]["action"].update(modification)
            with self.subTest(modification=modification), self.assertRaises(RenderError):
                choice_groups(invalid)
        actual["decision"] = None
        self.assertEqual(choice_groups(actual), [])

    def test_overlay_text_redacts_credentials_and_control_sequences(self):
        self.assertEqual(safe_text("marine\nready\x00"), "marine ready")
        for value in ("Bearer examplecredential", "TYPESAFE_API_KEY=examplecredential", "sk-abcdefghijklmnop", "apikey_abcdefghijklmnop", "A" * 50):
            self.assertIn("[redacted]", safe_text(value))
        self.assertLessEqual(len(safe_text("marine " * 100, 30)), 30)
        self.assertEqual(safe_text({"secret": "value"}), "—")

    def test_rounded_api_totals_are_labeled_without_normalizing_bars(self):
        import tsai_sc.render as module
        for marine, total in ((0.72, 99), (0.74, 101)):
            with self.subTest(total=total):
                item = record()
                item['decision']['answers']['action']['probabilities']['train_marine'] = marine
                original = copy.deepcopy(item)
                self.assertEqual(choice_groups(item)[0][1]['probabilities'],
                                 original['decision']['answers']['action']['probabilities'])
                with patch('tsai_sc.render._text', wraps=module._text) as draw_text:
                    compose_frame(item, Image.new('RGB', (640, 480)), test_only=True)
                labels = [str(call.args[2]) for call in draw_text.call_args_list]
                self.assertIn(f'API total {total}% (rounded; values shown unchanged)', labels)
                self.assertEqual(item, original)

    def test_renderer_rejects_larger_and_noncent_probability_total_errors(self):
        for values in (
            {'train_marine': 0.71, 'gather_minerals': 0.19, 'wait': 0.08},
            {'train_marine': 0.725, 'gather_minerals': 0.19, 'wait': 0.075},
        ):
            item = record()
            item['decision']['answers']['action']['probabilities'] = values
            with self.subTest(values=values), self.assertRaises(RenderError):
                choice_groups(item)

    def test_original_boot_camp_objectives_are_shown_without_marine_target(self):
        summary = _objective(record()["state"])
        self.assertIn("supply depots: 2/3", summary)
        self.assertIn("refineries: 1/1", summary)
        self.assertIn("gas: 40/100", summary)
        self.assertNotIn("marine", summary)

    def test_nested_supply_and_rejected_order_are_honest(self):
        self.assertEqual(_supply(record()["state"]), "17/18")
        self.assertEqual(_supply({"supply_used": 4, "supply_total": 10}), "4/10")
        self.assertEqual(_model_order({"label": "Build Supply Depot", "accepted": False}), "Not accepted — Build Supply Depot")
        self.assertEqual(_model_order({"label": "Build Supply Depot"}), "Build Supply Depot")

    def test_overlay_describes_last_decision_and_paused_pacing(self):
        import tsai_sc.render as module
        with patch("tsai_sc.render._text", wraps=module._text) as draw_text:
            compose_frame(record(), Image.new("RGB", (640, 480)), test_only=True)
        labels = [str(call.args[2]) for call in draw_text.call_args_list]
        self.assertIn("Last Jev probabilities · current game frame 1200", labels)
        self.assertIn("MODEL ORDER", labels)
        self.assertNotIn("EXECUTED ORDER", labels)
        self.assertTrue(any("PAUSED FOR DECISIONS" in label for label in labels))

    def test_decision_frame_uses_the_evaluated_observation_not_later_game_state(self):
        item = record()
        item['decision']['metadata']['observed_frame'] = 1100
        import tsai_sc.render as module
        with patch('tsai_sc.render._text', wraps=module._text) as draw_text:
            compose_frame(item, Image.new('RGB', (640, 480)), test_only=True)
        labels = [str(call.args[2]) for call in draw_text.call_args_list]
        self.assertIn('Last Jev decision · evaluated frame 1100', labels)

    def test_tied_selected_option_remains_visible_when_bars_are_truncated(self):
        item = record()
        names = ['A', 'B', 'C', 'D', 'E', 'Z selected']
        item['decision']['answers']['action'].update(
            probabilities={name: 1/6 for name in names}, choice='Z selected')
        import tsai_sc.render as module
        with patch('tsai_sc.render._text', wraps=module._text) as draw_text:
            compose_frame(item, Image.new('RGB', (640, 480)), test_only=True)
        labels = [str(call.args[2]) for call in draw_text.call_args_list]
        self.assertIn('Z selected', labels)

    def test_many_worker_options_preserve_probabilities(self):
        item = record()
        probabilities = {f"worker_{index}": 0.01 for index in range(10)}
        probabilities["build_supply_depot"] = 0.9
        item["decision"]["answers"]["action"].update(choice="build_supply_depot", probabilities=probabilities)
        before = copy.deepcopy(item)
        self.assertEqual(compose_frame(item, Image.new("RGB", (640, 480)), test_only=True).size, (1600, 900))
        self.assertEqual(item, before)

    def test_compose_requires_game_only_dimensions(self):
        with self.assertRaises(RenderError):
            compose_frame(record(), Image.new("RGB", (1280, 720)))
        result = compose_frame(record(), Image.new("RGB", (640, 480)), test_only=True)
        self.assertEqual(result.size, (1600, 900))

    def test_dynamic_question_groups_and_missing_decision_render(self):
        item = record()
        template = item["decision"]["answers"]["action"]
        for name in ("combat", "economy", "production"):
            item["decision"]["answers"][name] = copy.deepcopy(template)
        image = Image.new("RGB", (640, 480))
        self.assertEqual(compose_frame(item, image, test_only=True).size, (1600, 900))
        item["decision"] = None
        self.assertEqual(compose_frame(item, image, test_only=True).size, (1600, 900))

    def test_encoding_failure_preserves_existing_output_and_hides_stderr(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_run(root, [record()])
            output = root / "test-video.mp4"
            output.write_bytes(b"existing export")
            fake = subprocess.CompletedProcess([], 1, b"", b"Bearer do-not-expose")
            with patch("tsai_sc.render.shutil.which", return_value="/test/ffmpeg"), patch("tsai_sc.render.subprocess.run", return_value=fake) as run:
                with self.assertRaises(RenderError) as caught:
                    render(root, output)
                self.assertNotIn("do-not-expose", str(caught.exception))
                args = run.call_args.args[0]
                self.assertIsInstance(args, list)
                self.assertNotIn("shell", run.call_args.kwargs)
            self.assertEqual(output.read_bytes(), b"existing export")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe required")
class ExportTests(unittest.TestCase):
    def test_synthetic_export_is_h264_silent_and_correct_duration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_run(root, [record(0), record(4, "victory")])
            output = root / "TEST-synthetic.mp4"
            result = render(root, output, speed=4, fps=30)
            self.assertEqual(result["duration_seconds"], 4)
            self.assertEqual(result["status"], "victory")
            data = subprocess.run([
                "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,pix_fmt,width,height", "-of", "json", str(output),
            ], capture_output=True, text=True, check=True)
            probe = json.loads(data.stdout)
            self.assertAlmostEqual(float(probe["format"]["duration"]), 4, delta=1 / 30)
            self.assertEqual(len(probe["streams"]), 1)
            self.assertEqual(probe["streams"][0], {
                "codec_name": "h264", "codec_type": "video", "width": 1600, "height": 900, "pix_fmt": "yuv420p",
            })


if __name__ == "__main__":
    unittest.main()
