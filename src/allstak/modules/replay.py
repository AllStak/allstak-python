"""
Session replay module — POST /ingest/v1/replay.

Note: Session replay is primarily a browser-side feature.
In Python server contexts this module is useful for:
- Capturing server-side "session" events (API calls, state changes)
- Linking server events to browser sessions
- Testing the replay ingestion endpoint

The Python SDK does NOT capture DOM events (browser-only).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from ..buffer import FlushBuffer
from ..config import AllStakConfig
from ..models.replay import ReplayEvent, ReplayPayload
from ..transport import AllStakAuthError, AllStakTransportError, HttpTransport

logger = logging.getLogger("allstak.sdk")

_INGEST_PATH = "/ingest/v1/replay"
_BATCH_SIZE_THRESHOLD = 50


def _now_ms() -> int:
    return int(time.time() * 1000)


class ReplaySession:
    """
    Represents an active replay session.

    Create via ``ReplayModule.start_session()``.
    Close via ``session.end()`` or use as context manager.
    """

    def __init__(self, module: "ReplayModule", session_id: str, fingerprint: str) -> None:
        self._module = module
        self._session_id = session_id
        self._fingerprint = fingerprint
        self._active = True

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    def record(
        self,
        event_type: str,
        data: Dict[str, Any],
        *,
        url: Optional[str] = None,
        timestamp_millis: Optional[int] = None,
    ) -> None:
        """Record a server-side event for this session."""
        if not self._active:
            return
        event = ReplayEvent.from_dict(
            event_type=event_type,
            data=data,
            url=url,
            timestamp_millis=timestamp_millis or _now_ms(),
        )
        self._module._push_event(self._fingerprint, self._session_id, event)

    def end(self) -> None:
        """End this session and flush remaining events."""
        self._active = False
        self._module.flush()

    def __enter__(self) -> "ReplaySession":
        return self

    def __exit__(self, *_: Any) -> None:
        self.end()


class ReplayModule:
    """
    Buffers and sends session replay events to AllStak.

    Events from the same session (same fingerprint) are collected and
    sent together.  The buffer flushes every ``flush_interval_ms`` ms
    or when ``_BATCH_SIZE_THRESHOLD`` events accumulate.
    """

    def __init__(self, transport: HttpTransport, config: AllStakConfig) -> None:
        self._transport = transport
        self._config = config
        # Buffer of (fingerprint, session_id, event) tuples
        self._flush_buffer: FlushBuffer[tuple] = FlushBuffer(
            flush_fn=self._flush_batch,
            maxsize=config.buffer_size,
            interval_ms=config.flush_interval_ms,
            name="allstak-replay-flush",
        )
        self._flush_buffer.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_session(
        self,
        session_id: Optional[str] = None,
        fingerprint: Optional[str] = None,
    ) -> ReplaySession:
        """
        Start a new replay session.

        :param session_id: Human-readable session ID (auto-generated UUID if omitted).
        :param fingerprint: Stable partition key (defaults to session_id).
        :returns: A ``ReplaySession`` context object.
        """
        sid = session_id or str(uuid.uuid4())
        fp = fingerprint or sid
        return ReplaySession(module=self, session_id=sid, fingerprint=fp)

    def push_event(
        self,
        fingerprint: str,
        session_id: str,
        event_type: str,
        data: Dict[str, Any],
        *,
        url: Optional[str] = None,
        timestamp_millis: Optional[int] = None,
    ) -> None:
        """
        Push a single replay event without a session object.

        Useful for one-off event capture outside of a managed session.
        """
        try:
            event = ReplayEvent.from_dict(
                event_type=event_type,
                data=data,
                url=url,
                timestamp_millis=timestamp_millis or _now_ms(),
            )
            self._push_event(fingerprint, session_id, event)
        except Exception as exc:
            logger.debug("[AllStak] replay.push_event() failed silently: %s", exc)

    def flush(self) -> None:
        """Explicitly flush all buffered replay events."""
        self._flush_buffer.flush()

    def shutdown(self) -> None:
        """Drain buffer and stop the timer thread."""
        self._flush_buffer.stop()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _push_event(
        self, fingerprint: str, session_id: str, event: ReplayEvent
    ) -> None:
        self._flush_buffer.push((fingerprint, session_id, event))

    def _flush_batch(self, items: List[tuple]) -> None:
        """Group items by fingerprint and send one payload per session."""
        # Group by (fingerprint, session_id)
        sessions: Dict[tuple, List[ReplayEvent]] = {}
        for fingerprint, session_id, event in items:
            key = (fingerprint, session_id)
            sessions.setdefault(key, []).append(event)

        for (fingerprint, session_id), events in sessions.items():
            try:
                payload = ReplayPayload(
                    fingerprint=fingerprint,
                    session_id=session_id,
                    events=events,
                )
                status, body = self._transport.post(
                    _INGEST_PATH, payload.to_dict()
                )
                if status != 202:
                    logger.debug(
                        "[AllStak] Replay ingestion returned %d: %s", status, body
                    )
            except AllStakAuthError:
                logger.warning("[AllStak] Replay flush skipped — SDK disabled (invalid API key).")
                return
            except AllStakTransportError as exc:
                logger.debug("[AllStak] Replay transport error (discarding): %s", exc)
            except Exception as exc:
                logger.debug("[AllStak] Unexpected replay flush error: %s", exc)
