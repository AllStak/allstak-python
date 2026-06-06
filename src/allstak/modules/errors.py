"""Error capture module — POST /ingest/v1/errors."""

from __future__ import annotations

import logging
import random
import sys
import traceback
from typing import Any, Dict, List, Optional

from ..config import AllStakConfig
from ..models.breadcrumb import Breadcrumb
from ..models.errors import ErrorPayload, RequestContext, UserContext
from ..sanitize import scrub, scrub_values
from ..transport import AllStakAuthError, AllStakTransportError, HttpTransport

logger = logging.getLogger("allstak.sdk")

_INGEST_PATH = "/ingest/v1/errors"
_DEFAULT_MAX_BREADCRUMBS = 50


class ErrorModule:
    SDK_VERSION = "0.2.0"

    """
    Captures exceptions and sends them to AllStak.

    Errors are sent immediately (no buffering — errors are urgent).
    All failures are swallowed to prevent crashing the host application.
    """

    def __init__(self, transport: HttpTransport, config: AllStakConfig) -> None:
        self._transport = transport
        self._config = config
        self._max_breadcrumbs = getattr(config, "max_breadcrumbs", _DEFAULT_MAX_BREADCRUMBS)
        self._current_user: Optional[UserContext] = None
        self._breadcrumbs: List[Breadcrumb] = []
        self._breadcrumb_lock = __import__("threading").Lock()

    def set_user(self, user: UserContext) -> None:
        """Set a default user context that will be attached to all subsequent errors."""
        self._current_user = user

    def clear_user(self) -> None:
        """Clear the current user context."""
        self._current_user = None

    def add_breadcrumb(
        self,
        type: str,
        message: str,
        level: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Add a breadcrumb. Oldest are dropped when the buffer exceeds 50."""
        crumb = Breadcrumb(type=type, message=message, level=level or "info", data=data)
        with self._breadcrumb_lock:
            if len(self._breadcrumbs) >= self._max_breadcrumbs:
                self._breadcrumbs.pop(0)
            self._breadcrumbs.append(crumb)

    def clear_breadcrumbs(self) -> None:
        """Clear all breadcrumbs from the buffer."""
        with self._breadcrumb_lock:
            self._breadcrumbs.clear()

    def breadcrumb_count(self) -> int:
        """Number of breadcrumbs currently buffered for the next error."""
        with self._breadcrumb_lock:
            return len(self._breadcrumbs)

    def _drain_breadcrumbs(self) -> Optional[List[Dict[str, Any]]]:
        """Drain breadcrumbs and return them as a list of dicts, or None if empty."""
        with self._breadcrumb_lock:
            if not self._breadcrumbs:
                return None
            result = [b.to_dict() for b in self._breadcrumbs]
            self._breadcrumbs.clear()
            return result

    def capture_exception(
        self,
        exc: BaseException,
        *,
        level: str = "error",
        environment: Optional[str] = None,
        release: Optional[str] = None,
        session_id: Optional[str] = None,
        user: Optional[UserContext] = None,
        request_context: Optional[RequestContext] = None,
        trace_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        mechanism: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Capture a Python exception and send it to AllStak.

        Returns the event ID string on success, or None on failure.
        Never raises.

        :param exc: The exception to capture.
        :param level: Severity level (default: "error").
        :param environment: Override environment from init config.
        :param release: Override release from init config.
        :param session_id: Link to a session replay session.
        :param user: User context (overrides set_user()).
        :param metadata: Arbitrary key-value metadata dict.
        :param mechanism: Optional capture mechanism, e.g.
                          ``{"type": "excepthook", "handled": False}``.
        """
        try:
            frames = self._extract_stack_trace(exc)
            structured = self._extract_structured_frames(exc)
            breadcrumbs = self._drain_breadcrumbs()
            payload = ErrorPayload(
                exception_class=type(exc).__name__,
                message=str(exc) or repr(exc),
                stack_trace=frames,
                level=level,
                environment=environment or self._config.environment,
                release=release or self._config.release,
                session_id=session_id,
                user=user or self._current_user,
                request_context=request_context,
                trace_id=trace_id,
                metadata=metadata or {},
                breadcrumbs=breadcrumbs,
                # Phase 2 — v2 ingest contract
                sdk_name=getattr(self._config, "sdk_name", None) or "allstak-python",
                sdk_version=getattr(self._config, "sdk_version", None) or self.SDK_VERSION,
                platform=getattr(self._config, "platform", None) or "python",
                dist=getattr(self._config, "dist", None),
                frames=structured if structured else None,
                mechanism=mechanism,
            )
            return self._send(payload)
        except AllStakAuthError:
            raise  # let the caller (AllStakClient) handle auth disable
        except Exception as send_err:
            logger.debug("[AllStak] capture_exception failed silently: %s", send_err)
            return None

    def capture_error(
        self,
        exception_class: str,
        message: str,
        *,
        stack_trace: Optional[List[str]] = None,
        level: str = "error",
        environment: Optional[str] = None,
        release: Optional[str] = None,
        session_id: Optional[str] = None,
        user: Optional[UserContext] = None,
        request_context: Optional[RequestContext] = None,
        trace_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        mechanism: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Capture an error by class name and message (without a Python exception object).
        Useful when re-surfacing errors from external systems.

        Returns the event ID string on success, or None on failure.
        Never raises.
        """
        try:
            breadcrumbs = self._drain_breadcrumbs()
            payload = ErrorPayload(
                exception_class=exception_class,
                message=message,
                stack_trace=stack_trace or [],
                level=level,
                environment=environment or self._config.environment,
                release=release or self._config.release,
                session_id=session_id,
                user=user or self._current_user,
                request_context=request_context,
                trace_id=trace_id,
                metadata=metadata or {},
                breadcrumbs=breadcrumbs,
                mechanism=mechanism,
            )
            return self._send(payload)
        except AllStakAuthError:
            raise
        except Exception as send_err:
            logger.debug("[AllStak] capture_error failed silently: %s", send_err)
            return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _send(self, payload: ErrorPayload) -> Optional[str]:
        # Single capture chokepoint. Order:
        #   1. sample_rate drop (before before_send — dropped events never
        #      reach the user callback)
        #   2. built-in sanitization before before_send
        #   3. before_send hook (may modify the event or drop it via None;
        #      fail-open if it raises)
        #   4. final built-in sanitization after before_send
        #   5. transport
        #
        # 1. Probabilistic sampling for error/message events.
        sample_rate = getattr(self._config, "sample_rate", 1.0)
        if sample_rate < 1.0 and random.random() >= sample_rate:
            logger.debug("[AllStak] event dropped by sample_rate=%.3f", sample_rate)
            return None

        event = self._sanitize_event(payload.to_dict())

        # 3. before_send hook — runs on an already-sanitized structured event.
        before_send = getattr(self._config, "before_send", None)
        if before_send is not None:
            try:
                result = before_send(event)
                if result is None:
                    logger.debug("[AllStak] event dropped by before_send")
                    return None
                event = result
            except Exception as cb_err:
                # Fail open: a user callback must never crash capture. Fall
                # back to sending the already-sanitized event.
                logger.debug("[AllStak] before_send raised (fail-open): %s", cb_err)

        # 4. Normalize fields that have a richer SDK-side shape but a flatter
        # backend DTO, then sanitize again so hooks cannot reintroduce
        # credentials, cookies, tokens, card numbers, or private nested data.
        event = self._normalize_backend_contract(event)
        wire_payload = self._sanitize_event(event)
        status, body = self._transport.post(_INGEST_PATH, wire_payload)
        if status == 202:
            event_id: Optional[str] = None
            data = body.get("data") or {}
            if isinstance(data, dict):
                event_id = data.get("id")
            return event_id
        logger.debug("[AllStak] Error ingestion returned %d: %s", status, body)
        return None

    def _sanitize_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        # Sanitize the entire wire payload before transport — covers user,
        # metadata, breadcrumbs, request_context, contexts, and any nested
        # values that match the canonical denylist. Pure: no caller mutation.
        wire_payload = scrub(event)
        # Value-pattern PII scrubbing (CC/SSN always; email/IPv4 unless
        # send_default_pii). Protected keys (explicit user object, stack-frame
        # paths, release/sdk identity, URLs, session/trace ids) are skipped by
        # the scrubber so legitimate data is not corrupted. Fail-open inside
        # scrub_values — never breaks an event.
        wire_payload = scrub_values(
            wire_payload,
            send_default_pii=getattr(self._config, "send_default_pii", False),
        )
        # The top-level ``sessionId`` is the SDK-controlled release-health /
        # replay correlation key, not user PII. The canonical denylist scrubs
        # any nested ``session*`` key (correct for user-supplied metadata), so
        # restore the SDK's own top-level value after scrubbing — the backend
        # needs it to attribute errored/crashed sessions. Nested session keys
        # inside user metadata stay redacted.
        if isinstance(wire_payload, dict) and isinstance(event, dict) and "sessionId" in event:
            wire_payload["sessionId"] = event["sessionId"]
        return wire_payload if isinstance(wire_payload, dict) else event

    @staticmethod
    def _normalize_backend_contract(event: Dict[str, Any]) -> Dict[str, Any]:
        """Map SDK-rich fields to the current backend ingest DTO.

        Integrations use ``mechanism={"type": "...", "handled": false}``
        internally because it is expressive and convenient for session status
        handling. The DEV backend's current ``ErrorIngestRequest`` accepts
        ``mechanism`` as a string and ``handled`` as a top-level boolean, so the
        final wire payload must be flattened before transport.
        """
        out = dict(event)
        mechanism = out.get("mechanism")
        if isinstance(mechanism, dict):
            mechanism_type = mechanism.get("type") or mechanism.get("name")
            if mechanism_type is not None:
                out["mechanism"] = str(mechanism_type)
            else:
                out.pop("mechanism", None)
            if "handled" in mechanism and "handled" not in out:
                out["handled"] = bool(mechanism.get("handled"))
        elif mechanism is not None and not isinstance(mechanism, str):
            out["mechanism"] = str(mechanism)
        return out

    @staticmethod
    def _extract_stack_trace(exc: BaseException) -> List[str]:
        """Format the traceback of *exc* as a list of strings."""
        try:
            tb = exc.__traceback__
            if tb is None:
                return []
            lines = traceback.format_tb(tb)
            frames: List[str] = []
            for chunk in lines:
                for line in chunk.splitlines():
                    stripped = line.strip()
                    if stripped:
                        frames.append(stripped)
            return frames
        except Exception:
            return []

    @staticmethod
    def _extract_structured_frames(exc: BaseException) -> List[Dict[str, Any]]:
        """
        Phase 2 — produce v2-shape ``ErrorIngestRequest.Frame`` dicts from
        the traceback so the backend can resolve / display source-mapped
        frames without re-parsing the v1 string list.
        """
        out: List[Dict[str, Any]] = []
        try:
            tb = exc.__traceback__
            if tb is None:
                return out
            for f in traceback.extract_tb(tb):
                in_app = not (f.filename.startswith("<") or "site-packages" in f.filename)
                out.append({
                    "filename": f.filename,
                    "absPath": f.filename,
                    "function": f.name,
                    "lineno": f.lineno,
                    "colno": None,
                    "inApp": in_app,
                    "platform": "python",
                })
        except Exception:
            pass
        return out
