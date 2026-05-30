"""
Release-health session tracking — "one session per process".

On SDK init the client opens a single :class:`Session` for the running
process and POSTs ``/ingest/v1/sessions/start``. Errored / crashed
transitions are recorded purely in-memory; only the terminal
``/ingest/v1/sessions/end`` POST (on graceful shutdown) performs the second
network round-trip, so per-error latency is unaffected.

This mirrors the Java SDK's ``dev.allstak.session`` package
(:file:`Session.java`, :file:`SessionStatus.java`, :file:`SessionTracker.java`)
and the same OK / ERRORED / CRASHED / ABNORMAL status model.

Design rules (all fail-open — session tracking must never crash or block
the host application):

* Sessions are NEVER sampled — the start/end POSTs always fire regardless of
  ``sample_rate``.
* ``start()`` runs the network POST on a daemon thread so SDK init never
  blocks on a round-trip.
* ``end()`` is best-effort with a short timeout and is idempotent.
* Status escalates monotonically: OK → ERRORED → CRASHED. A crash is never
  downgraded back to errored, and the server also refuses to downgrade.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from typing import Any, Optional

logger = logging.getLogger("allstak.sdk")

_PATH_START = "/ingest/v1/sessions/start"
_PATH_END = "/ingest/v1/sessions/end"
_STATE_VERSION = 1
_STATE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000
_RECOVERY_LOCK_MS = 30_000
_RECOVERY_MAX_ATTEMPTS = 3


class SessionStatus:
    """Lifecycle status wire values — match the backend contract.

    * ``ok``       — session ended normally with at most non-fatal logs.
    * ``errored``  — at least one *handled* error-level event landed during
      the session, but the process kept running.
    * ``crashed``  — an unhandled / fatal exception ended the process (the SDK
      only reports this when it observes the uncaught exception itself).
    * ``abnormal`` — process ended without a normal flush. Reserved.
    """

    OK = "ok"
    ERRORED = "errored"
    CRASHED = "crashed"
    ABNORMAL = "abnormal"


class Session:
    """A single release-health session — one per process in server mode.

    Status mutations are guarded by a lock so concurrent
    ``record_error`` / ``record_crash`` calls from multiple threads are safe,
    mirroring the atomic semantics of the Java :class:`Session`.
    """

    def __init__(self, session_id: Optional[str] = None) -> None:
        self.id = session_id or str(uuid.uuid4())
        self._started_at_ms = int(time.time() * 1000)
        self._status = SessionStatus.OK
        self._error_count = 0
        self._lock = threading.Lock()

    @property
    def status(self) -> str:
        with self._lock:
            return self._status

    @property
    def error_count(self) -> int:
        with self._lock:
            return self._error_count

    def record_error(self) -> None:
        """Bump status to ERRORED unless already escalated to a terminal status."""
        with self._lock:
            self._error_count += 1
            if self._status == SessionStatus.OK:
                self._status = SessionStatus.ERRORED

    def record_crash(self) -> None:
        """Mark a terminal CRASHED status (overrides ERRORED). Used by the
        uncaught-exception handler."""
        with self._lock:
            self._error_count += 1
            self._status = SessionStatus.CRASHED

    def record_abnormal_exit(self) -> None:
        """Promote to ABNORMAL only if still OK or ERRORED."""
        with self._lock:
            if self._status in (SessionStatus.OK, SessionStatus.ERRORED):
                self._status = SessionStatus.ABNORMAL

    def duration_ms(self) -> int:
        """Duration from start to now in milliseconds, floored at 0."""
        return max(0, int(time.time() * 1000) - self._started_at_ms)


class SessionTracker:
    """Server-mode single-session tracker.

    One instance per :class:`~allstak.client.AllStakClient`. Re-entrancy safe:
    once started a second :meth:`start` is a no-op; once ended the tracker does
    not re-arm. All network failures are swallowed.
    """

    def __init__(self, transport: Any, config: Any, state_path: Optional[str] = None) -> None:
        self._transport = transport
        self._config = config
        self._lock = threading.Lock()
        self._active: Optional[Session] = None
        self._ended = False
        self._state_path = state_path or _default_state_path(config)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> Optional[Session]:
        """Open the session and POST ``/sessions/start`` on a daemon thread.

        Idempotent. Returns the active session (existing one on a repeat call).
        Sessions are never sampled — the POST always fires when a release is
        resolvable and the transport is enabled.
        """
        with self._lock:
            if self._active is not None:
                return self._active
            session = Session()
            self._active = session

        self._recover_previous_session()

        # No release ⇒ release-health cannot attribute the session. Keep the
        # in-memory tracker so errored/crashed transitions still set a sensible
        # final status, but skip the network call (mirrors the Java SDK).
        release = self._effective_release()
        self._write_state(
            {
                "version": _STATE_VERSION,
                "sessionId": session.id,
                "startedAt": session._started_at_ms,
                "updatedAt": _now_ms(),
                "status": session.status,
                "release": release,
                "environment": getattr(self._config, "environment", None),
                "userId": self._effective_user_id(),
                "sdkName": getattr(self._config, "sdk_name", None),
                "sdkVersion": getattr(self._config, "sdk_version", None),
                "platform": getattr(self._config, "platform", None),
                "closed": False,
            }
        )
        if self._transport_disabled() or not release:
            return session

        payload = {
            "sessionId": session.id,
            "release": release,
            "environment": getattr(self._config, "environment", None),
            "userId": self._effective_user_id(),
            "sdkName": getattr(self._config, "sdk_name", None),
            "sdkVersion": getattr(self._config, "sdk_version", None),
            "platform": getattr(self._config, "platform", None),
        }

        def worker() -> None:
            try:
                self._transport.post(_PATH_START, payload)
                logger.debug("[AllStak] session started: %s", session.id)
            except Exception as exc:  # never crash app boot on a network error
                logger.debug("[AllStak] session start failed: %s", exc)

        thread = threading.Thread(
            target=worker, name="allstak-session-start", daemon=True
        )
        thread.start()
        return session

    def current(self) -> Optional[Session]:
        """The active session, or ``None`` if not started or already ended."""
        with self._lock:
            return None if self._ended else self._active

    def record_error(self) -> None:
        """Record a handled error-level event. No I/O."""
        session = self.current()
        if session is not None:
            session.record_error()
            self._update_open_state(session)

    def record_crash(self) -> None:
        """Record an unhandled / fatal crash. No I/O — the end POST carries it."""
        session = self.current()
        if session is not None:
            session.record_crash()
            self._update_open_state(session)

    def end(self, final_status: Optional[str] = None) -> None:
        """Terminate the session and POST ``/sessions/end``. Idempotent.

        Best-effort, never blocks indefinitely, never raises. If
        ``final_status`` is ``None`` the session's accumulated status is used.
        """
        with self._lock:
            if self._ended or self._active is None:
                self._ended = True
                return
            session = self._active
            self._active = None
            self._ended = True

        status = final_status or session.status
        release = self._effective_release()
        self._write_state(
            {
                "version": _STATE_VERSION,
                "sessionId": session.id,
                "startedAt": session._started_at_ms,
                "updatedAt": _now_ms(),
                "status": status,
                "release": release,
                "environment": getattr(self._config, "environment", None),
                "userId": self._effective_user_id(),
                "sdkName": getattr(self._config, "sdk_name", None),
                "sdkVersion": getattr(self._config, "sdk_version", None),
                "platform": getattr(self._config, "platform", None),
                "closed": True,
                "endedAt": _now_ms(),
            }
        )
        if self._transport_disabled() or not release:
            return

        payload = {
            "sessionId": session.id,
            "durationMs": session.duration_ms(),
            "status": status,
        }
        try:
            self._transport.post(_PATH_END, payload)
            logger.debug(
                "[AllStak] session ended: %s status=%s errors=%d",
                session.id,
                status,
                session.error_count,
            )
        except Exception as exc:  # best-effort — shutdown must not raise
            logger.debug("[AllStak] session end failed: %s", exc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _transport_disabled(self) -> bool:
        try:
            return bool(self._transport.is_disabled())
        except Exception:
            return False

    def _effective_release(self) -> Optional[str]:
        """Release for the session, falling back to the SDK version when no
        release is configured (per the task contract — release is REQUIRED)."""
        release = getattr(self._config, "release", None)
        if release:
            return release
        return getattr(self._config, "sdk_version", None)

    def _effective_user_id(self) -> Optional[str]:
        """Resolve the configured/default user id if one was set, else ``None``."""
        getter = getattr(self, "_user_id_getter", None)
        if getter is None:
            return None
        try:
            return getter()
        except Exception:
            return None

    def _recover_previous_session(self) -> None:
        previous = self._read_state()
        if not previous:
            return

        now = _now_ms()
        if previous.get("closed") is True:
            self._remove_state()
            return
        started_at = previous.get("startedAt")
        if not isinstance(started_at, (int, float)) or now - int(started_at) > _STATE_MAX_AGE_MS:
            self._remove_state()
            return
        attempts = int(previous.get("recoveryAttempts") or 0)
        if attempts >= _RECOVERY_MAX_ATTEMPTS:
            self._remove_state()
            return
        lock_until = int(previous.get("recoveryLockUntil") or 0)
        if lock_until > now:
            return

        owner = str(uuid.uuid4())
        locked = {
            **previous,
            "recoveryAttempts": attempts + 1,
            "recoveryLockOwner": owner,
            "recoveryLockUntil": now + _RECOVERY_LOCK_MS,
            "updatedAt": now,
        }
        self._write_state(locked)
        claimed = self._read_state()
        if not claimed or claimed.get("recoveryLockOwner") != owner:
            return

        status = (
            SessionStatus.CRASHED
            if previous.get("status") == SessionStatus.CRASHED
            else SessionStatus.ABNORMAL
        )
        payload = {
            "sessionId": previous.get("sessionId"),
            "durationMs": max(0, min(2**53 - 1, int(previous.get("updatedAt") or now) - int(started_at))),
            "status": status,
        }
        try:
            if not self._transport_disabled() and self._effective_release():
                self._transport.post(_PATH_END, payload)
            self._write_state(
                {
                    **locked,
                    "status": status,
                    "closed": True,
                    "endedAt": now,
                    "recoveredAt": now,
                    "recoveryLockUntil": 0,
                }
            )
        except Exception as exc:
            logger.debug("[AllStak] session recovery failed: %s", exc)
            self._write_state({**locked, "recoveryLockUntil": 0})

    def _update_open_state(self, session: Session) -> None:
        current = self._read_state()
        if not current or current.get("sessionId") != session.id or current.get("closed") is True:
            return
        self._write_state(
            {
                **current,
                "status": session.status,
                "updatedAt": _now_ms(),
                "userId": self._effective_user_id(),
            }
        )

    def _read_state(self) -> Optional[dict]:
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                parsed = json.load(fh)
            if not _is_valid_state(parsed):
                self._remove_state()
                return None
            return parsed
        except FileNotFoundError:
            return None
        except Exception:
            self._remove_state()
            return None

    def _write_state(self, state: dict) -> None:
        try:
            directory = os.path.dirname(self._state_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = f"{self._state_path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, separators=(",", ":"), sort_keys=True)
            os.replace(tmp, self._state_path)
        except Exception:
            try:
                if "tmp" in locals() and os.path.exists(tmp):
                    os.unlink(tmp)
            except Exception:
                pass

    def _remove_state(self) -> None:
        try:
            os.unlink(self._state_path)
        except FileNotFoundError:
            pass
        except Exception:
            pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _default_state_path(config: Any) -> str:
    try:
        base_dir = getattr(config, "offline_queue_dir", None)
        if not base_dir:
            base_dir = os.path.join(tempfile.gettempdir(), "allstak-session-state")
        host = getattr(config, "host", "") or ""
        api_key = getattr(config, "api_key", "") or ""
        digest = hashlib.sha256(f"{host}|{api_key}".encode("utf-8")).hexdigest()[:16]
        return os.path.join(base_dir, f"session-{digest}.json")
    except Exception:
        return os.path.join(tempfile.gettempdir(), "allstak-session-state", "session-default.json")


def _is_valid_state(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("version") != _STATE_VERSION:
        return False
    if not isinstance(value.get("sessionId"), str) or not value.get("sessionId"):
        return False
    if not isinstance(value.get("startedAt"), (int, float)):
        return False
    if not isinstance(value.get("updatedAt"), (int, float)):
        return False
    return value.get("status") in {
        SessionStatus.OK,
        SessionStatus.ERRORED,
        SessionStatus.CRASHED,
        SessionStatus.ABNORMAL,
    }
