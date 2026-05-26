"""
HTTP transport layer with retry, exponential backoff, and timeout enforcement.

Contract from SDK guidelines:
- Connection timeout: 3s
- Read timeout: 3s
- Retry on: 5xx, 429, connection timeout, network error
- No retry on: 400, 401, 403, 422
- Backoff: 1s → 2s → 4s → 8s  (+jitter 0–500ms each)
- 429 / 503: honor ``Retry-After`` header (seconds or HTTP-date), clamped to 300s
- Max 5 attempts
- On 401: disable SDK
"""

from __future__ import annotations

import logging
import random
import sys
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger("allstak.sdk")

# HTTP status codes we NEVER retry — these 4xx are client errors
# Guidelines explicitly list 400, 401, 403, 422 but 404 is equally non-retryable.
# 429 (Too Many Requests) is explicitly NOT in this set — it is retryable with
# backpressure (Retry-After), so it must not be silently dropped.
_NO_RETRY_4XX = True  # 4xx responses are non-retryable, EXCEPT 429
_NO_RETRY_STATUSES = frozenset({400, 401, 403, 404, 422})

# Statuses that honor the ``Retry-After`` response header for backpressure.
_RETRY_AFTER_STATUSES = frozenset({429, 503})

# Maximum delay (seconds) we will ever wait, even if Retry-After asks for more.
_MAX_RETRY_AFTER = 300.0

# Backoff delays (seconds) for attempts 2-5
_BACKOFF_DELAYS = [1.0, 2.0, 4.0, 8.0]


def parse_retry_after(header_value: Optional[str], now: Optional[datetime] = None) -> float:
    """
    Parse an HTTP ``Retry-After`` header value into a delay in seconds.

    Accepts either delta-seconds (an integer, e.g. ``"120"``) or an HTTP-date
    (e.g. ``"Wed, 21 Oct 2015 07:28:00 GMT"``), per RFC 7231 §7.1.3.

    Returns a non-negative float of seconds to wait. Returns ``0.0`` when the
    header is absent, empty, or unparseable so the caller can fall back to its
    normal exponential backoff. The result is clamped to ``[0, 300]``.

    ``now`` is injectable for testing; defaults to the current UTC time.
    """
    if header_value is None:
        return 0.0
    value = header_value.strip()
    if not value:
        return 0.0

    # Form 1: delta-seconds (an integer count of seconds).
    try:
        seconds = float(int(value))
        if seconds < 0:
            return 0.0
        return min(seconds, _MAX_RETRY_AFTER)
    except (ValueError, TypeError):
        pass

    # Form 2: HTTP-date — wait until that absolute time.
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return 0.0
    if when is None:
        return 0.0

    reference = now if now is not None else datetime.now(timezone.utc)
    # Normalize naive datetimes (HTTP-dates without tz info are GMT/UTC).
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)

    delta = (when - reference).total_seconds()
    if delta <= 0:
        return 0.0
    return min(delta, _MAX_RETRY_AFTER)


class AllStakTransportError(Exception):
    """Raised internally when all retry attempts are exhausted."""


class AllStakAuthError(Exception):
    """Raised on 401 — SDK should disable itself."""


class HttpTransport:
    """
    Thread-safe, synchronous HTTP transport with retry/backoff.

    Uses httpx for all HTTP calls.  All public methods are safe to
    call from any thread — each call creates its own client with
    the configured timeouts.
    """

    def __init__(
        self,
        api_key: str,
        host: str,
        connect_timeout: float = 3.0,
        read_timeout: float = 3.0,
        max_retries: int = 5,
        debug: bool = False,
    ) -> None:
        self._api_key = api_key
        self._host = host.rstrip("/")
        self._timeout = httpx.Timeout(
            connect=connect_timeout,
            read=read_timeout,
            write=read_timeout,
            pool=connect_timeout,
        )
        self._max_retries = max(1, min(max_retries, 5))
        self._debug = debug
        self._disabled = False  # set True on 401

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def post(
        self,
        path: str,
        payload: Dict[str, Any],
    ) -> Tuple[int, Dict[str, Any]]:
        """
        POST ``payload`` to ``{host}{path}`` with retry/backoff.

        Returns ``(status_code, response_body_dict)``.
        Raises ``AllStakAuthError`` on 401 (caller should disable SDK).
        Raises ``AllStakTransportError`` when all retries are exhausted.
        """
        if self._disabled:
            raise AllStakAuthError("SDK is disabled due to invalid API key")

        url = f"{self._host}{path}"
        headers = {
            "Content-Type": "application/json",
            "X-AllStak-Key": self._api_key,
        }

        last_exc: Optional[Exception] = None
        last_status: int = 0
        # Delay requested by a Retry-After header on the most recent response;
        # 0.0 means "fall back to exponential backoff".
        retry_after_delay: float = 0.0

        for attempt in range(1, self._max_retries + 1):
            retry_after_delay = 0.0
            try:
                if self._debug:
                    logger.debug(
                        "[AllStak] POST %s attempt %d payload=%s",
                        url,
                        attempt,
                        payload,
                    )

                with httpx.Client(timeout=self._timeout) as client:
                    resp = client.post(url, json=payload, headers=headers)

                last_status = resp.status_code
                body: Dict[str, Any] = {}
                try:
                    body = resp.json()
                except Exception:
                    body = {"raw": resp.text}

                if self._debug:
                    logger.debug(
                        "[AllStak] Response %d: %s", last_status, body
                    )

                # 401 → disable SDK immediately, no retry
                if last_status == 401:
                    self._disabled = True
                    logger.warning(
                        "[AllStak] SDK disabled: invalid API key (401). "
                        "Check your X-AllStak-Key configuration."
                    )
                    raise AllStakAuthError(
                        "AllStak SDK: invalid API key — SDK disabled for this session"
                    )

                # 4xx client errors → no retry (except 401 handled above)
                if last_status in _NO_RETRY_STATUSES:
                    logger.debug(
                        "[AllStak] Non-retryable error %d: %s", last_status, body
                    )
                    return last_status, body

                # 2xx / 3xx → success
                if last_status < 400:
                    return last_status, body

                # 429 (rate limited) / 503 → retryable with backpressure.
                # Honor Retry-After if present; otherwise fall back to backoff.
                if last_status in _RETRY_AFTER_STATUSES:
                    retry_after_delay = parse_retry_after(
                        resp.headers.get("Retry-After")
                    )
                    logger.debug(
                        "[AllStak] Backpressure %d on attempt %d "
                        "(Retry-After=%s -> %.2fs)",
                        last_status,
                        attempt,
                        resp.headers.get("Retry-After"),
                        retry_after_delay,
                    )
                    # fall through to retry/backoff

                # 5xx → retryable, fall through to backoff

            except AllStakAuthError:
                raise
            except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError) as exc:
                last_exc = exc
                logger.debug(
                    "[AllStak] Network error on attempt %d: %s", attempt, exc
                )
            except Exception as exc:
                last_exc = exc
                logger.debug(
                    "[AllStak] Unexpected error on attempt %d: %s", attempt, exc
                )

            # Back off before next attempt (skip sleep after last attempt)
            if attempt < self._max_retries:
                if retry_after_delay > 0:
                    # Server told us how long to wait — honor it (already
                    # clamped to <= 300s) without adding jitter.
                    sleep_for = retry_after_delay
                else:
                    delay = _BACKOFF_DELAYS[min(attempt - 1, len(_BACKOFF_DELAYS) - 1)]
                    jitter = random.uniform(0, 0.5)
                    sleep_for = delay + jitter
                logger.debug(
                    "[AllStak] Retrying in %.2fs (attempt %d/%d)",
                    sleep_for,
                    attempt,
                    self._max_retries,
                )
                time.sleep(sleep_for)

        msg = (
            f"AllStak SDK: all {self._max_retries} attempts failed for POST {path}. "
            f"Last status: {last_status}. Last error: {last_exc}"
        )
        logger.debug(msg)
        raise AllStakTransportError(msg)

    def is_disabled(self) -> bool:
        """Returns True if the transport has been disabled (e.g. on 401)."""
        return self._disabled
