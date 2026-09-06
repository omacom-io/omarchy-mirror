"""Small shared contracts; no environment or network work at import time."""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath


class Error(Exception):
    def __init__(self, message, code="invalid", hint="Run the command with --help.", exit_code=1):
        super().__init__(message)
        self.code, self.hint, self.exit_code = code, hint, exit_code


class Conflict(Error):
    def __init__(self, message="State changed during publication; nothing was activated."):
        super().__init__(message, "conflict", "Read the current ring and retry or rebase the candidate.", 4)


class Missing(Error):
    def __init__(self, key):
        super().__init__(f"Not found: {key}", "not_found", "Check the ID and configured store.", 3)


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,100}", value):
        raise Error(f"Invalid name: {value!r}", hint="Use lowercase letters, digits, dots, underscores, and hyphens.")
    return value


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise Error("Invalid SHA-256 identifier.")
    return value


def repository_order(repositories, order=None):
    if order is None:
        ranks = {"core": 0, "extra": 1, "multilib": 2}
        return sorted(repositories, key=lambda r: (ranks.get(r, 3), r))
    if isinstance(order, str):
        order = order.split(",")
    if len(order) != len(set(order)) or set(order) != set(repositories):
        raise Error("Repository order must contain every selected repository exactly once.")
    return list(order)


def safe_path(value):
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise Error("Invalid relative object path.")
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or any(p in ("", ".", "..") for p in value.rstrip("/").split("/")):
        raise Error("Invalid relative object path.")
    return str(PurePosixPath(value))
