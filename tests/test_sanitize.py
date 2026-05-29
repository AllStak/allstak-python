"""Unit tests for allstak.sanitize.

Covers:
- Each sensitive key family from the canonical denylist.
- Nested dict, list, tuple recursion.
- Cycle protection.
- Canary string presence proof for the live E2E gate.
"""
from __future__ import annotations

import pytest

from allstak.sanitize import (
    DEFAULT_DENYLIST,
    REDACTED,
    VALUE_SCRUB_SKIP_KEYS,
    scrub,
    scrub_values,
)


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


# ---------------------------------------------------------------------------
# Layer-2: value-pattern PII scrubbing (scrub_values)
# ---------------------------------------------------------------------------


# A real Visa test number that passes the Luhn checksum.
_VALID_CC = "4111111111111111"
# A 16-digit run that FAILS Luhn (e.g. an order id / sequence) — must survive.
_INVALID_CC_RUN = "1234567890123456"


def test_credit_card_redacted_only_when_luhn_valid():
    # Luhn-valid card → redacted.
    out = scrub_values({"message": f"card {_VALID_CC} used"})
    assert _VALID_CC not in out["message"]
    assert REDACTED in out["message"]


def test_credit_card_with_separators_redacted():
    spaced = "4111 1111 1111 1111"
    hyphened = "4111-1111-1111-1111"
    out = scrub_values({"a": f"pay {spaced}", "b": f"pay {hyphened}"})
    assert "4111" not in out["a"]
    assert "4111" not in out["b"]


def test_luhn_invalid_run_is_preserved():
    # A 16-digit run that fails Luhn must NOT be redacted (no order-id nuking).
    msg = f"order {_INVALID_CC_RUN} shipped"
    out = scrub_values({"message": msg})
    assert out["message"] == msg


def test_short_or_long_digit_runs_preserved():
    # 12 digits (too short) and a long timestamp-ish run stay verbatim.
    out = scrub_values({"a": "id 123456789012", "b": "ts 20240517120000123"})
    assert out["a"] == "id 123456789012"
    # 19-digit run only redacts if Luhn-valid; this one is not.
    assert "20240517120000123" in out["b"]


def test_ssn_with_hyphens_redacted():
    out = scrub_values({"message": "ssn 123-45-6789 on file"})
    assert "123-45-6789" not in out["message"]
    assert REDACTED in out["message"]


def test_bare_nine_digit_number_not_treated_as_ssn():
    # SSN requires hyphens — a bare 9-digit number is NOT an SSN match.
    out = scrub_values({"message": "ref 123456789 ok"})
    assert "123456789" in out["message"]


def test_email_redacted_when_send_default_pii_false():
    out = scrub_values({"message": "contact alice@example.com"}, send_default_pii=False)
    assert "alice@example.com" not in out["message"]
    assert REDACTED in out["message"]


def test_email_preserved_when_send_default_pii_true():
    out = scrub_values({"message": "contact alice@example.com"}, send_default_pii=True)
    assert out["message"] == "contact alice@example.com"


def test_ipv4_redacted_when_send_default_pii_false():
    out = scrub_values({"message": "from 192.168.1.10 ok"}, send_default_pii=False)
    assert "192.168.1.10" not in out["message"]
    assert REDACTED in out["message"]


def test_ipv4_preserved_when_send_default_pii_true():
    out = scrub_values({"message": "from 192.168.1.10 ok"}, send_default_pii=True)
    assert out["message"] == "from 192.168.1.10 ok"


def test_invalid_ipv4_octet_not_redacted():
    # 999 is not a valid octet — version-like string must survive.
    out = scrub_values({"message": "version 1.2.999.4"}, send_default_pii=False)
    assert out["message"] == "version 1.2.999.4"


def test_cc_and_ssn_always_redacted_even_with_send_default_pii_true():
    # Layer (A) is unconditional — opting into PII does NOT allow CC/SSN.
    out = scrub_values(
        {"a": f"card {_VALID_CC}", "b": "ssn 123-45-6789"},
        send_default_pii=True,
    )
    assert _VALID_CC not in out["a"]
    assert "123-45-6789" not in out["b"]


def test_explicit_user_object_is_not_value_scrubbed():
    # setUser email/ip ship as-is (matches Sentry: send_default_pii never
    # strips explicitly-set user data).
    event = {"user": {"id": "u1", "email": "alice@example.com", "ip": "192.168.1.10"}}
    out = scrub_values(event, send_default_pii=False)
    assert out["user"]["email"] == "alice@example.com"
    assert out["user"]["ip"] == "192.168.1.10"


def test_stack_frame_paths_not_corrupted():
    # Stack-frame filename/absPath/function must survive untouched even if they
    # look like they could match (they won't, but the skip-key is the guarantee).
    event = {
        "stackTrace": ["File \"/srv/app/main.py\", line 10, in handler"],
        "frames": [
            {
                "filename": "/srv/app/2.3.4.5/main.py",
                "absPath": "/srv/app/2.3.4.5/main.py",
                "function": "do_thing",
                "lineno": 10,
            }
        ],
    }
    out = scrub_values(event, send_default_pii=False)
    assert out["stackTrace"] == event["stackTrace"]
    assert out["frames"][0]["filename"] == "/srv/app/2.3.4.5/main.py"
    assert out["frames"][0]["absPath"] == "/srv/app/2.3.4.5/main.py"


def test_release_and_url_keys_not_scrubbed():
    event = {
        "release": "1.2.3.4",          # IPv4-shaped but a release identifier
        "url": "http://10.0.0.1/path",  # URLs have their own redactor
        "sessionId": "192.168.0.1",     # SDK correlation key
        "metadata": {"note": "ip 10.0.0.5 leaked"},
    }
    out = scrub_values(event, send_default_pii=False)
    assert out["release"] == "1.2.3.4"
    assert out["url"] == "http://10.0.0.1/path"
    assert out["sessionId"] == "192.168.0.1"
    # ...but free-text metadata IS scrubbed.
    assert "10.0.0.5" not in out["metadata"]["note"]


def test_value_scrub_covers_breadcrumbs_and_metadata():
    event = {
        "message": "user bob@corp.io hit error",
        "metadata": {"extra": "ip 8.8.8.8"},
        "breadcrumbs": [{"message": "called carol@x.org", "data": {"note": "1.1.1.1"}}],
    }
    out = scrub_values(event, send_default_pii=False)
    assert "bob@corp.io" not in out["message"]
    assert "8.8.8.8" not in out["metadata"]["extra"]
    assert "carol@x.org" not in out["breadcrumbs"][0]["message"]
    assert "1.1.1.1" not in out["breadcrumbs"][0]["data"]["note"]


def test_value_scrub_fail_open_on_pathological_input():
    # A huge string is skipped gracefully (returned unchanged), never raises.
    big = "x" * 40_000 + " alice@example.com"
    out = scrub_values({"message": big}, send_default_pii=False)
    # Over the scan cap → preserved verbatim (fail-open, no crash).
    assert out["message"] == big


def test_value_scrub_handles_deeply_nested_without_crashing():
    node: dict = {"v": "alice@example.com"}
    root = node
    for _ in range(60):
        node = {"child": node}
    out = scrub_values(node, send_default_pii=False)
    assert isinstance(out, dict)  # did not raise


def test_value_scrub_cycle_safe():
    d: dict = {"message": "ip 9.9.9.9"}
    d["self"] = d
    out = scrub_values(d, send_default_pii=False)
    assert "9.9.9.9" not in out["message"]


def test_value_scrub_primitive_passthrough():
    assert scrub_values(42) == 42
    assert scrub_values(None) is None
    assert scrub_values("plain text") == "plain text"


def test_key_based_redaction_still_works_alongside_values():
    # Layer 1 (key) and layer 2 (value) compose: password redacted by key,
    # email redacted by value.
    event = {"password": "hunter2", "message": "mail to dan@x.com"}
    out = scrub_values(scrub(event), send_default_pii=False)
    assert out["password"] == REDACTED
    assert "dan@x.com" not in out["message"]


def test_user_in_skip_keys():
    assert "user" in VALUE_SCRUB_SKIP_KEYS
    assert "stacktrace" in VALUE_SCRUB_SKIP_KEYS
    assert "frames" in VALUE_SCRUB_SKIP_KEYS
