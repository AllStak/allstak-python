"""Diagnostics API tests."""

from __future__ import annotations

from allstak.client import AllStakClient
from allstak.config import AllStakConfig


def _client(tmp_path) -> AllStakClient:
    return AllStakClient(
        AllStakConfig(
            api_key="ask_test",
            offline_queue_dir=str(tmp_path / "spool"),
            enable_auto_session_tracking=False,
            auto_register_release=False,
            auto_breadcrumbs=False,
            capture_logs=False,
            install_excepthook=False,
            install_threading_excepthook=False,
        )
    )


def test_client_diagnostics_are_privacy_safe(tmp_path):
    client = _client(tmp_path)
    try:
        client.add_breadcrumb(
            "log",
            "user password is secret-value",
            data={"Authorization": "Bearer secret-value"},
        )
        client.set_user(user_id="usr_123", email="person@example.com", ip="10.1.2.3")
        client.set_trace_id("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
        with client.start_span("diagnostics.test"):
            snapshot = client.get_diagnostics()

        raw = str(snapshot)
        assert "secret-value" not in raw
        assert "person@example.com" not in raw
        assert "10.1.2.3" not in raw
        assert "Authorization" not in raw
        assert snapshot["breadcrumbCount"] == 1
        assert snapshot["activeTraceCount"] == 1
        assert snapshot["activeSpanCount"] == 1
        assert snapshot["queueSize"] == 0
        assert "eventsCaptured" in snapshot
        assert "eventsDropped" in snapshot
    finally:
        client._shutdown()


def test_module_level_get_diagnostics_before_init_is_empty(monkeypatch):
    import allstak
    import allstak.client as client_mod

    monkeypatch.setattr(client_mod, "_client", None)
    assert allstak.get_diagnostics() == {}
