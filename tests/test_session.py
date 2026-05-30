"""Tests for release-health session tracking (start/end + crash-free status).

Covers the "one session per process" lifecycle implemented in
``allstak.session`` and its wiring into ``AllStakClient``:

* start payload shape + that sessions are never sampled,
* end payload shape + status transitions ok -> errored -> crashed,
* fail-open behaviour (network errors never propagate),
* the ``enable_auto_session_tracking=False`` opt-out,
* the active session id is attached to captured error/event payloads.
"""

from __future__ import annotations

import os
import tempfile
import threading
from typing import Any, Dict

import pytest

from allstak.config import AllStakConfig
from allstak.session import Session, SessionStatus, SessionTracker


class FakeTransport:
    """Records every payload handed to post(). Mirrors the repo test style."""

    def __init__(self, disabled: bool = False, raise_on: str | None = None):
        self.posts: list[tuple[str, Dict[str, Any]]] = []
        self._disabled = disabled
        self._raise_on = raise_on

    def post(self, path: str, payload: Dict[str, Any]):
        if self._raise_on and self._raise_on in path:
            raise RuntimeError("network down")
        self.posts.append((path, payload))
        return 202, {}

    def is_disabled(self) -> bool:
        return self._disabled


def _wait_for_start(transport: FakeTransport, timeout: float = 2.0) -> None:
    """The start POST runs on a daemon thread — give it a moment to land."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(p[0].endswith("/sessions/start") for p in transport.posts):
            return
        time.sleep(0.01)


def _tracker(**config_kwargs) -> tuple[SessionTracker, FakeTransport]:
    transport = FakeTransport(disabled=config_kwargs.pop("_disabled", False),
                              raise_on=config_kwargs.pop("_raise_on", None))
    state_path = config_kwargs.pop("_state_path", None)
    if state_path is None:
        fd, state_path = tempfile.mkstemp(prefix="allstak-session-test-", suffix=".json")
        os.close(fd)
        os.unlink(state_path)
    config = AllStakConfig(api_key="ask_test", release="v9.9.9", **config_kwargs)
    return SessionTracker(transport, config, state_path=state_path), transport


# --------------------------------------------------------------------------
# Session model — status transitions
# --------------------------------------------------------------------------

def test_session_starts_ok():
    s = Session()
    assert s.status == SessionStatus.OK
    assert s.error_count == 0
    assert s.id  # a non-empty uuid


def test_record_error_escalates_ok_to_errored():
    s = Session()
    s.record_error()
    assert s.status == SessionStatus.ERRORED
    assert s.error_count == 1


def test_record_crash_escalates_to_crashed_and_is_terminal():
    s = Session()
    s.record_error()
    s.record_crash()
    assert s.status == SessionStatus.CRASHED
    # A subsequent handled error must NOT downgrade crashed back to errored.
    s.record_error()
    assert s.status == SessionStatus.CRASHED


def test_duration_is_non_negative():
    s = Session()
    assert s.duration_ms() >= 0


# --------------------------------------------------------------------------
# SessionTracker — start payload shape
# --------------------------------------------------------------------------

def test_start_posts_expected_payload_shape():
    tracker, transport = _tracker(environment="production")
    session = tracker.start()
    _wait_for_start(transport)

    assert len(transport.posts) == 1
    path, payload = transport.posts[0]
    assert path == "/ingest/v1/sessions/start"
    assert payload["sessionId"] == session.id
    assert payload["release"] == "v9.9.9"
    assert payload["environment"] == "production"
    assert payload["userId"] is None
    assert payload["sdkName"] == "allstak-python"
    assert payload["sdkVersion"] is not None
    assert payload["platform"] == "python"
    # Exact key set — no extras leak onto the wire.
    assert set(payload.keys()) == {
        "sessionId", "release", "environment", "userId",
        "sdkName", "sdkVersion", "platform",
    }


def test_start_is_idempotent():
    tracker, transport = _tracker()
    first = tracker.start()
    second = tracker.start()
    _wait_for_start(transport)
    assert first is second
    # Only ONE start POST regardless of repeat calls.
    assert sum(1 for p in transport.posts if p[0].endswith("/sessions/start")) == 1


def test_start_falls_back_to_sdk_version_when_no_release():
    # With auto-detect off and no release, _effective_release falls back to
    # the SDK version constant; release must never be empty on the wire.
    # Route through the helper so persistence uses an isolated temp state path
    # (a stale session in the SDK's default state file would otherwise be
    # recovered as "abnormal" and emit a spurious sessions/end POST).
    tracker, transport = _tracker(auto_detect_release=False)
    tracker._config.release = None
    tracker.start()
    _wait_for_start(transport)
    assert len(transport.posts) == 1
    _, payload = transport.posts[0]
    assert payload["release"]  # non-empty fallback


def test_start_skips_network_when_transport_disabled():
    tracker, transport = _tracker(_disabled=True)
    session = tracker.start()
    _wait_for_start(transport, timeout=0.3)
    # No POST, but the in-memory session still exists for status tracking.
    assert transport.posts == []
    assert session is not None
    assert tracker.current() is session


def test_start_is_fail_open_on_network_error():
    # raise_on="sessions/start" makes the daemon worker raise; init must not.
    tracker, transport = _tracker(_raise_on="sessions/start")
    session = tracker.start()
    _wait_for_start(transport, timeout=0.3)
    assert session is not None
    assert tracker.current() is session  # tracker survives the failure


# --------------------------------------------------------------------------
# SessionTracker — end payload shape + status
# --------------------------------------------------------------------------

def test_end_posts_status_ok_when_no_errors():
    tracker, transport = _tracker()
    tracker.start()
    _wait_for_start(transport)
    tracker.end(None)

    end_posts = [p for p in transport.posts if p[0].endswith("/sessions/end")]
    assert len(end_posts) == 1
    path, payload = end_posts[0]
    assert path == "/ingest/v1/sessions/end"
    assert payload["status"] == SessionStatus.OK
    assert isinstance(payload["durationMs"], int)
    assert payload["durationMs"] >= 0
    assert set(payload.keys()) == {"sessionId", "durationMs", "status"}


def test_end_reports_errored_after_record_error():
    tracker, transport = _tracker()
    session = tracker.start()
    _wait_for_start(transport)
    tracker.record_error()
    tracker.end(None)

    _, payload = [p for p in transport.posts if p[0].endswith("/sessions/end")][0]
    assert payload["sessionId"] == session.id
    assert payload["status"] == SessionStatus.ERRORED


def test_end_reports_crashed_after_record_crash():
    tracker, transport = _tracker()
    tracker.start()
    _wait_for_start(transport)
    tracker.record_error()   # ok -> errored
    tracker.record_crash()   # errored -> crashed
    tracker.end(None)

    _, payload = [p for p in transport.posts if p[0].endswith("/sessions/end")][0]
    assert payload["status"] == SessionStatus.CRASHED


def test_end_is_idempotent():
    tracker, transport = _tracker()
    tracker.start()
    _wait_for_start(transport)
    tracker.end(None)
    tracker.end(None)
    end_posts = [p for p in transport.posts if p[0].endswith("/sessions/end")]
    assert len(end_posts) == 1
    assert tracker.current() is None


def test_end_is_fail_open_on_network_error():
    tracker, transport = _tracker(_raise_on="sessions/end")
    tracker.start()
    _wait_for_start(transport)
    # Must not raise even though the end POST blows up.
    tracker.end(None)
    assert tracker.current() is None


def test_record_after_end_is_noop():
    tracker, transport = _tracker()
    tracker.start()
    _wait_for_start(transport)
    tracker.end(None)
    # No active session — these must be safe no-ops.
    tracker.record_error()
    tracker.record_crash()
    assert tracker.current() is None


def test_clean_shutdown_does_not_report_abnormal_on_next_start(tmp_path):
    state_path = str(tmp_path / "session.json")
    tracker, _ = _tracker(_state_path=state_path)
    tracker.start()
    tracker.end(None)

    next_tracker, transport = _tracker(_state_path=state_path)
    next_tracker.start()
    _wait_for_start(transport)

    assert [p for p in transport.posts if p[0].endswith("/sessions/end")] == []
    assert len([p for p in transport.posts if p[0].endswith("/sessions/start")]) == 1


def test_previous_open_session_is_reported_abnormal_on_next_start(tmp_path):
    state_path = str(tmp_path / "session.json")
    tracker, _ = _tracker(_state_path=state_path)
    session = tracker.start()

    next_tracker, transport = _tracker(_state_path=state_path)
    next_tracker.start()
    end_posts = [p for p in transport.posts if p[0].endswith("/sessions/end")]

    assert len(end_posts) == 1
    _, payload = end_posts[0]
    assert payload["sessionId"] == session.id
    assert payload["status"] == SessionStatus.ABNORMAL


def test_previous_crashed_session_is_reported_crashed_on_next_start(tmp_path):
    state_path = str(tmp_path / "session.json")
    tracker, _ = _tracker(_state_path=state_path)
    session = tracker.start()
    tracker.record_crash()

    next_tracker, transport = _tracker(_state_path=state_path)
    next_tracker.start()
    end_posts = [p for p in transport.posts if p[0].endswith("/sessions/end")]

    assert len(end_posts) == 1
    _, payload = end_posts[0]
    assert payload["sessionId"] == session.id
    assert payload["status"] == SessionStatus.CRASHED


def test_corrupt_session_state_is_removed_safely(tmp_path):
    state_path = tmp_path / "session.json"
    state_path.write_text("{not-json", encoding="utf-8")
    tracker, transport = _tracker(_state_path=str(state_path))

    tracker.start()
    _wait_for_start(transport)

    assert [p for p in transport.posts if p[0].endswith("/sessions/end")] == []
    assert len([p for p in transport.posts if p[0].endswith("/sessions/start")]) == 1


def test_recovered_abnormal_session_is_not_reported_twice(tmp_path):
    state_path = str(tmp_path / "session.json")
    tracker, _ = _tracker(_state_path=state_path)
    tracker.start()

    second, transport = _tracker(_state_path=state_path)
    second.start()
    second.end(None)

    third, third_transport = _tracker(_state_path=state_path)
    third.start()

    abnormal_second = [
        p for p in transport.posts
        if p[0].endswith("/sessions/end") and p[1]["status"] == SessionStatus.ABNORMAL
    ]
    abnormal_third = [
        p for p in third_transport.posts
        if p[0].endswith("/sessions/end") and p[1]["status"] == SessionStatus.ABNORMAL
    ]
    assert len(abnormal_second) == 1
    assert abnormal_third == []


# --------------------------------------------------------------------------
# Client wiring — opt-out, session-id attachment, status transitions
# --------------------------------------------------------------------------

class _RecordingErrorTransport:
    """Transport that records error POSTs and acks them with an event id."""

    def __init__(self):
        self.posts: list[tuple[str, Dict[str, Any]]] = []

    def post(self, path: str, payload: Dict[str, Any]):
        self.posts.append((path, payload))
        return 202, {"data": {"id": "evt_xyz"}}

    def is_disabled(self) -> bool:
        return False


def _client_with_tracker(monkeypatch, enable: bool = True):
    """Build a real AllStakClient with a recording transport and a force-on
    session tracker (the unit-test runtime guard is bypassed for this test)."""
    from allstak import client as client_mod

    monkeypatch.setattr(client_mod.AllStakClient, "_is_test_runtime", staticmethod(lambda: False))

    rec = _RecordingErrorTransport()
    config = AllStakConfig(
        api_key="ask_test",
        release="v1.2.3",
        environment="staging",
        offline_queue_dir=tempfile.mkdtemp(prefix="allstak-session-client-test-"),
        install_excepthook=False,
        install_threading_excepthook=False,
        auto_breadcrumbs=False,
        enable_auto_session_tracking=enable,
    )
    c = client_mod.AllStakClient(config)
    # Swap the transport used by the error module + tracker for the recorder so
    # every POST is observable (init already created modules off the real one).
    c._errors._transport = rec
    if c._session_tracker is not None:
        c._session_tracker._transport = rec
    return c, rec


def test_opt_out_disables_session_tracker(monkeypatch):
    c, _ = _client_with_tracker(monkeypatch, enable=False)
    assert c._session_tracker is None


def test_enabled_creates_session_tracker(monkeypatch):
    c, _ = _client_with_tracker(monkeypatch, enable=True)
    assert c._session_tracker is not None
    assert c._active_session_id() is not None


def test_capture_exception_attaches_session_id_and_marks_errored(monkeypatch):
    c, rec = _client_with_tracker(monkeypatch, enable=True)
    sid = c._active_session_id()

    try:
        raise ValueError("handled boom")
    except ValueError as e:
        c.capture_exception(e)  # handled (no mechanism) -> errored

    error_posts = [p for p in rec.posts if p[0].endswith("/errors")]
    assert len(error_posts) == 1
    _, payload = error_posts[0]
    assert payload["sessionId"] == sid
    assert c._session_tracker.current().status == SessionStatus.ERRORED


def test_unhandled_capture_marks_crashed(monkeypatch):
    c, rec = _client_with_tracker(monkeypatch, enable=True)
    try:
        raise RuntimeError("fatal boom")
    except RuntimeError as e:
        # mechanism.handled=False mirrors the excepthook unhandled path.
        c.capture_exception(e, mechanism={"type": "excepthook", "handled": False})

    assert c._session_tracker.current().status == SessionStatus.CRASHED


def test_shutdown_ends_session_with_final_status(monkeypatch):
    c, rec = _client_with_tracker(monkeypatch, enable=True)
    try:
        raise ValueError("boom")
    except ValueError as e:
        c.capture_exception(e)

    c._shutdown()
    end_posts = [p for p in rec.posts if p[0].endswith("/sessions/end")]
    assert len(end_posts) == 1
    _, payload = end_posts[0]
    assert payload["status"] == SessionStatus.ERRORED
