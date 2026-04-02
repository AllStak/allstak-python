"""
Flask extension integration for AllStak.

Records every inbound HTTP request/response automatically.

Setup::

    from flask import Flask
    from allstak.integrations.flask import AllStakFlask

    app = Flask(__name__)
    AllStakFlask(app)

Or use the factory pattern::

    allstak_ext = AllStakFlask()
    allstak_ext.init_app(app)
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

try:
    import flask
    _FLASK_AVAILABLE = True
except ImportError:
    _FLASK_AVAILABLE = False


class AllStakFlask:
    """Flask extension that records inbound HTTP request telemetry."""

    def __init__(self, app: Optional[object] = None) -> None:
        if not _FLASK_AVAILABLE:
            raise ImportError("Flask is not installed")
        if app is not None:
            self.init_app(app)

    def init_app(self, app: object) -> None:
        """Register before/after request hooks on *app*."""
        app.before_request(self._before_request)  # type: ignore[attr-defined]
        app.after_request(self._after_request)    # type: ignore[attr-defined]

    @staticmethod
    def _before_request() -> None:
        flask.g._allstak_start_ms = time.monotonic() * 1000
        flask.g._allstak_start_ts = AllStakFlask._now_iso()

    @staticmethod
    def _after_request(response: object) -> object:
        try:
            import allstak
            client = allstak.get_client()
            if client:
                start_ms = getattr(flask.g, "_allstak_start_ms", None)
                start_ts = getattr(flask.g, "_allstak_start_ts", AllStakFlask._now_iso())
                duration = int(time.monotonic() * 1000 - start_ms) if start_ms else 0

                req = flask.request
                path = req.path
                body_len = req.content_length or 0
                resp_len = (
                    int(response.headers.get("Content-Length", 0))  # type: ignore
                    if hasattr(response, "headers")
                    else 0
                )
                status = getattr(response, "status_code", 0)

                client.http.record(
                    direction="inbound",
                    method=req.method,
                    host=req.host or "localhost",
                    path=path,
                    status_code=status,
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
