"""Execution journal event envelope (EXECUTION_MODEL.md §9), schema version 1."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kiln_core.ids import new_id
from kiln_core.lifecycle import Writer, is_transition_event

SCHEMA_VERSION = 1
INLINE_PAYLOAD_LIMIT_BYTES = 16 * 1024


def canonical_json(value: Any) -> bytes:
    """Deterministic JSON encoding used for every digest in Kiln."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class Event:
    """One journal entry.

    ``lease_generation`` is the generation the *writer* believed it held. It is checked
    against the projection for WORKER events (fencing) and recorded for audit on all events.
    """

    organization_id: str
    execution_id: str
    sequence: int
    type: str
    writer: Writer
    lease_generation: int
    payload: dict[str, Any] = field(default_factory=dict)
    payload_ref: str | None = None
    payload_digest: str | None = None
    event_id: str = field(default_factory=lambda: new_id("event"))
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    schema: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("sequence starts at 1")
        is_transition_event(self.type)  # raises UnknownEventType
        if (
            self.payload_ref is None
            and len(canonical_json(self.payload)) > INLINE_PAYLOAD_LIMIT_BYTES
        ):
            raise ValueError(
                "payload exceeds inline limit; store it in object storage and set payload_ref"
            )

    @property
    def is_transition(self) -> bool:
        return is_transition_event(self.type)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "event_id": self.event_id,
            "organization_id": self.organization_id,
            "execution_id": self.execution_id,
            "sequence": self.sequence,
            "lease_generation": self.lease_generation,
            "type": self.type,
            "writer": self.writer.value,
            "occurred_at": self.occurred_at.isoformat(),
            "payload": self.payload,
            "payload_ref": self.payload_ref,
            "payload_digest": self.payload_digest,
        }
