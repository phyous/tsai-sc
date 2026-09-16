"""Small, fail-closed TypeSafe System One client using only the standard library.

The client never logs request bodies, credentials, or remote error bodies. Network
attempts (including retries) consume the request limit. Probabilities are preserved
as returned by the API, never synthesized or renormalized.
"""

from __future__ import annotations

import json
from http.client import HTTPException
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_URL = "https://api.typesafe.ai/v1/systemone"
MAX_RESPONSE_BYTES = 1_048_576
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})


class TypeSafeError(RuntimeError):
    """Safe to display: messages contain no credentials or remote response text."""


class ConfigurationError(TypeSafeError):
    pass


class RequestLimitError(TypeSafeError):
    pass


class TransportError(TypeSafeError):
    pass


class ResponseValidationError(TypeSafeError):
    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


class _NoRedirect(HTTPRedirectHandler):
    # Bearer credentials must never follow redirects, even to another HTTPS host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_api_key(env_file: str | os.PathLike[str] | None = None) -> str:
    """Read TYPESAFE_API_KEY, optionally from an explicitly selected private file.

    The environment takes precedence. Files must be regular, owned by this user,
    have no group/other permissions, and not be symlinks. Only a literal
    TYPESAFE_API_KEY assignment is read; shell syntax is never evaluated.
    """
    key = os.environ.get("TYPESAFE_API_KEY")
    if key is None and env_file is not None:
        descriptor = None
        try:
            descriptor = os.open(Path(env_file).expanduser(), os.O_RDONLY | os.O_NOFOLLOW)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > 65_536
            ):
                raise ConfigurationError("The API-key file must be private and owned by this user.")
            with os.fdopen(descriptor, encoding="utf-8") as source:
                descriptor = None
                content = source.read(65_537)
            matches = []
            for line in content.splitlines():
                match = re.fullmatch(r"\s*(?:export\s+)?TYPESAFE_API_KEY\s*=\s*(.*?)\s*", line)
                if match:
                    value = match.group(1)
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    matches.append(value)
            if len(matches) != 1:
                raise ConfigurationError("The API-key file must contain one TYPESAFE_API_KEY assignment.")
            key = matches[0]
        except (OSError, UnicodeError):
            raise ConfigurationError("Unable to read the private API-key file.") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
    if not key or not re.fullmatch(r"[!-~]{1,4096}", key):
        raise ConfigurationError("Set a valid TYPESAFE_API_KEY in the environment or a private env file.")
    return key


def _number(value: Any, low: float = 0.0, high: float = 1.0) -> bool:
    try:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and low <= value <= high
        )
    except OverflowError:
        return False


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def probability_total_valid(values, *, rounded_choice=False):
    """Accept exact totals, plus the observed one-point Choice rounding error.

    Jev-1.13.0 sometimes returns cent-quantized Choice values totaling0.99
    despite the documented sum1 contract. Preserve those reported values;
    never renormalize. This narrow compatibility rule also permits1.01.
    """
    values = list(values)
    if not values or not all(_number(value) for value in values):
        return False
    total = math.fsum(values)
    if math.isclose(total, 1, abs_tol=1e-6, rel_tol=0):
        return True
    return (rounded_choice and any(math.isclose(total, limit, abs_tol=1e-8, rel_tol=0) for limit in (.99, 1.01))
            and all(math.isclose(value * 100, round(value * 100), abs_tol=1e-8, rel_tol=0) for value in values))


def _validate_questions(questions: Any) -> None:
    if not isinstance(questions, dict) or not questions:
        raise ConfigurationError("Questions must be a nonempty mapping.")
    for name, question in questions.items():
        if not isinstance(name, str) or not name or not isinstance(question, dict):
            raise ConfigurationError("Question names and question objects are invalid.")
        if "instructions" not in question or not isinstance(question["instructions"], (str, list, dict)):
            raise ConfigurationError("Each question requires text or structured instructions.")
        kind = question.get("type")
        criteria = question.get("criteria")
        if kind == "choice":
            if (
                not isinstance(criteria, dict)
                or not 2 <= len(criteria) <= 255
                or not all(isinstance(key, str) and key for key in criteria)
                or not all(value is None or isinstance(value, (str, list, dict)) for value in criteria.values())
            ):
                raise ConfigurationError("A Choice requires 2 to 255 named options with descriptions.")
        elif kind == "score":
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 255:
                raise ConfigurationError("A Score requires 2 to 255 ordered criteria.")
        elif kind == "noul":
            if criteria is not None and not isinstance(criteria, dict):
                raise ConfigurationError("Noul criteria must be an object.")
        else:
            raise ConfigurationError("Unsupported question type.")


def validate_response(response: Any, questions: dict[str, dict]) -> dict:
    """Validate the complete response and return only known public output fields."""
    failure = "TypeSafe returned an invalid response; no action was selected."
    if not isinstance(response, dict):
        raise ResponseValidationError(failure, {'stage': 'response_object'})
    model = response.get("model")
    answers = response.get("answers")
    usage = response.get("usage")
    if (
        not isinstance(model, str)
        or not re.fullmatch(r"[a-zA-Z0-9._:/-]{1,128}", model)
        or not isinstance(answers, dict)
        or set(answers) != set(questions)
        or not isinstance(usage, dict)
        or not all(type(usage.get(key)) is int and usage[key] >= 0 for key in ("input_tokens", "output_tokens"))
    ):
        raise ResponseValidationError(failure, {'stage': 'response_schema',
                                                'answers_object': isinstance(answers, dict),
                                                'expected_answers': len(questions),
                                                'returned_answers': len(answers) if isinstance(answers, dict) else None,
                                                'answer_keys_match': isinstance(answers, dict) and set(answers) == set(questions),
                                                'usage_object': isinstance(usage, dict)})
    clean_answers = {}
    for question_index, (name, question) in enumerate(questions.items()):
        answer = answers[name]
        kind = question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise ResponseValidationError(failure, {'stage': 'answer_type', 'question_index': question_index})
        if kind == "noul":
            if not _number(answer.get("noul")):
                raise ResponseValidationError(failure)
            clean_answers[name] = {"type": kind, "noul": answer["noul"]}
            continue
        expected = set(question["criteria"]) if kind == "choice" else {str(i) for i in range(len(question["criteria"]))}
        probabilities = answer.get("probabilities")
        confidence = answer.get("confidence")
        if (
            not isinstance(probabilities, dict)
            or set(probabilities) != expected
            or not all(_number(value) for value in probabilities.values())
            or not probability_total_valid(probabilities.values(), rounded_choice=kind == 'choice')
            or not _number(confidence)
        ):
            diagnostics = {'stage': 'probabilities', 'question_index': question_index, 'probability_map': isinstance(probabilities, dict), 'expected_options': len(expected)}
            if isinstance(probabilities, dict):
                numeric = all(_number(p, low=-1e100, high=1e100) for p in probabilities.values())
                diagnostics.update(returned_options=len(probabilities), missing_options=len(expected - set(probabilities)),
                                   unexpected_options=len(set(probabilities) - expected),
                                   probability_sum=math.fsum(probabilities.values()) if numeric else None,
                                   invalid_probabilities=sum(not _number(p) for p in probabilities.values()),
                                   confidence_in_range=_number(confidence))
            raise ResponseValidationError(failure, diagnostics)
        clean = {"type": kind, "probabilities": dict(probabilities), "confidence": confidence}
        if kind == "choice":
            choice = answer.get("choice")
            if not isinstance(choice, str) or choice not in expected:
                raise ResponseValidationError(failure, {'stage': 'choice_key', 'question_index': question_index,
                                                        'choice_is_text': isinstance(choice, str), 'choice_is_expected': False})
            if not math.isclose(probabilities[choice], max(probabilities.values()), abs_tol=1e-9, rel_tol=0):
                raise ResponseValidationError(failure, {'stage': 'choice_argmax', 'question_index': question_index,
                                                        'selected_probability': probabilities[choice],
                                                        'maximum_probability': max(probabilities.values())})
            clean["choice"] = choice
        else:
            score = answer.get("score")
            legend = answer.get("legend")
            weighted = math.fsum(int(key) * value for key, value in probabilities.items())
            if (
                not _number(score, high=len(expected) - 1)
                or not math.isclose(score, weighted, abs_tol=1e-6, rel_tol=0)
                or not isinstance(legend, dict)
                or set(legend) != expected
                or not all(isinstance(value, str) for value in legend.values())
            ):
                raise ResponseValidationError(failure)
            clean.update(score=score, legend=dict(legend))
        clean_answers[name] = clean
    return {
        "model": model,
        "answers": clean_answers,
        "usage": {name: usage[name] for name in ("input_tokens", "output_tokens")},
    }


class TypeSafeClient:
    """Synchronous client. The default request cap bounds total API attempts.

    ``opener`` and ``sleep`` are dependency-injection points for offline transport
    tests. Production calls always use the fixed HTTPS endpoint with redirects
    disabled. One client serializes evaluations to keep its request cap exact.
    """

    def __init__(
        self,
        *,
        model: str = "jev-latest",
        timeout: float = 15.0,
        max_retries: int = 2,
        max_requests: int = 1000,
        env_file: str | os.PathLike[str] | None = None,
        opener: Callable | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not isinstance(model, str) or not re.fullmatch(r"jev[a-zA-Z0-9._-]*", model):
            raise ConfigurationError("Select a Jev model identifier.")
        if not _number(timeout, low=0.1, high=120):
            raise ConfigurationError("Timeout must be between 0.1 and 120 seconds.")
        if type(max_retries) is not int or not 0 <= max_retries <= 5:
            raise ConfigurationError("Retries must be between zero and five.")
        if type(max_requests) is not int or max_requests < 1:
            raise ConfigurationError("The request limit must be a positive integer.")
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_requests = max_requests
        self.request_count = 0
        self.input_tokens_total = 0
        self.output_tokens_total = 0
        self.rejected_response_attempts_total = 0
        self.rejected_usage_unavailable_attempts_total = 0
        self._api_key = read_api_key(env_file)
        self._open = opener if opener is not None else build_opener(_NoRedirect()).open
        self._sleep = sleep
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"TypeSafeClient(model={self.model!r}, request_count={self.request_count}, max_requests={self.max_requests})"

    def evaluate(self, state: Any, questions: dict[str, dict]) -> dict:
        """Send one batch of questions; return validated answers and call metadata.

        Any error raises TypeSafeError. No guessed action or fabricated probability
        is returned. A request cap exhaustion also prevents retry attempts.
        """
        _validate_questions(questions)
        if not isinstance(state, (str, dict, list)):
            raise ConfigurationError("State must be text, an object, or an array.")
        try:
            payload = json.dumps({"model": self.model, "state": state, "questions": questions}, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise ConfigurationError("State and questions must contain valid JSON data.") from None
        with self._lock:
            return self._evaluate(payload, questions)

    def _evaluate(self, payload: bytes, questions: dict[str, dict]) -> dict:
        started = time.perf_counter()
        rejected = rejected_input = rejected_output = rejected_unknown_usage = 0
        for attempt in range(self.max_retries + 1):
            if self.request_count >= self.max_requests:
                raise RequestLimitError("TypeSafe request limit reached; no additional request was sent.")
            self.request_count += 1
            request = Request(API_URL, data=payload, headers={
                "Authorization": "Bearer " + self._api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            }, method="POST")
            status = None
            retry_after = None
            try:
                with self._open(request, timeout=self.timeout) as response:
                    status = response.status
                    if status == 200:
                        body = response.read(MAX_RESPONSE_BYTES + 1)
                    else:
                        retry_after = response.headers.get("Retry-After")
            except HTTPError as error:
                status = error.code
                retry_after = error.headers.get("Retry-After") if error.headers else None
                error.close()
            except (URLError, OSError, HTTPException):
                if attempt >= self.max_retries:
                    raise TransportError("TypeSafe request failed after bounded retries.") from None
            else:
                if status == 200:
                    raw = None
                    try:
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise ResponseValidationError("TypeSafe response exceeded the size limit.")
                        try:
                            raw = json.loads(body, object_pairs_hook=_json_object)
                        except (ValueError, UnicodeError, RecursionError):
                            raise ResponseValidationError("TypeSafe returned invalid JSON; no action was selected.") from None
                        result = validate_response(raw, questions)
                    except ResponseValidationError:
                        # A malformed successful reply is not a game command.
                        # Retry the same request within the shared attempt cap;
                        # never repair probabilities or invent a chosen action.
                        rejected += 1
                        self.rejected_response_attempts_total += 1
                        usage = raw.get("usage") if isinstance(raw, dict) else None
                        known = {name: usage[name] for name in ("input_tokens", "output_tokens")
                                 if isinstance(usage, dict) and type(usage.get(name)) is int and usage[name] >= 0}
                        rejected_input += known.get("input_tokens", 0)
                        rejected_output += known.get("output_tokens", 0)
                        self.input_tokens_total += known.get("input_tokens", 0)
                        self.output_tokens_total += known.get("output_tokens", 0)
                        if len(known) != 2:
                            rejected_unknown_usage += 1
                            self.rejected_usage_unavailable_attempts_total += 1
                        if attempt >= self.max_retries:
                            raise
                        self._sleep(min(0.25 * (2 ** attempt), 2.0))
                        continue
                    self.input_tokens_total += result["usage"]["input_tokens"]
                    self.output_tokens_total += result["usage"]["output_tokens"]
                    result["metadata"] = {
                        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                        "attempts": attempt + 1,
                        "request_count": self.request_count,
                        "input_tokens_total": self.input_tokens_total,
                        "output_tokens_total": self.output_tokens_total,
                        "rejected_response_attempts": rejected,
                        "rejected_input_tokens": rejected_input,
                        "rejected_output_tokens": rejected_output,
                        "rejected_usage_unavailable_attempts": rejected_unknown_usage,
                        "reported_probability_totals": {name: math.fsum(answer['probabilities'].values())
                                                        for name, answer in result['answers'].items() if 'probabilities' in answer},
                    }
                    return result
            if status is not None and (status not in RETRY_STATUSES or attempt >= self.max_retries):
                raise TransportError(f"TypeSafe request failed (HTTP {status}).") from None
            delay = min(0.25 * (2 ** attempt), 2.0)
            try:
                if retry_after is not None and math.isfinite(float(retry_after)):
                    delay = min(max(float(retry_after), 0.0), 2.0)
            except (ValueError, TypeError):
                pass
            self._sleep(delay)
        raise TransportError("TypeSafe request failed after bounded retries.")
