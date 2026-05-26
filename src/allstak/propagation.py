"""Trace propagation helpers shared by framework integrations."""

from __future__ import annotations

from typing import Iterable, Optional


def allstak_baggage(trace_id: str, request_id: Optional[str] = None, span_id: Optional[str] = None) -> str:
    items = [f"allstak-trace_id={trace_id}"]
    if request_id:
        items.append(f"allstak-request_id={request_id}")
    if span_id:
        items.append(f"allstak-span_id={span_id}")
    return ",".join(items)


def merge_baggage(existing: Optional[str], trace_id: str, request_id: Optional[str] = None, span_id: Optional[str] = None) -> str:
    preserved = [
        part.strip()
        for part in (existing or "").split(",")
        if part.strip() and not part.strip().lower().startswith("allstak-")
    ]
    preserved.extend(allstak_baggage(trace_id, request_id, span_id).split(","))
    return ",".join(preserved)


def _trace_flags(sampled: bool) -> str:
    """W3C ``traceparent`` trace-flags byte: ``01`` sampled, ``00`` not sampled."""
    return "01" if sampled else "00"


def set_mapping_headers(
    headers: object,
    *,
    trace_id: str,
    request_id: Optional[str] = None,
    span_id: Optional[str] = None,
    sampled: bool = True,
    merge_existing: bool = True,
    overwrite: bool = True,
) -> None:
    """Set trace headers on dict-like request/response headers.

    ``sampled`` drives the W3C ``traceparent`` trace-flags byte (``-01``
    when sampled, ``-00`` when not). Defaults to ``True`` for backward
    compatibility with the previous always-sampled behaviour.
    """

    def set_header(name: str, value: str, *, force: bool = False) -> None:
        if not force and not overwrite and hasattr(headers, "get") and headers.get(name):  # type: ignore[attr-defined]
            return
        headers[name] = value  # type: ignore[index]

    set_header("x-allstak-trace-id", trace_id)
    if request_id:
        set_header("x-allstak-request-id", request_id)
    if span_id:
        set_header("x-allstak-span-id", span_id)
        set_header("traceparent", f"00-{trace_id}-{span_id[:16]}-{_trace_flags(sampled)}")

    baggage = merge_baggage(
        headers.get("baggage") if merge_existing and hasattr(headers, "get") else None,  # type: ignore[attr-defined]
        trace_id,
        request_id,
        span_id,
    )
    set_header("baggage", baggage, force=True)
    set_header("allstak-baggage", allstak_baggage(trace_id, request_id, span_id), force=True)


def set_asgi_headers(
    raw_headers: Iterable[tuple[bytes, bytes]],
    *,
    trace_id: str,
    request_id: Optional[str] = None,
    span_id: Optional[str] = None,
    sampled: bool = True,
) -> list[tuple[bytes, bytes]]:
    existing: list[tuple[bytes, bytes]] = []
    baggage: Optional[str] = None
    for key, value in raw_headers:
        lower = key.lower()
        if lower == b"baggage":
            baggage = value.decode("latin-1")
            continue
        if lower in {b"allstak-baggage", b"x-allstak-trace-id", b"x-allstak-request-id", b"x-allstak-span-id", b"traceparent"}:
            continue
        existing.append((key, value))

    def add(name: str, value: str) -> None:
        existing.append((name.encode("latin-1"), value.encode("latin-1")))

    add("x-allstak-trace-id", trace_id)
    if request_id:
        add("x-allstak-request-id", request_id)
    if span_id:
        add("x-allstak-span-id", span_id)
        add("traceparent", f"00-{trace_id}-{span_id[:16]}-{_trace_flags(sampled)}")
    add("baggage", merge_baggage(baggage, trace_id, request_id, span_id))
    add("allstak-baggage", allstak_baggage(trace_id, request_id, span_id))
    return existing
