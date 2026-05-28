"""
Offline / persistent event spool — survive a process restart *and* a network
outage by writing un-sent, PII-scrubbed telemetry to a filesystem directory and
replaying it on the next SDK init.

This is the server-runtime analogue of Sentry's offline envelope cache. It is
the idiomatic mechanism for this SDK: the in-memory :class:`~allstak.buffer.
FlushBuffer` absorbs back-pressure during the process lifetime; the spool
catches whatever the transport could *not* deliver before the process ended or
while the network was down, and re-sends it on the next boot.

Design rules (all fail-open — persistence must never crash, block, or slow down
capture / init):

* **Scrub before persist.** Every payload is run through the SDK's canonical
  :func:`~allstak.sanitize.scrub` *before* it touches disk. Scrubbing is
  idempotent, so payloads that a module already scrubbed (errors) stay scrubbed
  and the rest get covered for free. Secrets never hit the spool.
* **One file per event.** ``{ts_ms}-{uuid}.json`` written atomically via a
  ``.tmp`` sibling + :func:`os.replace`. The filename's millisecond prefix gives
  a cheap oldest-first ordering for both eviction and drain.
* **Bounded.** Capped by count, total bytes, *and* max age. When over a cap the
  OLDEST entries are dropped. The spool can never grow without bound.
* **Session lifecycle is never persisted.** ``/sessions/start`` and
  ``/sessions/end`` are best-effort live-only; a replayed stale session would
  skew durations. Only error / log / span / http / db telemetry is spooled.
* **Graceful degradation.** If the directory is unavailable / unwritable
  (read-only FS, serverless, sandbox) the spool silently no-ops and the SDK
  falls back to its existing in-memory behaviour. It never raises.
* **Drain on init.** A daemon thread loads persisted entries oldest-first and
  re-sends them through the *existing* transport (so retry / backoff / circuit
  breaker still apply). An entry is removed only once it is accepted (2xx) or is
  permanently undeliverable (4xx other than 429). A 401 stops the drain (SDK
  disabled). Network errors leave the entry on disk for a later boot.
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
from typing import Any, Callable, Dict, List, Optional, Tuple

from .sanitize import scrub

logger = logging.getLogger("allstak.sdk")

# Ingest paths whose telemetry is safe + useful to replay after a restart.
# Everything else (sessions, heartbeats, releases, flags, replay) is live-only.
PERSISTABLE_PATHS: frozenset[str] = frozenset(
    {
        "/ingest/v1/errors",
        "/ingest/v1/logs",
        "/ingest/v1/spans",
        "/ingest/v1/http-requests",
        "/ingest/v1/db",
    }
)

# Sane server defaults (a few MB / a couple of days).
_DEFAULT_MAX_EVENTS = 100
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB
_DEFAULT_MAX_AGE_S = 48 * 3600  # 48 hours

# A single oversized payload should not wedge the spool — skip anything larger
# than the whole byte budget rather than evicting everything else for it.
_PER_ENTRY_BYTE_GUARD = _DEFAULT_MAX_BYTES


def is_persistable_path(path: str) -> bool:
    """Whether telemetry posted to ``path`` may be written to the spool.

    Session lifecycle and other live-only endpoints return ``False`` so a
    replayed stale entry can never skew their semantics.
    """
    return path in PERSISTABLE_PATHS


def default_spool_dir(host: str) -> str:
    """Per-backend spool directory under the system temp dir.

    The host is hashed into the directory name so two SDKs pointed at different
    backends (or different API environments) do not replay each other's events.
    """
    digest = hashlib.sha1((host or "default").encode("utf-8")).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), "allstak-spool", digest)


class EventSpool:
    """Thread-safe, bounded, fail-open filesystem spool for un-sent events.

    The spool is created lazily: the directory is only touched on the first
    write / drain, and any failure flips the spool to a permanent no-op so the
    SDK keeps working with its in-memory buffers.
    """

    def __init__(
        self,
        directory: str,
        *,
        max_events: int = _DEFAULT_MAX_EVENTS,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        max_age_s: float = _DEFAULT_MAX_AGE_S,
        enabled: bool = True,
    ) -> None:
        self._dir = directory
        self._max_events = max(1, int(max_events))
        self._max_bytes = max(1024, int(max_bytes))
        self._max_age_s = max(0.0, float(max_age_s))
        self._enabled = bool(enabled)
        self._unavailable = not self._enabled  # set True permanently on failure
        self._lock = threading.Lock()
        # Monotonic per-process counter used as a filename tiebreaker so entries
        # written within the same millisecond still sort in insertion
        # (oldest-first) order. The millisecond ``ts`` prefix remains the primary
        # sort key, so cross-restart ordering is preserved.
        self._seq = 0

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    @property
    def directory(self) -> str:
        return self._dir

    def available(self) -> bool:
        """True if the spool can be used. Degrades to False permanently once a
        directory operation fails (read-only FS, sandbox, serverless)."""
        if self._unavailable:
            return False
        try:
            os.makedirs(self._dir, exist_ok=True)
            return os.access(self._dir, os.W_OK)
        except Exception as exc:  # pragma: no cover — platform dependent
            logger.debug("[AllStak] spool dir unavailable (%s): %s", self._dir, exc)
            self._unavailable = True
            return False

    # ------------------------------------------------------------------
    # Persist
    # ------------------------------------------------------------------

    def persist(self, path: str, payload: Dict[str, Any]) -> bool:
        """Scrub then atomically write one event to the spool. Fail-open.

        Returns ``True`` if the entry was written. Returns ``False`` (without
        raising) when persistence is disabled, the path is not persistable, the
        payload cannot be serialised / scrubbed, or the directory is unwritable.
        """
        if self._unavailable or not is_persistable_path(path):
            return False
        try:
            # Scrub BEFORE serialisation so no secret is ever encoded to disk.
            # Idempotent for already-scrubbed payloads (e.g. error events).
            scrubbed = scrub(payload)
            record = {"path": path, "payload": scrubbed, "ts": int(time.time() * 1000)}
            blob = json.dumps(record, ensure_ascii=False, default=str).encode("utf-8")
        except Exception as exc:
            logger.debug("[AllStak] spool serialise failed (dropping): %s", exc)
            return False

        if len(blob) > _PER_ENTRY_BYTE_GUARD:
            logger.debug("[AllStak] spool entry too large (%d bytes); skipped", len(blob))
            return False

        if not self.available():
            return False

        with self._lock:
            self._seq += 1
            seq = self._seq
            name = f"{record['ts']:013d}-{seq:08d}-{uuid.uuid4().hex}.json"
            final = os.path.join(self._dir, name)
            tmp = final + ".tmp"
            try:
                with open(tmp, "wb") as fh:
                    fh.write(blob)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, final)
            except Exception as exc:
                logger.debug("[AllStak] spool write failed (%s): %s", final, exc)
                self._cleanup_tmp(tmp)
                self._unavailable = True
                return False
            # Enforce bounds while holding the lock so concurrent writers agree.
            self._enforce_bounds_locked()
        return True

    # ------------------------------------------------------------------
    # Drain
    # ------------------------------------------------------------------

    def drain(self, send_fn: Callable[[str, Dict[str, Any]], Tuple[int, Any]]) -> int:
        """Replay spooled events oldest-first through ``send_fn``.

        ``send_fn(path, payload)`` must return an HTTP ``(status, body)`` tuple
        (i.e. the transport's own ``post`` semantics). An entry file is deleted
        only when it is accepted (2xx) or permanently undeliverable (a 4xx other
        than 429). Any exception (network error / retries exhausted) or a 429 /
        5xx leaves the entry on disk for a later boot. A raised
        :class:`~allstak.transport.AllStakAuthError` (401) aborts the drain.

        Returns the number of entries removed. Never raises.
        """
        if not self.available():
            return 0
        removed = 0
        try:
            files = self._list_entries()
        except Exception:
            return 0
        for fpath in files:
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    record = json.load(fh)
            except Exception:
                # Corrupt / partial file — drop it so it can't wedge the drain.
                self._safe_remove(fpath)
                continue
            path = record.get("path")
            payload = record.get("payload")
            if not isinstance(path, str) or not isinstance(payload, dict):
                self._safe_remove(fpath)
                continue
            try:
                status, _body = send_fn(path, payload)
            except Exception as exc:
                # AllStakAuthError (401) or any other transport failure. On a
                # 401 the SDK is disabled — stop draining, keep the entry.
                if exc.__class__.__name__ == "AllStakAuthError":
                    logger.debug("[AllStak] spool drain stopped: SDK disabled (401)")
                    break
                logger.debug("[AllStak] spool drain deferred for %s: %s", path, exc)
                continue
            if self._is_terminal(status):
                self._safe_remove(fpath)
                removed += 1
            else:
                # 429 / 5xx without an exception — keep for next boot.
                logger.debug("[AllStak] spool entry kept (status=%s) %s", status, path)
        return removed

    def drain_async(
        self, send_fn: Callable[[str, Dict[str, Any]], Tuple[int, Any]]
    ) -> Optional[threading.Thread]:
        """Run :meth:`drain` on a daemon thread so init never blocks. Returns the
        thread (or ``None`` when persistence is unavailable)."""
        if not self.available():
            return None

        def worker() -> None:
            try:
                count = self.drain(send_fn)
                if count:
                    logger.debug("[AllStak] spool drained %d persisted event(s)", count)
            except Exception as exc:  # pragma: no cover — drain is already guarded
                logger.debug("[AllStak] spool drain worker failed: %s", exc)

        thread = threading.Thread(target=worker, name="allstak-spool-drain", daemon=True)
        thread.start()
        return thread

    # ------------------------------------------------------------------
    # Introspection (used by tests / diagnostics)
    # ------------------------------------------------------------------

    def count(self) -> int:
        """Number of persisted entries currently on disk."""
        if self._unavailable:
            return 0
        try:
            return len(self._list_entries())
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _is_terminal(status: int) -> bool:
        """A response is terminal (remove the entry) when accepted (2xx) or
        permanently rejected (4xx other than 429). 429 and 5xx are transient."""
        if 200 <= status < 300:
            return True
        if 400 <= status < 500 and status != 429:
            return True
        return False

    def _list_entries(self) -> List[str]:
        """Spool entry paths sorted oldest-first by their millisecond prefix."""
        names = [n for n in os.listdir(self._dir) if n.endswith(".json")]
        names.sort()  # 13-digit zero-padded ts prefix sorts chronologically
        return [os.path.join(self._dir, n) for n in names]

    def _enforce_bounds_locked(self) -> None:
        """Drop oldest entries until count / bytes / age caps are satisfied.

        Must be called while holding ``self._lock``. Fully guarded.
        """
        try:
            entries = self._list_entries()
        except Exception:
            return

        now = time.time()
        # 1. Age: drop anything older than max_age_s (mtime is a safe proxy).
        if self._max_age_s > 0:
            survivors: List[str] = []
            for fpath in entries:
                try:
                    age = now - os.path.getmtime(fpath)
                except Exception:
                    survivors.append(fpath)
                    continue
                if age > self._max_age_s:
                    self._safe_remove(fpath)
                else:
                    survivors.append(fpath)
            entries = survivors

        # 2. Count: drop oldest beyond max_events.
        while len(entries) > self._max_events:
            self._safe_remove(entries.pop(0))

        # 3. Bytes: drop oldest until total size fits the budget.
        try:
            sizes = [(f, os.path.getsize(f)) for f in entries]
        except Exception:
            return
        total = sum(s for _f, s in sizes)
        idx = 0
        while total > self._max_bytes and idx < len(sizes):
            fpath, size = sizes[idx]
            self._safe_remove(fpath)
            total -= size
            idx += 1

    @staticmethod
    def _safe_remove(fpath: str) -> None:
        try:
            os.remove(fpath)
        except FileNotFoundError:
            pass
        except Exception as exc:  # pragma: no cover — best effort
            logger.debug("[AllStak] spool remove failed (%s): %s", fpath, exc)

    @staticmethod
    def _cleanup_tmp(tmp: str) -> None:
        try:
            os.remove(tmp)
        except Exception:
            pass


__all__ = [
    "EventSpool",
    "PERSISTABLE_PATHS",
    "is_persistable_path",
    "default_spool_dir",
]
