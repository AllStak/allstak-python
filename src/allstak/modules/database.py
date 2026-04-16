"""Database query monitoring module — POST /ingest/v1/db."""

from __future__ import annotations

import hashlib
import logging
import re
import sys
import time
from typing import Any, Dict, List, Optional

from ..buffer import FlushBuffer
from ..config import AllStakConfig
from ..transport import AllStakAuthError, AllStakTransportError, HttpTransport

logger = logging.getLogger("allstak.sdk")

_INGEST_PATH = "/ingest/v1/db"
_BATCH_SIZE = 100
_FLUSH_THRESHOLD = 50  # flush early when buffer has this many items


# ---------------------------------------------------------------------------
# Query normalisation helpers
# ---------------------------------------------------------------------------

def normalize_query(sql: str) -> str:
    """Replace literal values with ``?`` placeholders."""
    # String literals (single-quoted)
    sql = re.sub(r"'[^']*'", "?", sql)
    # Numeric literals
    sql = re.sub(r"\b\d+(\.\d+)?\b", "?", sql)
    # Collapse whitespace
    sql = re.sub(r"\s+", " ", sql).strip()
    return sql


def hash_query(normalized: str) -> str:
    """Create a short hash of the normalised query."""
    return hashlib.md5(normalized.encode()).hexdigest()[:16]


def detect_query_type(sql: str) -> str:
    """Detect SELECT / INSERT / UPDATE / DELETE / OTHER."""
    first_word = sql.strip().split()[0].upper() if sql.strip() else "OTHER"
    return first_word if first_word in ("SELECT", "INSERT", "UPDATE", "DELETE") else "OTHER"


# ---------------------------------------------------------------------------
# Database module
# ---------------------------------------------------------------------------

class DatabaseModule:
    """
    Buffers and batches database query telemetry for delivery to AllStak.

    Batches are flushed every ``flush_interval_ms`` ms or when
    ``_FLUSH_THRESHOLD`` items accumulate.

    Max batch size: 100 (backend enforced).
    """

    def __init__(self, transport: HttpTransport, config: AllStakConfig) -> None:
        self._transport = transport
        self._config = config
        self._flush_buffer: FlushBuffer[Dict[str, Any]] = FlushBuffer(
            flush_fn=self._flush_batch,
            maxsize=config.buffer_size,
            interval_ms=config.flush_interval_ms,
            name="allstak-db-flush",
        )
        self._flush_buffer.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        normalized_query: str,
        duration_ms: float,
        status: str = "success",
        error_message: Optional[str] = None,
        database_name: str = "",
        database_type: str = "",
        query_type: Optional[str] = None,
        rows_affected: int = -1,
        trace_id: str = "",
        span_id: str = "",
    ) -> None:
        """
        Record a single database query.

        :param normalized_query: SQL with literal values replaced by ``?``.
        :param duration_ms: Query duration in milliseconds (>= 0).
        :param status: ``"success"`` or ``"error"``.
        :param error_message: Error description (truncated to 500 chars).
        :param database_name: Logical database name.
        :param database_type: Driver / engine name (e.g. ``"postgresql"``, ``"sqlite"``).
        :param query_type: ``SELECT`` / ``INSERT`` / ``UPDATE`` / ``DELETE`` / ``OTHER``.
        :param rows_affected: Number of rows affected (``-1`` if unknown).
        :param trace_id: Distributed trace correlation ID.
        :param span_id: Span correlation ID.
        """
        try:
            if query_type is None:
                query_type = detect_query_type(normalized_query)

            item: Dict[str, Any] = {
                "normalizedQuery": normalized_query,
                "queryHash": hash_query(normalized_query),
                "queryType": query_type,
                "durationMs": max(0, int(duration_ms)),
                "timestampMillis": int(time.time() * 1000),
                "status": status,
                "errorMessage": (error_message or "")[:500],
                "databaseName": database_name,
                "databaseType": database_type,
                "service": getattr(self._config, "service", ""),
                "environment": self._config.environment or "",
                "traceId": trace_id,
                "spanId": span_id,
                "rowsAffected": rows_affected,
            }
            self._flush_buffer.push(item)
        except Exception as exc:
            logger.debug("[AllStak] database.record() failed silently: %s", exc)

    def flush(self) -> None:
        """Explicitly flush all buffered database queries."""
        self._flush_buffer.flush()

    def shutdown(self) -> None:
        """Drain the buffer and stop the timer thread."""
        self._flush_buffer.stop()

    # ------------------------------------------------------------------
    # Internal flush
    # ------------------------------------------------------------------

    def _flush_batch(self, items: List[Dict[str, Any]]) -> None:
        """Send items in batches of up to _BATCH_SIZE."""
        for i in range(0, len(items), _BATCH_SIZE):
            chunk = items[i : i + _BATCH_SIZE]
            try:
                status, body = self._transport.post(
                    _INGEST_PATH, {"queries": chunk}
                )
                if status != 202:
                    logger.debug(
                        "[AllStak] DB queries batch returned %d: %s", status, body
                    )
            except AllStakAuthError:
                logger.warning("[AllStak] DB batch skipped — SDK disabled (invalid API key).")
                return
            except AllStakTransportError as exc:
                logger.debug("[AllStak] DB batch transport error (discarding): %s", exc)
            except Exception as exc:
                logger.debug("[AllStak] Unexpected DB batch error: %s", exc)


# ---------------------------------------------------------------------------
# Auto-instrumentation — psycopg2
# ---------------------------------------------------------------------------

def instrument_psycopg2(db_module: DatabaseModule) -> None:
    """Patch ``psycopg2`` cursor.execute() for automatic DB capture."""
    try:
        import psycopg2  # type: ignore[import-untyped]
        import psycopg2.extensions  # type: ignore[import-untyped]

        _original_execute = psycopg2.extensions.cursor.execute

        def _patched_execute(self: Any, query: Any, vars: Any = None) -> Any:
            start = time.time()
            normalized = normalize_query(str(query))
            try:
                result = _original_execute(self, query, vars)
                duration = (time.time() - start) * 1000
                db_name = ""
                try:
                    db_name = self.connection.info.dbname if hasattr(self.connection, "info") else ""
                except Exception:
                    pass
                db_module.record(
                    normalized_query=normalized,
                    duration_ms=duration,
                    status="success",
                    database_type="postgresql",
                    database_name=db_name,
                    rows_affected=self.rowcount if self.rowcount >= 0 else -1,
                )
                return result
            except Exception as e:
                duration = (time.time() - start) * 1000
                db_module.record(
                    normalized_query=normalized,
                    duration_ms=duration,
                    status="error",
                    error_message=str(e)[:500],
                    database_type="postgresql",
                )
                raise

        psycopg2.extensions.cursor.execute = _patched_execute  # type: ignore[assignment]
        logger.debug("[AllStak] psycopg2 auto-instrumentation enabled.")
    except ImportError:
        pass  # psycopg2 not installed


# ---------------------------------------------------------------------------
# Auto-instrumentation — sqlite3
# ---------------------------------------------------------------------------
# sqlite3.Cursor is a C type and cannot be monkey-patched, so we wrap Connection
# instead: patch the free ``sqlite3.connect`` function so every new Connection
# returns a Cursor subclass whose execute/executemany methods record timing.

def instrument_sqlite3(db_module: DatabaseModule) -> None:
    """Wrap ``sqlite3.connect`` so new connections produce tracked cursors."""
    try:
        import sqlite3
    except ImportError:
        return

    # Guard against double instrumentation
    if getattr(sqlite3, "_allstak_instrumented", False):
        return

    _original_connect = sqlite3.connect

    def _record(normalized: str, start: float, status: str, err: Optional[str] = None, rows: int = -1) -> None:
        duration = (time.time() - start) * 1000
        try:
            db_module.record(
                normalized_query=normalized,
                duration_ms=duration,
                status=status,
                error_message=err,
                database_type="sqlite",
                rows_affected=rows,
            )
        except Exception:
            pass

    class _TrackedCursor(sqlite3.Cursor):
        def execute(self, sql, parameters=()):  # type: ignore[override]
            start = time.time()
            normalized = normalize_query(str(sql))
            try:
                result = super().execute(sql, parameters)
                _record(normalized, start, "success", rows=self.rowcount if self.rowcount >= 0 else -1)
                return result
            except Exception as e:
                _record(normalized, start, "error", err=str(e)[:500])
                raise

        def executemany(self, sql, seq_of_parameters):  # type: ignore[override]
            start = time.time()
            normalized = normalize_query(str(sql))
            try:
                result = super().executemany(sql, seq_of_parameters)
                _record(normalized, start, "success", rows=self.rowcount if self.rowcount >= 0 else -1)
                return result
            except Exception as e:
                _record(normalized, start, "error", err=str(e)[:500])
                raise

    class _TrackedConnection(sqlite3.Connection):
        def cursor(self, factory=_TrackedCursor):  # type: ignore[override]
            return super().cursor(factory or _TrackedCursor)

        def execute(self, sql, parameters=()):  # type: ignore[override]
            # Convenience API delegates through a cursor so the cursor subclass
            # captures it for us.
            return self.cursor().execute(sql, parameters)

        def executemany(self, sql, seq):  # type: ignore[override]
            return self.cursor().executemany(sql, seq)

    def _patched_connect(database, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs.setdefault("factory", _TrackedConnection)
        return _original_connect(database, *args, **kwargs)

    sqlite3.connect = _patched_connect  # type: ignore[assignment]
    sqlite3._allstak_instrumented = True  # type: ignore[attr-defined]
    logger.debug("[AllStak] sqlite3 auto-instrumentation enabled (factory-based).")


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def enable_db_auto_instrumentation(db_module: DatabaseModule) -> None:
    """Enable auto-instrumentation for all supported database drivers."""
    instrument_psycopg2(db_module)
    instrument_sqlite3(db_module)
