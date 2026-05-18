"""Unit tests for allstak.sanitize.

Covers:
- Each sensitive key family from the canonical denylist.
- Nested dict, list, tuple recursion.
- Cycle protection.
- Canary string presence proof for the live E2E gate.
"""
from __future__ import annotations

import pytest

from allstak.sanitize import DEFAULT_DENYLIST, REDACTED, scrub


def test_top_level_sensitive_key_is_redacted():
    out = scrub({"Authorization": "Bearer abc"})
    assert out == {"Authorization": REDACTED}


def test_case_insensitive_match():
    out = scrub({"AUTHORIZATION": "x", "x-Api-Key": "y"})
    assert out["AUTHORIZATION"] == REDACTED
    assert out["x-Api-Key"] == REDACTED


def test_nested_dict_recursion():
    out = scrub({"user": {"email": "a@b", "password": "p"}})
    assert out["user"]["email"] == "a@b"
    assert out["user"]["password"] == REDACTED


def test_list_recursion():
    out = scrub({"items": [{"token": "t"}, {"safe": "v"}]})
    assert out["items"][0]["token"] == REDACTED
    assert out["items"][1]["safe"] == "v"


def test_tuple_recursion():
    out = scrub({"pair": ({"jwt": "j"}, "x")})
    assert out["pair"][0]["jwt"] == REDACTED
    assert out["pair"][1] == "x"


def test_canonical_denylist_coverage():
    payload = {term: "leaky" for term in DEFAULT_DENYLIST}
    out = scrub(payload)
    for term in DEFAULT_DENYLIST:
        assert out[term] == REDACTED, f"term {term} not redacted"


def test_extras_metadata_breadcrumbs_surface():
    event = {
        "user": {"id": "u1", "api_key": "k"},
        "extras": {"password": "p", "ok": 1},
        "metadata": {"session_id": "s", "ok": 1},
        "breadcrumbs": [{"data": {"cookie": "c", "msg": "hi"}}],
        "contexts": {"runtime": {"secret": "x"}},
        "request": {"headers": {"Authorization": "Bearer"}},
    }
    out = scrub(event)
    assert out["user"]["api_key"] == REDACTED
    assert out["extras"]["password"] == REDACTED
    assert out["metadata"]["session_id"] == REDACTED
    assert out["breadcrumbs"][0]["data"]["cookie"] == REDACTED
    assert out["contexts"]["runtime"]["secret"] == REDACTED
    assert out["request"]["headers"]["Authorization"] == REDACTED


def test_does_not_mutate_caller():
    payload = {"Authorization": "v"}
    scrub(payload)
    assert payload == {"Authorization": "v"}


def test_cycle_protection():
    d: dict = {"a": 1}
    d["self"] = d
    out = scrub(d)
    assert out["a"] == 1
    # The cycle node is collapsed to REDACTED, not infinite-recursion.
    assert out["self"] == REDACTED


def test_canary_should_not_leak_python():
    # Canary for the live ingest E2E gate. The sanitizer must remove the canary
    # whenever it appears under a sensitive key.
    event = {
        "metadata": {"api_key": "should_not_leak_python"},
        "user": {"password": "should_not_leak_python"},
    }
    out = scrub(event)
    assert "should_not_leak_python" not in str(out["metadata"])
    assert "should_not_leak_python" not in str(out["user"])


def test_extension_list_adds_terms():
    out = scrub({"custom_pii": "v"}, extra_denylist=["custom_pii"])
    assert out["custom_pii"] == REDACTED


def test_primitive_passthrough():
    assert scrub(42) == 42
    assert scrub("x") == "x"
    assert scrub(None) is None
