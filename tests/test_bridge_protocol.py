"""Optional checks against a running local bridge; all requests are rejected safely."""
import json
import os
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@unittest.skipUnless(os.environ.get('TSAI_TEST_BRIDGE_URL'), 'Set TSAI_TEST_BRIDGE_URL for live protocol checks')
class BridgeProtocolTests(unittest.TestCase):
    def test_origin_commands_and_ranges_are_rejected_without_reflection(self):
        from tsai_sc.engine import _validate_base_url
        base = _validate_base_url(os.environ['TSAI_TEST_BRIDGE_URL'])
        cases = [
            ('/health', None, {'Origin':'http://untrusted.test'}, 403),
            ('/memory?addr=-1&len=64', None, {}, 400),
            ('/memory?addr=0xffffffff&len=2', None, {}, 400),
            ('/memory?len=64', None, {}, 400),
            ('/rpc', {'cmd':'readBytes','args':[0x400000,65537]}, {}, 400),
            ('/rpc', {'cmd':'waitUntil','args':[{'__fn':'FAKE_TEST_SECRET'}]}, {}, 400),
            ('/step', {'frames':241}, {}, 400),
        ]
        for path, body, headers, expected in cases:
            with self.subTest(path=path, body=body):
                request = Request(base+path, data=None if body is None else json.dumps(body).encode(), headers=headers)
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=3)
                error = raised.exception
                self.assertEqual(error.code, expected)
                self.assertNotIn(b'FAKE_TEST_SECRET', error.read())
                error.close()


if __name__ == '__main__':
    unittest.main()
