"""Tests for the offline / persistent event spool.

Covers the durability contract added in the offline-queue change:

* persist-on-send-failure (transport writes to the spool when retries exhaust),
* drain-and-resend-on-init (entries replay through the transport),
* scrub-before-persist (no secret value ever lands on disk),
* cap / eviction (oldest dropped by count and by bytes),
* session lifecycle calls are NEVER persisted,
* the opt-out flag (``offline_storage=False``) disables persistence,
* graceful no-op when the store directory is unavailable / unwritable.

The spool and its transport hook are entirely fail-open — none of these paths
may raise into the host application.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Tuple

import httpx
import pytest
import respx

from allstak.config import AllStakConfig
from allstak.spool import (
    EventSpool,
    default_spool_dir,
    is_persistable_path,
)
from allstak.transport import (
    AllStakAuthError,
    AllStakTransportError,
    HttpTransport,
)


BASE = "http://test-backend:8080"
KEY = "ask_test_key"

SECRET = "super-secret-bearer-token-value"


def _spool(tmp_path, **kwargs) -> EventSpool:
    return EventSpool(str(tmp_path / "spool"), **kwargs)


# ---------------------------------------------------------------------------
# Path allowlist
# ---------------------------------------------------------------------------


class TestPersistablePaths:
    def test_telemetry_paths_are_persistable(self):
        for path in (
            "/ingest/v1/errors",
            "/ingest/v1/logs",
            "/ingest/v1/spans",
            "/ingest/v1/http-requests",
            "/ingest/v1/db",
        ):
            assert is_persistable_path(path) is True

    def test_session_lifecycle_paths_are_not_persistable(self):
        assert is_persistable_path("/ingest/v1/sessions/start") is False
        assert is_persistable_path("/ingest/v1/sessions/end") is False

    def test_other_live_only_paths_are_not_persistable(self):
        assert is_persistable_path("/ingest/v1/heartbeat") is False
        assert is_persistable_path("/ingest/v1/releases") is False
        assert is_persistable_path("/ingest/v1/replay") is False

    def test_default_dir_is_per_backend(self):
        a = default_spool_dir("https://api.allstak.sa")
        b = default_spool_dir("http://localhost:8080")
        assert a != b
        assert "allstak-spool" in a


# ---------------------------------------------------------------------------
# Persist + scrub
# ---------------------------------------------------------------------------


class TestPersist:
    def test_persist_writes_one_file(self, tmp_path):
        spool = _spool(tmp_path)
        assert spool.persist("/ingest/v1/logs", {"message": "hi"}) is True
        assert spool.count() == 1

    def test_persist_skips_non_persistable_path(self, tmp_path):
        spool = _spool(tmp_path)
        assert spool.persist("/ingest/v1/sessions/start", {"sessionId": "x"}) is False
        assert spool.count() == 0

    def test_scrub_before_persist_no_secret_on_disk(self, tmp_path):
        spool = _spool(tmp_path)
        spool.persist(
            "/ingest/v1/errors",
            {
                "message": "boom",
                "metadata": {"authorization": f"Bearer {SECRET}", "password": SECRET},
                "user": {"email": "a@b.com", "token": SECRET},
            },
        )
        # Read every byte the spool wrote and assert the secret is absent.
        files = list((tmp_path / "spool").glob("*.json"))
        assert len(files) == 1
        raw = files[0].read_text(encoding="utf-8")
        assert SECRET not in raw
        assert "[REDACTED]" in raw
        record = json.loads(raw)
        assert record["payload"]["metadata"]["authorization"] == "[REDACTED]"
        assert record["payload"]["metadata"]["password"] == "[REDACTED]"
        assert record["payload"]["user"]["token"] == "[REDACTED]"

    def test_persist_disabled_is_noop(self, tmp_path):
        spool = _spool(tmp_path, enabled=False)
        assert spool.persist("/ingest/v1/logs", {"message": "hi"}) is False
        assert spool.available() is False
        assert spool.count() == 0


# ---------------------------------------------------------------------------
# Bounds / eviction
# ---------------------------------------------------------------------------


class TestBounds:
    def test_count_cap_drops_oldest(self, tmp_path):
        spool = _spool(tmp_path, max_events=3)
        for i in range(6):
            spool.persist("/ingest/v1/logs", {"seq": i})
            time.sleep(0.002)  # keep millisecond filename prefixes distinct
        assert spool.count() == 3
        # The three survivors must be the NEWEST (seq 3,4,5).
        seqs = sorted(_load_all(tmp_path), key=lambda r: r["ts"])
        assert [r["payload"]["seq"] for r in seqs] == [3, 4, 5]

    def test_byte_cap_drops_oldest(self, tmp_path):
        # Byte budget above the 1 KiB safety floor; big entries blow it.
        budget = 4096
        spool = _spool(tmp_path, max_events=1000, max_bytes=budget)
        for i in range(20):
            spool.persist("/ingest/v1/logs", {"seq": i, "pad": "x" * 400})
            time.sleep(0.002)
        # Must stay under budget and keep the newest entries.
        total = sum(
            os.path.getsize(p) for p in (tmp_path / "spool").glob("*.json")
        )
        assert total <= budget
        survivors = [r["payload"]["seq"] for r in _load_all(tmp_path)]
        assert max(survivors) == 19  # newest kept
        assert len(survivors) < 20  # something was evicted

    def test_age_cap_drops_stale(self, tmp_path):
        spool = _spool(tmp_path, max_age_s=0.05)
        spool.persist("/ingest/v1/logs", {"seq": "old"})
        time.sleep(0.08)
        # A fresh write triggers bound enforcement, which evicts the stale one.
        spool.persist("/ingest/v1/logs", {"seq": "new"})
        survivors = [r["payload"]["seq"] for r in _load_all(tmp_path)]
        assert survivors == ["new"]


# ---------------------------------------------------------------------------
# Drain
# ---------------------------------------------------------------------------


class TestDrain:
    def test_drain_removes_accepted_entries(self, tmp_path):
        spool = _spool(tmp_path)
        spool.persist("/ingest/v1/logs", {"seq": 1})
        spool.persist("/ingest/v1/logs", {"seq": 2})

        sent: List[Tuple[str, Dict[str, Any]]] = []

        def send(path, payload):
            sent.append((path, payload))
            return 202, {}

        removed = spool.drain(send)
        assert removed == 2
        assert spool.count() == 0
        assert [p["seq"] for _path, p in sent] == [1, 2]  # oldest-first

    def test_drain_keeps_entry_on_network_error(self, tmp_path):
        spool = _spool(tmp_path)
        spool.persist("/ingest/v1/logs", {"seq": 1})

        def send(path, payload):
            raise AllStakTransportError("network down")

        removed = spool.drain(send)
        assert removed == 0
        assert spool.count() == 1  # kept for the next boot

    def test_drain_keeps_entry_on_429(self, tmp_path):
        spool = _spool(tmp_path)
        spool.persist("/ingest/v1/logs", {"seq": 1})

        def send(path, payload):
            return 429, {}

        assert spool.drain(send) == 0
        assert spool.count() == 1

    def test_drain_removes_entry_on_permanent_4xx(self, tmp_path):
        spool = _spool(tmp_path)
        spool.persist("/ingest/v1/errors", {"seq": 1})

        def send(path, payload):
            return 400, {}

        assert spool.drain(send) == 1
        assert spool.count() == 0

    def test_drain_stops_on_auth_error(self, tmp_path):
        spool = _spool(tmp_path)
        spool.persist("/ingest/v1/logs", {"seq": 1})
        spool.persist("/ingest/v1/logs", {"seq": 2})

        def send(path, payload):
            raise AllStakAuthError("401")

        assert spool.drain(send) == 0
        assert spool.count() == 2  # nothing removed, drain aborted

    def test_drain_drops_corrupt_entry(self, tmp_path):
        spool = _spool(tmp_path)
        spool.available()  # create dir
        bad = tmp_path / "spool" / "0000000000001-bad.json"
        bad.write_text("{ this is not valid json", encoding="utf-8")

        def send(path, payload):  # pragma: no cover — should never be called
            raise AssertionError("corrupt entry must not be sent")

        spool.drain(send)
        assert spool.count() == 0


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_unwritable_dir_degrades_to_noop(self, tmp_path, monkeypatch):
        spool = _spool(tmp_path)

        def boom(*_a, **_k):
            raise OSError("read-only filesystem")

        monkeypatch.setattr("allstak.spool.os.makedirs", boom)
        assert spool.available() is False
        assert spool.persist("/ingest/v1/logs", {"x": 1}) is False
        assert spool.drain(lambda p, q: (202, {})) == 0  # never raises


# ---------------------------------------------------------------------------
# Transport integration: persist-on-failure
# ---------------------------------------------------------------------------


def _transport(spool, **kwargs) -> HttpTransport:
    defaults = dict(
        api_key=KEY,
        host=BASE,
        connect_timeout=0.2,
        read_timeout=0.2,
        max_retries=2,
        spool=spool,
    )
    defaults.update(kwargs)
    return HttpTransport(**defaults)


class TestTransportPersistOnFailure:
    @respx.mock
    def test_persists_when_retries_exhausted(self, tmp_path, monkeypatch):
        # No sleeping between retries — keep the test fast.
        monkeypatch.setattr("allstak.transport.time.sleep", lambda *_a: None)
        respx.post(f"{BASE}/ingest/v1/logs").mock(
            return_value=httpx.Response(503, json={})
        )
        spool = _spool(tmp_path)
        t = _transport(spool)
        with pytest.raises(AllStakTransportError):
            t.post("/ingest/v1/logs", {"message": "buffered at outage"})
        assert spool.count() == 1
        stats = t.stats()
        assert stats["eventsPersisted"] == 1
        assert stats["eventsDropped"] == 0

    @respx.mock
    def test_network_error_persists_scrubbed_payload(self, tmp_path, monkeypatch):
        monkeypatch.setattr("allstak.transport.time.sleep", lambda *_a: None)
        respx.post(f"{BASE}/ingest/v1/errors").mock(
            side_effect=httpx.ConnectError("no route to host")
        )
        spool = _spool(tmp_path)
        t = _transport(spool)
        with pytest.raises(AllStakTransportError):
            t.post(
                "/ingest/v1/errors",
                {"message": "boom", "metadata": {"password": SECRET}},
            )
        assert spool.count() == 1
        raw = next((tmp_path / "spool").glob("*.json")).read_text()
        assert SECRET not in raw
        assert "[REDACTED]" in raw

    @respx.mock
    def test_session_lifecycle_not_persisted_on_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr("allstak.transport.time.sleep", lambda *_a: None)
        respx.post(f"{BASE}/ingest/v1/sessions/end").mock(
            return_value=httpx.Response(503, json={})
        )
        spool = _spool(tmp_path)
        t = _transport(spool)
        with pytest.raises(AllStakTransportError):
            t.post("/ingest/v1/sessions/end", {"sessionId": "abc", "durationMs": 10})
        assert spool.count() == 0  # session calls are live-only
        stats = t.stats()
        assert stats["eventsPersisted"] == 0
        assert stats["eventsDropped"] == 1

    @respx.mock
    def test_permanent_4xx_not_persisted(self, tmp_path):
        # 400 returns without raising and must never be spooled.
        respx.post(f"{BASE}/ingest/v1/logs").mock(
            return_value=httpx.Response(400, json={})
        )
        spool = _spool(tmp_path)
        t = _transport(spool)
        status, _body = t.post("/ingest/v1/logs", {"message": "bad"})
        assert status == 400
        assert spool.count() == 0

    @respx.mock
    def test_drain_resend_does_not_repersist_on_transient_failure(
        self, tmp_path, monkeypatch
    ):
        # An entry replayed via send_for_drain that fails transiently must NOT
        # create a second spool file — the original on-disk copy is kept by the
        # drain logic instead.
        monkeypatch.setattr("allstak.transport.time.sleep", lambda *_a: None)
        respx.post(f"{BASE}/ingest/v1/logs").mock(
            return_value=httpx.Response(503, json={})
        )
        spool = _spool(tmp_path)
        spool.persist("/ingest/v1/logs", {"message": "x"})
        assert spool.count() == 1
        t = _transport(spool)
        # Drain through the transport; the 503 keeps the entry but must not add
        # a duplicate via the persist-on-failure hook.
        spool.drain(t.send_for_drain)
        assert spool.count() == 1
        stats = t.stats()
        assert stats["eventsPersisted"] == 0
        assert stats["eventsDropped"] == 0


# ---------------------------------------------------------------------------
# Config opt-out + client wiring
# ---------------------------------------------------------------------------


class TestConfigOptOut:
    def test_offline_storage_on_by_default(self):
        cfg = AllStakConfig(api_key="ask_test")
        assert cfg.offline_storage is True

    def test_offline_storage_opt_out(self):
        cfg = AllStakConfig(api_key="ask_test", offline_storage=False)
        assert cfg.offline_storage is False

    def test_client_opt_out_attaches_no_spool(self, tmp_path):
        from allstak.client import AllStakClient

        cfg = AllStakConfig(
            api_key="ask_test",
            offline_storage=False,
            enable_auto_session_tracking=False,
            auto_register_release=False,
            auto_breadcrumbs=False,
            install_excepthook=False,
            install_threading_excepthook=False,
        )
        client = AllStakClient(cfg)
        try:
            assert client._spool is None
            assert client._transport._spool is None
        finally:
            client._shutdown()

    def test_client_default_attaches_spool(self, tmp_path):
        from allstak.client import AllStakClient

        cfg = AllStakConfig(
            api_key="ask_test",
            offline_queue_dir=str(tmp_path / "spool"),
            enable_auto_session_tracking=False,
            auto_register_release=False,
            auto_breadcrumbs=False,
            install_excepthook=False,
            install_threading_excepthook=False,
        )
        client = AllStakClient(cfg)
        try:
            assert client._spool is not None
            assert client._transport._spool is client._spool
        finally:
            client._shutdown()


# ---------------------------------------------------------------------------
# Drain-on-init: a previous run's spool is replayed on the next init
# ---------------------------------------------------------------------------


class TestDrainOnInit:
    @respx.mock
    def test_previous_run_events_replayed_on_init(self, tmp_path, monkeypatch):
        from allstak.client import AllStakClient

        # Simulate a previous process that left two persisted events behind.
        seed_dir = str(tmp_path / "spool")
        seed = EventSpool(seed_dir)
        seed.persist("/ingest/v1/logs", {"message": "from-previous-run-1"})
        seed.persist("/ingest/v1/errors", {"message": "from-previous-run-2"})
        assert seed.count() == 2

        # The new process's backend is up and accepts everything.
        log_route = respx.post(f"{BASE}/ingest/v1/logs").mock(
            return_value=httpx.Response(202, json={"data": {"id": "l1"}})
        )
        err_route = respx.post(f"{BASE}/ingest/v1/errors").mock(
            return_value=httpx.Response(202, json={"data": {"id": "e1"}})
        )

        # Force the client to actually run its drain (it normally skips under
        # the SDK's own test runtime).
        monkeypatch.setattr(AllStakClient, "_is_test_runtime", staticmethod(lambda: False))

        cfg = AllStakConfig(
            api_key="ask_test",
            host=BASE,
            offline_queue_dir=seed_dir,
            enable_auto_session_tracking=False,
            auto_register_release=False,
            auto_breadcrumbs=False,
            install_excepthook=False,
            install_threading_excepthook=False,
        )
        client = AllStakClient(cfg)
        try:
            # Drain runs on a daemon thread — wait for the spool to empty.
            deadline = time.time() + 3.0
            while time.time() < deadline and client._spool.count() > 0:
                time.sleep(0.02)
            assert client._spool.count() == 0
            assert log_route.called
            assert err_route.called
        finally:
            client._shutdown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_all(tmp_path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in (tmp_path / "spool").glob("*.json"):
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out
