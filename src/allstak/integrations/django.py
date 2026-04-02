"""
Django middleware integration for AllStak.

Records every inbound HTTP request/response automatically.

Setup in settings.py::

    MIDDLEWARE = [
        "allstak.integrations.django.AllStakMiddleware",
        # ... your other middleware
    ]

    ALLSTAK = {
        "api_key": "ask_live_...",
        "host": "http://localhost:8080",
        "environment": "production",
    }

Or configure programmatically before the first request.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

try:
    from django.conf import settings
    from django.http import HttpRequest, HttpResponse
    _DJANGO_AVAILABLE = True
except ImportError:
    _DJANGO_AVAILABLE = False


class AllStakMiddleware:
    """
    Django middleware that tracks every inbound HTTP request.

    Automatically initializes the AllStak SDK on first use if
    ``ALLSTAK`` settings block is present.
    """

    def __init__(self, get_response: Callable) -> None:
        if not _DJANGO_AVAILABLE:
            raise ImportError("Django is not installed")
        self.get_response = get_response
        self._ensure_initialized()

    def __call__(self, request: "HttpRequest") -> "HttpResponse":
        start_ms = time.monotonic() * 1000
        start_ts = self._now_iso()

        response = self.get_response(request)

        try:
            import allstak
            client = allstak.get_client()
            if client:
                duration = int(time.monotonic() * 1000 - start_ms)
                path = request.path  # already without query string
                body_len = int(request.META.get("CONTENT_LENGTH") or 0)
                resp_len = len(response.content) if hasattr(response, "content") else 0

                client.http.record(
                    direction="inbound",
                    method=request.method or "GET",
                    host=request.get_host() or "localhost",
                    path=path,
                    status_code=response.status_code,
                    duration_ms=duration,
                    request_size=body_len,
                    response_size=resp_len,
                    timestamp=start_ts,
                )
        except Exception:
            pass

        return response

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _ensure_initialized() -> None:
        try:
            import allstak
            if allstak.get_client() is not None:
                return
            cfg = getattr(settings, "ALLSTAK", {})
            if cfg.get("api_key"):
                allstak.init(**cfg)
        except Exception:
            pass
