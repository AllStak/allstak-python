"""AllStak Python SDK sanitizer.

Provides recursive scrubbing of sensitive keys across the full event surface
(user, extras, metadata, breadcrumbs.data, contexts, request, response).

Conforms to the canonical AllStak SDK denylist defined in
docs/standards/sdk-platform-standards.md.

Semantics:
- Case-insensitive substring match on map keys.
- Value replacement with the sentinel string ``[REDACTED]`` (key preserved).
- Recursion into dict, list, tuple, set; primitive values are passed through.
- Cycle protection via an identity set (``id()`` of visited containers).
- Pure: returns a sanitized copy; never mutates caller-owned structures.
"""
from __future__ import annotations

from typing import Any, Iterable

REDACTED = "[REDACTED]"

# Canonical denylist (case-insensitive substring match on keys).
# Order is presentation-only.
DEFAULT_DENYLIST: tuple[str, ...] = (
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "password",
    "passwd",
    "pwd",
    "api_key",
    "apikey",
    "x-api-key",
    "x-allstak-key",
    "x-auth-token",
    "x-access-token",
    "token",
    "bearer",
    "jwt",
    "session",
    "sessionid",
    "session_id",
    "secret",
    "credit_card",
    "card_number",
    "cvv",
    "ssn",
    "csrf",
)


def _is_sensitive(key: str, denylist: Iterable[str]) -> bool:
    k = key.lower()
    return any(term in k for term in denylist)


def scrub(payload: Any, extra_denylist: Iterable[str] | None = None) -> Any:
    """Return a sanitized deep copy of ``payload``.

    ``extra_denylist`` may add terms; it must not narrow the canonical list.
    """
    denylist = tuple(t.lower() for t in DEFAULT_DENYLIST)
    if extra_denylist:
        denylist = denylist + tuple(t.lower() for t in extra_denylist)
    seen: set[int] = set()
    return _walk(payload, denylist, seen)


def _walk(value: Any, denylist: tuple[str, ...], seen: set[int]) -> Any:
    if isinstance(value, dict):
        if id(value) in seen:
            return REDACTED  # cycle guard
        seen.add(id(value))
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _is_sensitive(k, denylist):
                out[k] = REDACTED
            else:
                out[k] = _walk(v, denylist, seen)
        return out
    if isinstance(value, list):
        if id(value) in seen:
            return REDACTED
        seen.add(id(value))
        return [_walk(v, denylist, seen) for v in value]
    if isinstance(value, tuple):
        if id(value) in seen:
            return REDACTED
        seen.add(id(value))
        return tuple(_walk(v, denylist, seen) for v in value)
    if isinstance(value, set):
        # Sets are unhashable for nested content; preserve as list.
        if id(value) in seen:
            return REDACTED
        seen.add(id(value))
        return [_walk(v, denylist, seen) for v in value]
    return value


__all__ = ["scrub", "DEFAULT_DENYLIST", "REDACTED"]
