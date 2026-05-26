"""Tests for the synchronous ``requests`` trace-propagation integration."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

import allstak
from allstak import client as allstak_client
from allstak.integrations import requests as allstak_requests

TRACE_ID = "7f3ac1d92b8e4a6f"


@pytest.fixture
def installed_requests():
    """Install the integration over a faked Session.send (no real network)."""
    original_send = requests.sessions.Session.send
    fake_send = MagicMock(return_value=MagicMock(status_code=200))
    requests.sessions.Session.send = fake_send  # captured as the "original" by install

    allstak_requests._INSTALLED = False
    allstak_client._client = None
    allstak_client._initialized_once = False
    allstak.init(api_key="ask_test", host="http://allstak.test")

    yield fake_send

    requests.sessions.Session.send = original_send
    allstak_requests._INSTALLED = False
    allstak_client._client = None
    allstak_client._initialized_once = False


def test_injects_trace_headers_on_outbound_request(installed_requests):
    allstak.set_trace_id(TRACE_ID)

    req = requests.Request("GET", "http://example.com/x").prepare()
    requests.Session().send(req)

    assert installed_requests.called
    assert req.headers.get("x-allstak-trace-id") == TRACE_ID
    assert req.headers.get("traceparent", "").startswith(f"00-{TRACE_ID}-")
    assert f"allstak-trace_id={TRACE_ID}" in req.headers.get("baggage", "")


def test_does_not_inject_for_own_ingest_host(installed_requests):
    allstak.set_trace_id(TRACE_ID)

    req = requests.Request("GET", "http://allstak.test/ingest/v1/errors").prepare()
    requests.Session().send(req)

    assert installed_requests.called
    assert req.headers.get("traceparent") is None
    assert req.headers.get("x-allstak-trace-id") is None


def test_respects_user_supplied_headers(installed_requests):
    allstak.set_trace_id(TRACE_ID)

    req = requests.Request(
        "GET", "http://example.com/x", headers={"traceparent": "EXISTING"}
    ).prepare()
    requests.Session().send(req)

    # set-if-missing: user's traceparent is preserved.
    assert req.headers.get("traceparent") == "EXISTING"
