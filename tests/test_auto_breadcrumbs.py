"""Tests for auto-breadcrumb instrumentation idempotency (no real network)."""
from __future__ import annotations

import requests

from allstak.integrations import auto_breadcrumbs
from allstak.integrations.auto_breadcrumbs import (
    instrument_requests,
    _BREADCRUMB_PATCH_MARKER,
)


def _restore(original_send):
    requests.Session.send = original_send


def test_instrument_requests_is_idempotent():
    """Second call must be a no-op — Session.send wrapped exactly once."""
    original_send = requests.Session.send
    try:
        # Sanity: a pristine send is not yet marked.
        assert not getattr(original_send, _BREADCRUMB_PATCH_MARKER, False)

        crumbs = []

        def add_breadcrumb(**kwargs):
            crumbs.append(kwargs)

        instrument_requests(add_breadcrumb)
        first_wrapped = requests.Session.send
        assert getattr(first_wrapped, _BREADCRUMB_PATCH_MARKER, False) is True

        # Re-init / both-paths-active: calling again must not re-wrap.
        instrument_requests(add_breadcrumb)
        second_wrapped = requests.Session.send
        assert second_wrapped is first_wrapped
    finally:
        _restore(original_send)


def test_single_request_produces_one_breadcrumb_layer():
    """One outbound request yields exactly one HTTP breadcrumb after re-init."""
    original_send = requests.Session.send
    try:
        crumbs = []

        def add_breadcrumb(**kwargs):
            crumbs.append(kwargs)

        # Stub the real network at the bottom of the stack.
        class _FakeResponse:
            status_code = 200

        sent = {"count": 0}

        def fake_send(self, request, **kwargs):
            sent["count"] += 1
            return _FakeResponse()

        requests.Session.send = fake_send

        # Instrument twice to simulate re-init / double-path; must stay single-layer.
        instrument_requests(add_breadcrumb)
        instrument_requests(add_breadcrumb)

        req = requests.Request("GET", "http://example.com/x?token=secret").prepare()
        requests.Session().send(req)

        # Underlying transport called exactly once, one breadcrumb recorded.
        assert sent["count"] == 1
        http_crumbs = [c for c in crumbs if c.get("type") == "http"]
        assert len(http_crumbs) == 1
        # Query string with sensitive param is stripped.
        assert http_crumbs[0]["data"]["url"] == "http://example.com/x"
    finally:
        _restore(original_send)
