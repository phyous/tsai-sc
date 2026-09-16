"""Small, loopback-only client for the local BottleShip game harness bridge."""
from __future__ import annotations
import ipaddress
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_READ = 16 * 1024 * 1024
RPC_READ_LIMIT = 65536
MAX_RESPONSE = 4 * 1024 * 1024
COMMANDS = frozenset(('ping','readBytes','state','report','shot','pause','resume',
    'tickFrames','clickAt','clickHold','move','drag','key','keyHold','type','sleep','fsList','fsRead'))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise URLError('Bridge redirects are disabled')


def _validate_base_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname
        loopback = host == 'localhost' or bool(host and ipaddress.ip_address(host).is_loopback)
        valid_port = parts.port is None or 1 <= parts.port <= 65535
    except ValueError:
        loopback = valid_port = False
    if (not loopback or not valid_port or parts.scheme != 'http' or parts.username
            or parts.password or parts.query or parts.fragment or parts.path not in ('', '/')):
        raise ValueError('The game bridge URL must be plain HTTP on a loopback host, without credentials or a path')
    return value.rstrip('/')


def _memory_range(address: int, length: int, limit: int):
    if (type(address) is not int or type(length) is not int or address < 0
            or length < 0 or length > limit or address > 0xffffffff
            or address + length > 0x100000000):
        raise ValueError('Memory range must be a bounded non-negative 32-bit guest address range')


class BottleShipBridge:
    def __init__(self, base_url: str = 'http://127.0.0.1:3917', timeout: float = 40):
        self.base_url = _validate_base_url(base_url)
        self.timeout = timeout
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _request(self, path: str, body=None, binary=False):
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base_url + path, data=data, headers={'Content-Type': 'application/json'})
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except HTTPError as exc:
            # Do not copy arbitrary backend/HTML error bodies into run logs.
            status = exc.code
            exc.close()
            raise RuntimeError(f'Game bridge request failed (HTTP {status})') from None
        except (URLError, TimeoutError):
            raise RuntimeError('Game bridge is unavailable or timed out') from None
        if len(raw) > MAX_RESPONSE:
            raise RuntimeError('Game bridge response exceeded its size limit')
        if binary:
            return raw
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise RuntimeError('Game bridge returned invalid JSON') from None

    def rpc(self, command: str, *args):
        if command not in COMMANDS:
            raise ValueError('Unsupported game harness command')
        if command == 'readBytes':
            if len(args) != 2:
                raise ValueError('readBytes requires an address and length')
            _memory_range(args[0], args[1], RPC_READ_LIMIT)
            if args[1] == 0:
                raise ValueError('readBytes requires a positive length')
        return self._request('/rpc', {'cmd': command, 'args': list(args)})

    def read_memory(self, address: int, length: int) -> bytes:
        """Read up to 16 MiB, chunking the runtime's 64 KiB RPC limit."""
        _memory_range(address, length, MAX_READ)
        out = bytearray()
        for offset in range(0, length, RPC_READ_LIMIT):
            count = min(RPC_READ_LIMIT, length - offset)
            part = self.rpc('readBytes', address + offset, count)
            try:
                chunk = bytes.fromhex(part['hex'])
            except (KeyError, ValueError, TypeError):
                raise RuntimeError('Game bridge returned malformed memory bytes') from None
            if len(chunk) != count:
                raise RuntimeError('Game bridge returned an incomplete memory range')
            out.extend(chunk)
        return bytes(out)

    def capture(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = self._request('/screenshot', binary=True)
        if not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise RuntimeError('Game bridge returned an invalid PNG')
        destination.write_bytes(data)
        return destination

    def pause(self):
        return self.rpc('pause')

    def resume(self):
        return self.rpc('resume')

    def step(self, frames: int = 24):
        """Resume, await N rendered presents and park; not game logic frame count."""
        if type(frames) is not int or not 1 <= frames <= 240:
            raise ValueError('Step requires 1–240 rendered presents')
        return self._request('/step', {'frames': frames})
