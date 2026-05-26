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
class RequestContext:
    """Optional HTTP request context attached to an error event."""

    method: Optional[str] = None
    path: Optional[str] = None
    host: Optional[str] = None
    status_code: Optional[int] = None
    user_agent: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.method is not None:
            out["method"] = self.method
        if self.path is not None:
            out["path"] = self.path
        if self.host is not None:
            out["host"] = self.host
        if self.status_code is not None:
            out["statusCode"] = self.status_code
        if self.user_agent is not None:
            out["userAgent"] = self.user_agent
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

    request_context: Optional[RequestContext] = None
    """HTTP request context at the time of the error (auto-populated by framework integrations)."""

    trace_id: Optional[str] = None
    """Distributed trace id correlated with this error."""

    metadata: Dict[str, Any] = field(default_factory=dict)
    """Arbitrary key-value context."""

    breadcrumbs: Optional[List[Dict[str, Any]]] = None
    """Breadcrumbs captured before this error."""

    # ── Phase 2 — v2 ingest contract ──────────────────────────────
    sdk_name: Optional[str] = None
    """SDK identifier on the wire — e.g. ``allstak-python``."""

    sdk_version: Optional[str] = None
    """SDK semver — e.g. ``1.2.0``."""

    platform: Optional[str] = None
    """Runtime platform — set to ``python`` by the SDK."""

    dist: Optional[str] = None
    """Build distribution tag (mobile / multi-bundle releases)."""

    frames: Optional[List[Dict[str, Any]]] = None
    """Structured stack frames matching ErrorIngestRequest.Frame on the backend."""

    mechanism: Optional[Dict[str, Any]] = None
    """How the event was captured, e.g. ``{"type": "excepthook", "handled": False}``.
    ``handled=False`` marks the error as unhandled (uncaught) by the application."""

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
        if self.trace_id:
            payload["traceId"] = self.trace_id
        if self.user:
            payload["user"] = self.user.to_dict()
        if self.request_context:
            payload["requestContext"] = self.request_context.to_dict()
        if self.metadata:
            payload["metadata"] = self.metadata
        if self.breadcrumbs:
            payload["breadcrumbs"] = self.breadcrumbs
        # v2 fields
        if self.sdk_name:
            payload["sdkName"] = self.sdk_name
        if self.sdk_version:
            payload["sdkVersion"] = self.sdk_version
        if self.platform:
            payload["platform"] = self.platform
        if self.dist:
            payload["dist"] = self.dist
        if self.frames:
            payload["frames"] = self.frames
        if self.mechanism:
            payload["mechanism"] = self.mechanism
        return payload
