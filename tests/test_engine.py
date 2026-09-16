"""Protocol boundary tests; the live emulator is not needed."""
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock
from urllib.error import HTTPError

from tsai_sc.engine import BottleShipBridge, MAX_READ


class BridgeTests(unittest.TestCase):
    def test_only_loopback_http_without_credentials(self):
        for url in ('http://127.0.0.1:3917', 'http://localhost:3917/', 'http://[::1]:3917'):
            self.assertTrue(BottleShipBridge(url).base_url.startswith('http:'))
        for url in ('https://localhost:3917', 'http://example.com', 'http://192.168.1.2',
                    'http://localhost.evil', 'http://user:password@localhost',
                    'http://127.0.0.1/a', 'http://127.0.0.1?token=x',
                    'http://127.0.0.1#x', 'file:///tmp/a', 'http://[broken'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                BottleShipBridge(url)

    def test_read_chunks_and_checks_short_response(self):
        bridge = BottleShipBridge()
        bridge.rpc = Mock(side_effect=lambda cmd, addr, count: {'hex': ('ab' * count)})
        self.assertEqual(bridge.read_memory(0x400000, 65540), b'\xab' * 65540)
        self.assertEqual([call.args for call in bridge.rpc.call_args_list],
                         [('readBytes', 0x400000, 65536), ('readBytes', 0x410000, 4)])
        bridge.rpc.return_value = {'hex':'00'}
        bridge.rpc.side_effect = None
        with self.assertRaisesRegex(RuntimeError, 'incomplete'):
            bridge.read_memory(0x400000, 4)

    def test_bad_ranges_rejected_before_network(self):
        bridge = BottleShipBridge()
        bridge._request = Mock()
        for addr, count in ((-1, 1), (0, -1), (0, MAX_READ+1), (0xffffffff, 2),
                            (0x100000000, 0), (True, 1), (0, 1.0)):
            with self.subTest(addr=addr, count=count), self.assertRaises(ValueError):
                bridge.read_memory(addr, count)
        self.assertEqual(bridge.read_memory(0x400000, 0), b'')
        for length in (0, -1, 65537):
            with self.assertRaises(ValueError):
                bridge.rpc('readBytes', 0x400000, length)
        bridge._request.assert_not_called()

    def test_no_arbitrary_eval_and_bounded_step(self):
        bridge = BottleShipBridge()
        bridge._request = Mock()
        with self.assertRaises(ValueError):
            bridge.rpc('waitUntil', {'__fn':'secret()'})
        for value in (0, 241, -1, 1.1, True):
            with self.assertRaises(ValueError):
                bridge.step(value)
        bridge._request.assert_not_called()
        bridge.rpc('key', 'b')
        bridge._request.assert_called_once_with('/rpc', {'cmd':'key', 'args':['b']})

    def test_backend_error_body_not_logged(self):
        bridge = BottleShipBridge()
        bridge._opener = Mock()
        bridge._opener.open.side_effect = HTTPError('http://localhost/',500,'oops',{},
            io.BytesIO(b'{"error":"token=DO_NOT_LEAK_THIS"}'))
        with self.assertRaises(RuntimeError) as raised:
            bridge.pause()
        self.assertEqual(str(raised.exception), 'Game bridge request failed (HTTP 500)')
        self.assertNotIn('DO_NOT_LEAK', str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_redirect_is_not_followed(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/should-not-follow')
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            bridge = BottleShipBridge(f'http://127.0.0.1:{server.server_port}')
            with self.assertRaisesRegex(RuntimeError,'unavailable'):
                bridge._request('/health')
            self.assertEqual(requests, ['/health'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
