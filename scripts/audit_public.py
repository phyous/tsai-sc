#!/usr/bin/env python3
"""Fail closed before publishing tracked files; print paths, never secret values."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = [
    re.compile(rb"apikey_[A-Za-z0-9_-]{24,}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(rb"AKIA[A-Z0-9]{16}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"sk-[A-Za-z0-9_-]{30,}"),
]
FORBIDDEN_SUFFIXES = {".mpq", ".exe", ".scm", ".scx", ".wgb", ".rep", ".log"}
FORBIDDEN_PARTS = {"assets", "vendor", "runs", ".runtime", ".venv", "node_modules", "recordings"}


def audit() -> list[str]:
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(b"\0")
    failures = []
    for raw in filter(None, paths):
        relative = Path(raw.decode())
        file = ROOT / relative
        if (
            (relative.name.startswith(".env") and relative.name != ".env.example")
            or relative.suffix.lower() in FORBIDDEN_SUFFIXES
            or set(relative.parts) & FORBIDDEN_PARTS
            or file.is_symlink()
        ):
            failures.append(f"Private/generated asset or symlink: {relative}")
            continue
        # Inspect staged bytes, because those are the content about to be published.
        data = subprocess.check_output(["git", "show", f":{relative.as_posix()}"], cwd=ROOT)
        if len(data) > 5_000_000:
            failures.append(f"Large file needs explicit review: {relative}")
        if any(pattern.search(data) for pattern in SECRET_PATTERNS):
            failures.append(f"Possible credential: {relative}")
    return failures


if __name__ == "__main__":
    problems = audit()
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        sys.exit(1)
    print("Public source audit passed: no detected credentials or private game assets.")
