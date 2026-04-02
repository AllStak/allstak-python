"""Cron heartbeat DTO — mirrors HeartbeatRequest on the backend."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Optional

_SLUG_RE = re.compile(r"^[a-z0-9\-]+$")


@dataclass
class HeartbeatPayload:
    """
    Payload for ``POST /ingest/v1/heartbeat``.

    The ``slug`` must match an existing CronMonitor created in the
    AllStak management console.  Sending a heartbeat for an unknown
    slug returns 404.
    """

    slug: str
    """Cron monitor slug — lowercase alphanumeric + hyphens only.
    Must match an existing CronMonitor in the AllStak console."""

    status: str
    """``"success"`` or ``"failed"``."""

    duration_ms: int
    """Job execution duration in milliseconds."""

    message: Optional[str] = None
    """Optional human-readable result message."""

    def to_dict(self) -> Dict[str, Any]:
        if not _SLUG_RE.match(self.slug):
            raise ValueError(
                f"Invalid slug '{self.slug}'. "
                "Slugs must match ^[a-z0-9\\-]+$"
            )
        status = self.status.lower()
        if status not in ("success", "failed"):
            raise ValueError(
                f"Invalid status '{self.status}'. Must be 'success' or 'failed'."
            )
        payload: Dict[str, Any] = {
            "slug": self.slug,
            "status": status,
            "durationMs": self.duration_ms,
        }
        if self.message is not None:
            payload["message"] = self.message
        return payload
