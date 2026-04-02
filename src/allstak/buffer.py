"""
Bounded ring buffer with background flush timer.

Contract from SDK guidelines:
- Default size: 500 items per feature
- Eviction: oldest item dropped when full (tail-drop)
- Flush triggers: timer (5s default), 80% capacity, explicit flush(), shutdown
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Callable, Deque, Generic, List, Optional, TypeVar

logger = logging.getLogger("allstak.sdk")

T = TypeVar("T")


class RingBuffer(Generic[T]):
    """
    Thread-safe bounded FIFO buffer.

    When ``maxsize`` items are held and a new item is pushed,
    the **oldest** item is silently dropped (tail-drop policy).
    """

    def __init__(self, maxsize: int = 500) -> None:
        self._maxsize = maxsize
        self._buf: Deque[T] = deque()
        self._lock = threading.Lock()
        self._overflow_warned = False

    @property
    def capacity(self) -> int:
        return self._maxsize

    def push(self, item: T) -> None:
        with self._lock:
            if len(self._buf) >= self._maxsize:
                self._buf.popleft()  # drop oldest
                if not self._overflow_warned:
                    logger.warning(
                        "[AllStak] Buffer is full (%d items); oldest events are being dropped. "
                        "Increase buffer_size or reduce flush_interval_ms.",
                        self._maxsize,
                    )
                    self._overflow_warned = True
            else:
                self._overflow_warned = False
            self._buf.append(item)

    def drain(self) -> List[T]:
        """Remove and return all current items atomically."""
        with self._lock:
            items = list(self._buf)
            self._buf.clear()
            return items

    def peek(self) -> List[T]:
        """Return all items without removing them."""
        with self._lock:
            return list(self._buf)

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)

    def is_nearly_full(self, threshold: float = 0.8) -> bool:
        with self._lock:
            return len(self._buf) >= self._maxsize * threshold


class FlushBuffer(Generic[T]):
    """
    Ring buffer with a background timer thread that periodically
    drains the buffer and calls ``flush_fn``.

    Flush is triggered when:
    - The timer fires (every ``interval_ms`` ms)
    - ``len(buffer) >= maxsize * 0.8``
    - ``flush()`` is called explicitly
    - ``shutdown()`` is called (best-effort drain)
    """

    def __init__(
        self,
        flush_fn: Callable[[List[T]], None],
        maxsize: int = 500,
        interval_ms: int = 5_000,
        name: str = "allstak-flush",
    ) -> None:
        self._flush_fn = flush_fn
        self._buffer: RingBuffer[T] = RingBuffer(maxsize)
        self._interval_s = interval_ms / 1_000
        self._name = name
        self._stop_event = threading.Event()
        self._flush_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the background flush timer thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_timer,
            name=self._name,
            daemon=True,  # doesn't prevent interpreter exit
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the timer and do a best-effort final flush (5s deadline)."""
        self._stop_event.set()
        self.flush()  # drain remaining
        if self._thread:
            self._thread.join(timeout=5.0)

    def push(self, item: T) -> None:
        self._buffer.push(item)
        if self._buffer.is_nearly_full():
            self._trigger_flush()

    def flush(self) -> None:
        """Synchronously drain the buffer and call flush_fn."""
        self._trigger_flush()

    # ------------------------------------------------------------------

    def _run_timer(self) -> None:
        while not self._stop_event.wait(timeout=self._interval_s):
            self._trigger_flush()

    def _trigger_flush(self) -> None:
        items = self._buffer.drain()
        if not items:
            return
        # Serialize flushes so we don't double-send
        with self._flush_lock:
            try:
                self._flush_fn(items)
            except Exception as exc:
                logger.debug(
                    "[AllStak] Flush error for %s: %s", self._name, exc
                )

    def __len__(self) -> int:
        return len(self._buffer)
