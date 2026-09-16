"""Capture guards use labeled temporary fixtures, never gameplay assets."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from tsai_sc.run import Recorder


class CaptureBridge:
    def __init__(self):
        self.blank = False
        self.size = (640, 480)
        self.format = 'PNG'
        self.invalid_bytes = False

    def capture(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.invalid_bytes:
            path.write_bytes(b'not an image')
            return
        image = Image.new('RGB', self.size, 'black')
        if not self.blank:
            ImageDraw.Draw(image).text((160, 220), 'TEST capture fixture', fill='white')
        image.save(path, format=self.format)


class RecorderCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.now = 100.0
        clock = patch('tsai_sc.run.time.monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.bridge = CaptureBridge()
        self.recorder = Recorder(self.bridge, Path(self.temporary.name) / 'run', fps=5)
        self.addCleanup(self.recorder.close)

    def capture(self, seconds):
        self.now = 100 + seconds
        self.recorder.frame(force=True)

    def test_blank_first_capture_stops_before_recording_an_usable_frame(self):
        self.bridge.blank = True
        with self.assertRaisesRegex(RuntimeError, 'first gameplay capture is blank'):
            self.capture(0)
        self.assertEqual(self.recorder.index, 0)
        self.assertEqual((self.recorder.directory / 'trace.jsonl').read_text(), '')
        self.assertTrue((self.recorder.directory / 'frames/000000.png').is_file())

    def test_persistent_blank_stops_at_two_wall_seconds(self):
        self.capture(0)
        self.bridge.blank = True
        self.capture(0.5)
        self.capture(2.49)
        with self.assertRaisesRegex(RuntimeError, 'stayed blank for two seconds'):
            self.capture(2.5)
        self.assertEqual(self.recorder.index, 3)

    def test_nonblank_recovery_resets_the_transition_timer(self):
        self.capture(0)
        self.bridge.blank = True
        self.capture(1)
        self.bridge.blank = False
        self.capture(2)
        self.bridge.blank = True
        self.capture(3)
        self.capture(4.99)
        self.bridge.blank = False
        self.capture(5)
        self.assertIsNone(self.recorder.blank_since)
        self.assertEqual(self.recorder.index, 6)

    def test_wrong_dimensions_or_format_are_rejected(self):
        for size, format in (((1280, 720), 'PNG'), ((640, 480), 'JPEG')):
            with self.subTest(size=size, format=format):
                self.bridge.size, self.bridge.format = size, format
                with self.assertRaisesRegex(RuntimeError, 'original 640x480 PNG'):
                    self.capture(0)
        self.assertEqual(self.recorder.index, 0)

    def test_undecodable_capture_is_rejected_without_raw_error(self):
        self.bridge.invalid_bytes = True
        with self.assertRaisesRegex(RuntimeError, '^Unable to decode the recorded game canvas$'):
            self.capture(0)


if __name__ == '__main__':
    unittest.main()
