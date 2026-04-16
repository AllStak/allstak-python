"""Breadcrumb model for contextual error debugging."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional

VALID_TYPES = frozenset({"http", "log", "ui", "navigation", "query", "default"})
VALID_LEVELS = frozenset({"info", "warn", "error", "debug"})


@dataclass
class Breadcrumb:
    """
    A breadcrumb captures a small piece of contextual information
    (e.g., an HTTP request, a log message, a UI action) that occurred
    before an error. Breadcrumbs are attached to error events.
    """

    type: str
    """Category of the breadcrumb: http, log, ui, navigation, query, default."""

    message: str
    """Human-readable description of the breadcrumb."""

    level: str = "info"
    """Severity level: info, warn, error, debug."""

    data: Optional[Dict[str, Any]] = None
    """Optional key-value data associated with this breadcrumb."""

    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    """ISO-8601 timestamp. Auto-set to current time if not provided."""

    def __post_init__(self) -> None:
        if self.type not in VALID_TYPES:
            self.type = "default"
        if self.level not in VALID_LEVELS:
            self.level = "info"

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "timestamp": self.timestamp,
            "type": self.type,
            "message": self.message,
            "level": self.level,
        }
        if self.data:
            result["data"] = self.data
        return result
