"""AllStak Python SDK sanitizer.

Two layers of redaction, applied on the wire path:

1. **Key-name redaction** (``scrub``) — recursive scrub of sensitive *keys*
   (password/token/cookie/...) across the full event surface
   (user, extras, metadata, breadcrumbs.data, contexts, request, response).
   Conforms to the canonical AllStak SDK denylist defined in
   ``docs/standards/sdk-platform-standards.md``.

2. **Value-pattern redaction** (``scrub_values``) — Sentry-parity scrubbing of
   PII that leaks into free-text string *values*: credit-card numbers (Luhn
   validated), US SSNs, e-mail addresses and IPv4 addresses. This is
   conservative by design — only Luhn-valid card runs and hyphenated SSNs are
   touched, so order ids / timestamps / bare 9-digit numbers are preserved.

Layering / ``send_default_pii`` (Sentry parity, default ``False``):

* **Always** scrubbed regardless of ``send_default_pii`` — high-risk
  financial / identity data never legitimately wanted in telemetry:
  credit-card numbers and US SSNs.
* Scrubbed **unless** ``send_default_pii is True`` — e-mail addresses and
  IPv4 addresses. When the operator opts into PII these pass through in free
  text (matching Sentry's ``send_default_pii=True``).

Key-name redaction (layer 1) is *always* applied and is independent of
``send_default_pii``.

Semantics (key-name redaction):
- Case-insensitive substring match on map keys.
- Value replacement with the sentinel string ``[REDACTED]`` (key preserved).
- Recursion into dict, list, tuple, set; primitive values are passed through.
- Cycle protection via an identity set (``id()`` of visited containers).
- Pure: returns a sanitized copy; never mutates caller-owned structures.

Semantics (value-pattern redaction):
- Only string values are inspected; the match is replaced in-place inside the
  string with ``[REDACTED]``.
- Keys whose *name* is in :data:`VALUE_SCRUB_SKIP_KEYS` (stack-frame paths,
  release/sdk identity, span/operation names, URLs, the SDK session id, and
  the explicit ``user`` object) are passed through untouched so legitimate,
  intentionally-shipped data is not corrupted.
- Fail-open: any scrubber error returns the input value unchanged — value
  scrubbing must never drop or break an event.
- Bounded: regexes are compiled once at import; recursion depth and the length
  of any scanned string are capped so a pathological payload cannot blow up the
  wire path.
"""
from __future__ import annotations

import re
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

# ---------------------------------------------------------------------------
# Value-pattern scrubbing
# ---------------------------------------------------------------------------

# Keys whose *string values* must NOT be value-scrubbed. These either hold
# intentionally-shipped identity/diagnostic data (explicit user object, release
# / sdk identity, span / operation names) or structured paths that a free-text
# scrubber would corrupt (stack-frame filename/function/absPath, URLs handled by
# the dedicated URL redactor, the SDK's own correlation session id).
#
# Matching is case-insensitive and exact on the key name. ``user`` short-circuits
# the whole subtree — an explicitly-set user object (id/email/ip) ships as-is,
# matching Sentry's behaviour where send_default_pii never strips explicit user
# data.
VALUE_SCRUB_SKIP_KEYS: frozenset[str] = frozenset({
    "user",            # explicit setUser object — id/email/ip ship as-is
    "userid",          # explicit user identifier (intentional, like user.id)
    "user_id",
    "errorid",         # error correlation id
    "error_id",
    "requestid",       # request correlation id
    "request_id",
    "stacktrace",      # raw frame-string list (file paths / source lines)
    "frames",          # structured stack frames (filename/function/absPath/...)
    "filename",        # stack-frame paths
    "abspath",
    "function",
    "module",
    "release",
    "dist",
    "platform",
    "sdkname",
    "sdkversion",
    "sdk.name",
    "sdk.version",
    "commit.sha",
    "commit.branch",
    "version",
    "sessionid",       # SDK-controlled session/replay correlation key
    "session_id",
    "traceid",
    "trace_id",
    "spanid",
    "span_id",
    "parentspanid",
    "parent_span_id",
    "op",              # span / operation names
    "operation",
    "url",             # URLs/paths have their own URL redactor
    "path",
    "queryhash",
    "fingerprint",
    "errorfingerprint",
})

# Cap how deep we recurse and how long a string we will scan. A pathological
# payload (deeply nested or a multi-megabyte blob) must not stall the wire path.
_MAX_VALUE_DEPTH = 25
_MAX_SCAN_CHARS = 32_768

# --- Compiled patterns (compiled once at import) ---------------------------

# Candidate credit-card run: 13-19 digits with optional single space/hyphen
# separators between digits. Luhn-validated before replacement (see below) so
# Luhn-failing runs (order ids, timestamps) are preserved.
_CC_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

# US SSN — REQUIRE the hyphens. Bare 9-digit numbers are NOT matched (avoids
# nuking order ids / numeric identifiers).
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# Standard email address.
_EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
)

# IPv4 with each octet validated to 0-255.
_IPV4_OCTET = r"(?:25[0-5]|2[0-4]\d|1?\d?\d)"
_IPV4_RE = re.compile(
    r"\b" + _IPV4_OCTET + r"(?:\." + _IPV4_OCTET + r"){3}\b"
)


def _luhn_valid(digits: str) -> bool:
    """Return True if the digit string passes the Luhn checksum.

    ``digits`` must already be stripped of separators. Length is bounded by the
    caller (13-19) but we re-check defensively.
    """
    n = len(digits)
    if n < 13 or n > 19:
        return False
    total = 0
    # Walk right-to-left, doubling every second digit.
    parity = n % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48  # ord('0')
        if d < 0 or d > 9:
            return False
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _scrub_credit_cards(text: str) -> str:
    """Replace only Luhn-valid 13-19 digit runs with the redaction sentinel."""
    def _repl(m: "re.Match[str]") -> str:
        run = m.group(0)
        digits = run.replace(" ", "").replace("-", "")
        if _luhn_valid(digits):
            return REDACTED
        return run  # not a card (fails Luhn) — preserve verbatim
    return _CC_CANDIDATE_RE.sub(_repl, text)


def _scrub_string(text: str, send_default_pii: bool) -> str:
    """Apply the value-pattern scrubbers to a single string. Fail-open.

    Always: credit cards (Luhn) + SSN. Unless ``send_default_pii``: email +
    IPv4. Returns ``text`` unchanged on any error or when nothing matches.
    """
    if not text:
        return text
    try:
        # Skip very large strings gracefully — scan only the cheap prefix would
        # leak the tail, so instead we bail on the whole value (preserve it):
        # over-scanning a multi-MB blob on the wire path is the worse failure.
        if len(text) > _MAX_SCAN_CHARS:
            return text
        # ALWAYS-ON layer (A).
        out = _scrub_credit_cards(text)
        out = _SSN_RE.sub(REDACTED, out)
        # PII layer (B) — disabled when the operator opted into PII.
        if not send_default_pii:
            out = _EMAIL_RE.sub(REDACTED, out)
            out = _IPV4_RE.sub(REDACTED, out)
        return out
    except Exception:
        # Fail-open: never break an event over a scrubber error.
        return text


def _is_sensitive(key: str, denylist: Iterable[str]) -> bool:
    k = key.lower()
    return any(term in k for term in denylist)


def scrub(payload: Any, extra_denylist: Iterable[str] | None = None) -> Any:
    """Return a key-name-sanitized deep copy of ``payload``.

    ``extra_denylist`` may add terms; it must not narrow the canonical list.
    This is the layer-1 (key-name) scrubber and is unconditional — it does not
    touch free-text values. Use :func:`scrub_values` for layer-2 PII patterns.
    """
    denylist = tuple(t.lower() for t in DEFAULT_DENYLIST)
    if extra_denylist:
        denylist = denylist + tuple(t.lower() for t in extra_denylist)
    seen: set[int] = set()
    return _walk(payload, denylist, seen)


def scrub_values(
    payload: Any,
    *,
    send_default_pii: bool = False,
) -> Any:
    """Return a copy of ``payload`` with PII value-patterns scrubbed.

    Layer-2 scrubbing of free-text string values (credit cards, SSNs, and —
    unless ``send_default_pii`` — emails / IPv4). Keys named in
    :data:`VALUE_SCRUB_SKIP_KEYS` (and their subtrees, for ``user``) are passed
    through untouched so stack-frame paths, release/sdk identity, URLs, the SDK
    session id, and the explicit user object are never corrupted.

    Fail-open and bounded: a scrubber error or excessive depth returns the
    (partially) processed value rather than raising.
    """
    try:
        seen: set[int] = set()
        return _walk_values(payload, send_default_pii, seen, 0)
    except Exception:
        # Absolute fail-open: never drop/break an event over value scrubbing.
        return payload


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


def _walk_values(
    value: Any,
    send_default_pii: bool,
    seen: set[int],
    depth: int,
) -> Any:
    # Depth cap: stop descending into pathological nesting. Returning the value
    # unscrubbed (rather than redacting it) keeps us conservative.
    if depth > _MAX_VALUE_DEPTH:
        return value
    if isinstance(value, str):
        return _scrub_string(value, send_default_pii)
    if isinstance(value, dict):
        if id(value) in seen:
            return value  # cycle guard — already handled / will recurse safely
        seen.add(id(value))
        out: dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in VALUE_SCRUB_SKIP_KEYS:
                # Protected key — ship the value as-is (explicit user object,
                # stack-frame path, release identity, URL, span name, session id).
                out[k] = v
            else:
                out[k] = _walk_values(v, send_default_pii, seen, depth + 1)
        return out
    if isinstance(value, list):
        if id(value) in seen:
            return value
        seen.add(id(value))
        return [_walk_values(v, send_default_pii, seen, depth + 1) for v in value]
    if isinstance(value, tuple):
        if id(value) in seen:
            return value
        seen.add(id(value))
        return tuple(_walk_values(v, send_default_pii, seen, depth + 1) for v in value)
    if isinstance(value, set):
        if id(value) in seen:
            return value
        seen.add(id(value))
        return [_walk_values(v, send_default_pii, seen, depth + 1) for v in value]
    return value


__all__ = [
    "scrub",
    "scrub_values",
    "DEFAULT_DENYLIST",
    "VALUE_SCRUB_SKIP_KEYS",
    "REDACTED",
]
