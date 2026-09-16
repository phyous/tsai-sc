"""Transport and contract tests; no real model calls or credentials required."""

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import traceback
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from tsai_sc.typesafe import (
    ConfigurationError,
    RequestLimitError,
    ResponseValidationError,
    TransportError,
    TypeSafeClient,
    read_api_key,
)


QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "Which available action advances the mission?",
        "criteria": {"gather": "Mine minerals", "train": "Train a marine", "wait": "Keep current orders"},
    }
}


def api_response():
    return {
        "model": "jev-latest",
        "answers": {
            "action": {
                "type": "choice",
                "choice": "train",
                "probabilities": {"gather": 0.15, "train": 0.8, "wait": 0.05},
                "confidence": 0.72,
            }
        },
        "usage": {"input_tokens": 300, "output_tokens": 45},
    }


class Response(io.BytesIO):
    def __init__(self, data, status=200, headers=None):
        super().__init__(json.dumps(data).encode() if not isinstance(data, bytes) else data)
        self.status = status
        self.headers = headers or {}


class QueueTransport:
    def __init__(self, *results):
        self.results = iter(results)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"TYPESAFE_API_KEY": "test-placeholder"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def client(self, *results, **kwargs):
        transport = QueueTransport(*results)
        sleeps = []
        client = TypeSafeClient(opener=transport, sleep=sleeps.append, **kwargs)
        return client, transport, sleeps

    def test_http_contract_and_usage_telemetry(self):
        client, transport, _ = self.client(Response(api_response()), Response(api_response()))
        result = client.evaluate({"minerals": 50}, QUESTIONS)
        client.evaluate({"minerals": 0}, QUESTIONS)
        request, timeout = transport.requests[0]
        self.assertEqual(request.full_url, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-placeholder")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(timeout, 15)
        self.assertEqual(json.loads(request.data), {
            "state": {"minerals": 50}, "questions": QUESTIONS, "model": "jev-latest",
        })
        self.assertEqual(result["answers"], api_response()["answers"])
        self.assertEqual(result["metadata"]["attempts"], 1)
        self.assertGreaterEqual(result["metadata"]["latency_ms"], 0)
        self.assertEqual(client.input_tokens_total, 600)
        self.assertEqual(client.output_tokens_total, 90)
        self.assertNotIn("test-placeholder", repr(client))

    def test_invalid_probabilities_or_choice_never_produce_actions(self):
        mutations = {
            "missing option": lambda a: a["probabilities"].pop("wait"),
            "extra option": lambda a: a["probabilities"].update(cheat=0),
            "not normalized": lambda a: a["probabilities"].update(train=0.7),
            "negative": lambda a: a["probabilities"].update(train=-0.8),
            "infinite": lambda a: a["probabilities"].update(train=float("inf")),
            "nan": lambda a: a["probabilities"].update(train=float("nan")),
            "boolean": lambda a: a["probabilities"].update(train=True),
            "huge number": lambda a: a["probabilities"].update(train=10 ** 400),
            "unknown choice": lambda a: a.update(choice="cheat"),
            "choice not maximum": lambda a: a.update(choice="gather"),
            "invalid confidence": lambda a: a.update(confidence=1.2),
            "wrong answer type": lambda a: a.update(type="noul"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                data = api_response()
                mutate(data["answers"]["action"])
                client, transport, _ = self.client(Response(data))
                with self.assertRaises(ResponseValidationError):
                    client.evaluate({}, QUESTIONS)
                self.assertEqual(len(transport.requests), 1)

    def test_exact_answer_keys_and_integer_token_usage_required(self):
        mutations = [
            lambda r: r["answers"].update(unrequested=r["answers"]["action"]),
            lambda r: r["answers"].clear(),
            lambda r: r["usage"].update(input_tokens=-1),
            lambda r: r["usage"].update(input_tokens=True),
            lambda r: r.pop("usage"),
        ]
        for mutate in mutations:
            data = api_response()
            mutate(data)
            client, _, _ = self.client(Response(data))
            with self.assertRaises(ResponseValidationError):
                client.evaluate({}, QUESTIONS)

    def test_untrusted_extra_response_fields_are_removed(self):
        data = api_response()
        data["debug"] = "remote-debug-data"
        data["answers"]["action"]["debug"] = "remote-debug-data"
        data["usage"]["debug"] = "remote-debug-data"
        client, _, _ = self.client(Response(data))
        result = client.evaluate({}, QUESTIONS)
        self.assertNotIn("remote-debug-data", json.dumps(result))

    def test_retry_after_is_bounded_and_attempts_count_toward_cap(self):
        failure = HTTPError("https://api.typesafe.ai/v1/systemone", 429, "private server error", {"Retry-After": "999"}, io.BytesIO(b"private body"))
        client, transport, sleeps = self.client(failure, Response(api_response()), max_requests=2)
        result = client.evaluate({}, QUESTIONS)
        self.assertEqual(result["metadata"]["attempts"], 2)
        self.assertEqual(result["metadata"]["request_count"], 2)
        self.assertEqual(sleeps, [2.0])
        with self.assertRaises(RequestLimitError):
            client.evaluate({}, QUESTIONS)
        self.assertEqual(len(transport.requests), 2)

    def test_connection_failures_retry_then_stop_without_raw_errors(self):
        client, transport, sleeps = self.client(
            URLError("private server error"), TimeoutError("private server error"), URLError("private server error"),
        )
        with self.assertRaises(TransportError) as caught:
            client.evaluate({}, QUESTIONS)
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(sleeps, [0.25, 0.5])
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("private server error", rendered)
        self.assertNotIn("test-placeholder", rendered)

    def test_nonretryable_http_error_does_not_read_or_expose_body(self):
        body = io.BytesIO(b"private response body")
        failure = HTTPError("https://api.typesafe.ai/v1/systemone", 401, "private server error", {}, body)
        client, transport, sleeps = self.client(failure)
        with self.assertRaisesRegex(TransportError, r"HTTP 401") as caught:
            client.evaluate({}, QUESTIONS)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(sleeps, [])
        self.assertTrue(body.closed)
        self.assertNotIn("private", "".join(traceback.format_exception(caught.exception)))

    def test_retry_cannot_exceed_request_limit(self):
        client, transport, _ = self.client(URLError("offline"), max_requests=1)
        with self.assertRaises(RequestLimitError):
            client.evaluate({}, QUESTIONS)
        self.assertEqual(len(transport.requests), 1)

    def test_invalid_or_duplicate_json_fails_closed(self):
        for body in (b"not json", b'{"model":"jev-latest","model":"jev-other"}', b"x" * 1_048_577):
            client, _, _ = self.client(Response(body))
            with self.assertRaises(ResponseValidationError):
                client.evaluate({}, QUESTIONS)

    def test_request_validation_prevents_network_call(self):
        bad_questions = copy.deepcopy(QUESTIONS)
        bad_questions["action"]["criteria"] = {"only": "One option"}
        client, transport, _ = self.client()
        with self.assertRaises(ConfigurationError):
            client.evaluate({}, bad_questions)
        with self.assertRaises(ConfigurationError):
            client.evaluate({"x": float("nan")}, QUESTIONS)
        self.assertEqual(transport.requests, [])

    def test_noul_and_score(self):
        questions = {
            "danger": {"type": "noul", "instructions": "Are enemies nearby?"},
            "readiness": {"type": "score", "instructions": "How ready is the squad?", "criteria": ["Weak", "Adequate", "Strong"]},
        }
        data = {
            "model": "jev-latest", "usage": {"input_tokens": 100, "output_tokens": 20},
            "answers": {
                "danger": {"type": "noul", "noul": 0.8},
                "readiness": {"type": "score", "score": 1.6, "confidence": 0.7,
                              "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7},
                              "legend": {"0": "Weak", "1": "Adequate", "2": "Strong"}},
            },
        }
        client, _, _ = self.client(Response(data))
        self.assertEqual(client.evaluate({}, questions)["answers"], data["answers"])

    def test_default_transport_refuses_redirects(self):
        paths = []

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                paths.append(self.path)
                self.send_response(307)
                self.send_header("Location", "/credential-collector")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch("tsai_sc.typesafe.API_URL", f"http://127.0.0.1:{server.server_port}/v1/systemone"):
                client = TypeSafeClient()
                with self.assertRaisesRegex(TransportError, "HTTP 307"):
                    client.evaluate({}, QUESTIONS)
            self.assertEqual(paths, ["/v1/systemone"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_environment_is_required_by_default(self):
        with self.assertRaises(ConfigurationError):
            TypeSafeClient()
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "invalid\nheader"}):
            with self.assertRaises(ConfigurationError):
                TypeSafeClient()

    def test_private_file_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text('# Local key\nTYPESAFE_API_KEY="file-placeholder"\n')
            path.chmod(0o600)
            self.assertEqual(read_api_key(path), "file-placeholder")
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": "environment-placeholder"}):
                self.assertEqual(read_api_key(path), "environment-placeholder")
            path.chmod(0o644)
            with self.assertRaises(ConfigurationError):
                read_api_key(path)
            path.chmod(0o600)
            link = Path(temporary) / "link"
            link.symlink_to(path)
            with self.assertRaises(ConfigurationError):
                read_api_key(link)

    def test_duplicate_assignments_and_shell_expressions_are_not_evaluated(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text("TYPESAFE_API_KEY=first\nTYPESAFE_API_KEY=second\n")
            path.chmod(0o600)
            with self.assertRaises(ConfigurationError):
                read_api_key(path)
            sentinel = Path(temporary) / "must-not-exist"
            path.write_text(f"TYPESAFE_API_KEY=$(touch {sentinel})\n")
            with self.assertRaises(ConfigurationError):
                read_api_key(path)
            self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
