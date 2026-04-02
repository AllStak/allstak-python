"""Unit tests for the ring buffer and flush buffer."""

import time
import threading
import pytest

from allstak.buffer import RingBuffer, FlushBuffer


class TestRingBuffer:
    def test_push_and_drain(self):
        buf = RingBuffer(maxsize=5)
        buf.push(1)
        buf.push(2)
        buf.push(3)
        items = buf.drain()
        assert items == [1, 2, 3]
        assert len(buf) == 0

    def test_drain_is_atomic(self):
        buf = RingBuffer(maxsize=5)
        buf.push("a")
        buf.push("b")
        items = buf.drain()
        assert items == ["a", "b"]
        # Second drain returns empty
        assert buf.drain() == []

    def test_evicts_oldest_when_full(self):
        buf = RingBuffer(maxsize=3)
        buf.push("a")
        buf.push("b")
        buf.push("c")
        buf.push("d")  # "a" should be evicted
        items = buf.drain()
        assert items == ["b", "c", "d"]

    def test_is_nearly_full(self):
        buf = RingBuffer(maxsize=10)
        for i in range(7):
            buf.push(i)
        assert not buf.is_nearly_full()
        buf.push(8)  # 8/10 = 80%
        assert buf.is_nearly_full()

    def test_thread_safe(self):
        buf = RingBuffer(maxsize=1000)
        errors = []

        def producer():
            try:
                for i in range(100):
                    buf.push(i)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=producer) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(buf) <= 1000


class TestFlushBuffer:
    def test_flush_calls_flush_fn(self):
        received = []

        def flush_fn(items):
            received.extend(items)

        fb = FlushBuffer(flush_fn=flush_fn, maxsize=10, interval_ms=60_000)
        fb.start()
        fb.push("x")
        fb.push("y")
        fb.flush()
        time.sleep(0.05)
        assert received == ["x", "y"]
        fb.stop()

    def test_timer_fires(self):
        received = []

        def flush_fn(items):
            received.extend(items)

        fb = FlushBuffer(flush_fn=flush_fn, maxsize=10, interval_ms=100)
        fb.start()
        fb.push("timer-test")
        time.sleep(0.3)  # wait for timer
        assert "timer-test" in received
        fb.stop()

    def test_shutdown_drains_remaining(self):
        received = []

        def flush_fn(items):
            received.extend(items)

        fb = FlushBuffer(flush_fn=flush_fn, maxsize=10, interval_ms=60_000)
        fb.start()
        fb.push("drain-me")
        fb.stop()
        assert "drain-me" in received

    def test_nearly_full_triggers_flush(self):
        received = []

        def flush_fn(items):
            received.extend(items)

        fb = FlushBuffer(flush_fn=flush_fn, maxsize=10, interval_ms=60_000)
        fb.start()
        # Push 8 items (80% of 10) — should trigger early flush
        for i in range(8):
            fb.push(i)
        time.sleep(0.05)  # allow background flush to complete
        assert len(received) >= 8
        fb.stop()

    def test_flush_fn_exception_does_not_crash(self):
        call_count = 0

        def bad_flush_fn(items):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("flush error!")

        fb = FlushBuffer(flush_fn=bad_flush_fn, maxsize=10, interval_ms=60_000)
        fb.start()
        fb.push("x")
        fb.flush()  # should not raise
        time.sleep(0.05)
        assert call_count >= 1
        fb.stop()
