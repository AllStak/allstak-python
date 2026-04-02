"""Error ingest DTO — mirrors ErrorIngestRequest on the backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Valid level values for error ingestion (backend pattern: debug|info|warn|error|fatal|warning)
ERROR_LEVELS = frozenset({"debug", "info", "warn", "error", "fatal", "warning"})


@dataclass
class UserContext:
    """Optional user context attached to an error event."""

    id: Optional[str] = None
    email: Optional[str] = None
    ip: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.id is not None:
            out["id"] = self.id
        if self.email is not None:
            out["email"] = self.email
        if self.ip is not None:
            out["ip"] = self.ip
        return out


@dataclass
class ErrorPayload:
    """
    Payload for ``POST /ingest/v1/errors``.

    Required fields: ``exception_class``, ``message``.
    All other fields are optional.
    """

    exception_class: str
    """Exception type/class name, e.g. ``"ValueError"``."""

    message: str
    """Exception message string."""

    stack_trace: List[str] = field(default_factory=list)
    """Stack trace as a list of frame strings (one frame per element)."""

    level: str = "error"
    """Severity level.  One of: debug, info, warn, error, fatal, warning."""

    environment: Optional[str] = None
    """Deployment environment, e.g. ``"production"``."""

    release: Optional[str] = None
    """Application release/version tag."""

    session_id: Optional[str] = None
    """Links this error to a session replay session."""

    user: Optional[UserContext] = None
    """User context at the time of the error."""

    metadata: Dict[str, Any] = field(default_factory=dict)
    """Arbitrary key-value context."""

    def to_dict(self) -> Dict[str, Any]:
        if self.level not in ERROR_LEVELS:
            raise ValueError(
                f"Invalid error level '{self.level}'. "
                f"Must be one of: {sorted(ERROR_LEVELS)}"
            )
        payload: Dict[str, Any] = {
            "exceptionClass": self.exception_class,
            "message": self.message,
        }
        if self.stack_trace:
            payload["stackTrace"] = self.stack_trace
        if self.level:
            payload["level"] = self.level
        if self.environment:
            payload["environment"] = self.environment
        if self.release:
            payload["release"] = self.release
        if self.session_id:
            payload["sessionId"] = self.session_id
        if self.user:
            payload["user"] = self.user.to_dict()
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload
