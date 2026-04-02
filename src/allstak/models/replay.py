"""Session replay DTOs — mirrors ReplayIngestRequest on the backend."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ReplayEvent:
    """
    A single captured session event.

    ``event_data`` must be a **JSON-serialized string** — not a dict.
    Use ``from_dict()`` to build from a plain Python dict.
    """

    event_type: str
    """Event category label, e.g. ``"navigation"``, ``"mouse_click"``."""

    event_data: str
    """JSON-serialized string of the event payload.
    Must be a string — the backend stores it verbatim."""

    url: Optional[str] = None
    """Page URL when the event fired."""

    timestamp_millis: Optional[int] = None
    """Unix epoch milliseconds."""

    @classmethod
    def from_dict(
        cls,
        event_type: str,
        data: Dict[str, Any],
        url: Optional[str] = None,
        timestamp_millis: Optional[int] = None,
    ) -> "ReplayEvent":
        """Helper: serialize ``data`` dict to JSON string automatically."""
        return cls(
            event_type=event_type,
            event_data=json.dumps(data, separators=(",", ":")),
            url=url,
            timestamp_millis=timestamp_millis,
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "eventType": self.event_type,
            "eventData": self.event_data,
        }
        if self.url is not None:
            payload["url"] = self.url
        if self.timestamp_millis is not None:
            payload["timestampMillis"] = self.timestamp_millis
        return payload


@dataclass
class ReplayPayload:
    """
    Payload for ``POST /ingest/v1/replay``.

    ``fingerprint`` and ``session_id`` are both required.
    Recommended: set them to the same UUID generated at session start.
    """

    fingerprint: str
    """Stable session fingerprint — used as Kafka partition key.
    Must remain the same across all batches for one session."""

    session_id: str
    """Human-readable session identifier."""

    events: List[ReplayEvent] = field(default_factory=list)
    """At least one event required."""

    def to_dict(self) -> Dict[str, Any]:
        if not self.events:
            raise ValueError("replay payload must contain at least 1 event")
        # Sort events by timestamp for ordering guarantee
        sorted_events = sorted(
            self.events,
            key=lambda e: (e.timestamp_millis or 0),
        )
        return {
            "fingerprint": self.fingerprint,
            "sessionId": self.session_id,
            "events": [e.to_dict() for e in sorted_events],
        }
