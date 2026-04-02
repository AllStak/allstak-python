"""
Cron job monitoring module — POST /ingest/v1/heartbeat.

Usage pattern:
    with allstak.cron.job("my-job-slug") as job:
        # do work...
        # heartbeat is sent automatically on exit (success or failure)

Or manually:
    handle = allstak.cron.start("my-job-slug")
    try:
        do_work()
        allstak.cron.finish(handle, "success")
    except Exception as e:
        allstak.cron.finish(handle, "failed", message=str(e))
        raise  # always rethrow — never swallow job errors

IMPORTANT: The slug must match an existing CronMonitor in the AllStak
management console.  Sending a heartbeat for an unknown slug returns 404.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator, Optional

from ..config import AllStakConfig
from ..models.heartbeat import HeartbeatPayload
from ..transport import AllStakAuthError, AllStakTransportError, HttpTransport

logger = logging.getLogger("allstak.sdk")

_INGEST_PATH = "/ingest/v1/heartbeat"


@dataclass
class JobHandle:
    """Returned by ``CronModule.start()`` — tracks job start time."""
    slug: str
    start_ms: float


class CronModule:
    """
    Sends heartbeat pings to AllStak cron monitors.

    Heartbeats are sent immediately (no buffering — each ping signals
    a job completion and may trigger alerts if status is "failed").
    """

    def __init__(self, transport: HttpTransport, config: AllStakConfig) -> None:
        self._transport = transport
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, slug: str) -> JobHandle:
        """
        Record the start time for a cron job.

        :param slug: The CronMonitor slug (must exist in AllStak console).
        :returns: A ``JobHandle`` to pass to ``finish()``.
        """
        return JobHandle(slug=slug, start_ms=time.monotonic() * 1000)

    def finish(
        self,
        handle: JobHandle,
        status: str,
        *,
        message: Optional[str] = None,
    ) -> bool:
        """
        Send a heartbeat ping for a completed job.

        :param handle: The handle returned by ``start()``.
        :param status: ``"success"`` or ``"failed"``.
        :param message: Optional human-readable result or error message.
        :returns: True if the ping was accepted, False otherwise.
        """
        duration_ms = int(time.monotonic() * 1000 - handle.start_ms)
        return self._send_ping(handle.slug, status, duration_ms, message)

    @contextmanager
    def job(
        self,
        slug: str,
        *,
        success_message: Optional[str] = None,
    ) -> Generator[JobHandle, None, None]:
        """
        Context manager that times a job and sends the heartbeat automatically.

        On normal exit → status ``"success"``.
        On exception → status ``"failed"`` with ``str(exc)`` as message.
        The exception is **always re-raised** — the SDK never swallows job errors.

        Usage::

            with allstak.cron.job("daily-report") as j:
                run_daily_report()
        """
        handle = self.start(slug)
        try:
            yield handle
            self.finish(handle, "success", message=success_message)
        except Exception as exc:
            self.finish(handle, "failed", message=str(exc))
            raise  # ALWAYS re-raise — never swallow job errors

    def ping(
        self,
        slug: str,
        status: str,
        duration_ms: int,
        *,
        message: Optional[str] = None,
    ) -> bool:
        """
        Send a raw heartbeat ping without a JobHandle.

        :param slug: CronMonitor slug.
        :param status: ``"success"`` or ``"failed"``.
        :param duration_ms: Job execution duration in milliseconds.
        :param message: Optional result message.
        :returns: True if accepted (202), False otherwise.
        """
        return self._send_ping(slug, status, duration_ms, message)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _send_ping(
        self,
        slug: str,
        status: str,
        duration_ms: int,
        message: Optional[str],
    ) -> bool:
        try:
            payload = HeartbeatPayload(
                slug=slug,
                status=status,
                duration_ms=duration_ms,
                message=message,
            )
            status_code, body = self._transport.post(
                _INGEST_PATH, payload.to_dict()
            )
            if status_code == 202:
                return True
            if status_code == 404:
                logger.warning(
                    "[AllStak] Cron ping rejected — no CronMonitor found with slug '%s'. "
                    "Create the monitor in the AllStak console first.",
                    slug,
                )
                return False
            logger.debug(
                "[AllStak] Cron ping returned %d: %s", status_code, body
            )
            return False
        except AllStakAuthError:
            raise
        except AllStakTransportError as exc:
            logger.debug("[AllStak] Cron ping transport error: %s", exc)
            return False
        except Exception as exc:
            logger.debug("[AllStak] Cron ping failed silently: %s", exc)
            return False
