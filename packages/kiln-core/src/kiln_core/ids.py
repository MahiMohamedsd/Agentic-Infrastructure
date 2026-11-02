"""Prefixed, time-sortable identifiers.

Format: ``<prefix>_<26 chars Crockford base32>`` = 48-bit millisecond timestamp + 80 random bits
(ULID layout). The prefix makes IDs self-describing in logs and API responses; sortability
keeps B-tree inserts append-friendly.
"""

from __future__ import annotations

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

PREFIXES = {
    "organization": "org",
    "project": "prj",
    "agent": "agt",
    "agent_version": "agtv",
    "deployment": "dep",
    "execution": "run",
    "event": "evt",
    "task": "tsk",
    "checkpoint": "chk",
    "tool_definition": "tool",
    "tool_invocation": "tinv",
    "policy": "pol",
    "approval": "apr",
    "dataset": "eds",
    "evaluation_run": "evr",
    "evaluation_result": "evres",
    "worker": "wkr",
}


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_id(kind: str, *, now_ms: int | None = None) -> str:
    prefix = PREFIXES[kind]
    ts = int(time.time() * 1000) if now_ms is None else now_ms
    if not 0 <= ts < 2**48:
        raise ValueError("timestamp out of range")
    value = (ts << 80) | secrets.randbits(80)
    return f"{prefix}_{_encode(value, 26)}"


def kind_of(identifier: str) -> str:
    prefix, _, body = identifier.partition("_")
    if len(body) != 26 or any(c not in _ALPHABET for c in body):
        raise ValueError(f"malformed id: {identifier!r}")
    for kind, p in PREFIXES.items():
        if p == prefix:
            return kind
    raise ValueError(f"unknown id prefix: {prefix!r}")
