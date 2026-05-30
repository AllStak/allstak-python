"""Tests for the Django AppConfig auto-wiring of ``AllStakMiddleware``.

Django settings are a process singleton, so these tests exercise the
auto-insertion logic (:func:`ensure_middleware_installed`) directly against a
mutated ``settings.MIDDLEWARE`` list rather than re-running ``settings.configure``.
The AppConfig's ``ready()`` is a thin wrapper over that function.

Asserted behaviour:
  * the canonical middleware path is inserted at the FRONT of MIDDLEWARE;
  * insertion is idempotent under either the canonical or the legacy alias path;
  * ``auto_middleware=False`` (block or top-level setting) opts out;
  * the legacy ``AllStakDjangoMiddleware`` name aliases the canonical class.
"""
from __future__ import annotations

import pytest

# Django settings are a process singleton. Import the integration test module
# first so IT owns ``settings.configure`` (with the real urlconf + the
# AllStakMiddleware registered) regardless of collection order — these tests
# only need a mutable ``settings.MIDDLEWARE`` to operate on, while the
# integration suite needs its specific urlconf/middleware. Sharing one configured
# settings object keeps both suites green together.
import tests.test_django_integration  # noqa: F401,E402  (configures Django)
from django.conf import settings  # noqa: E402

from allstak.integrations.django import (  # noqa: E402
    AllStakDjangoMiddleware,
    AllStakMiddleware,
    _MIDDLEWARE_ALIAS_PATH,
    _MIDDLEWARE_PATH,
)
from allstak.integrations.django_app import (  # noqa: E402
    AllStakAppConfig,
    ensure_middleware_installed,
)


@pytest.fixture
def restore_middleware():
    """Snapshot/restore the global ``settings.MIDDLEWARE`` + ALLSTAK block."""
    saved_mw = list(getattr(settings, "MIDDLEWARE", []) or [])
    saved_block = getattr(settings, "ALLSTAK", None)
    saved_flag = getattr(settings, "ALLSTAK_AUTO_MIDDLEWARE", None)
    yield
    settings.MIDDLEWARE = saved_mw
    if saved_block is not None:
        settings.ALLSTAK = saved_block
    if saved_flag is None:
        if hasattr(settings, "ALLSTAK_AUTO_MIDDLEWARE"):
            delattr(settings, "ALLSTAK_AUTO_MIDDLEWARE")
    else:
        settings.ALLSTAK_AUTO_MIDDLEWARE = saved_flag


def test_legacy_alias_is_the_same_class():
    assert AllStakDjangoMiddleware is AllStakMiddleware


def test_inserts_middleware_at_front(restore_middleware):
    settings.MIDDLEWARE = ["other.Middleware"]
    settings.ALLSTAK = {"api_key": "ask_test"}

    inserted = ensure_middleware_installed()
    assert inserted is True
    assert settings.MIDDLEWARE[0] == _MIDDLEWARE_PATH
    assert "other.Middleware" in settings.MIDDLEWARE


def test_insertion_is_idempotent_canonical(restore_middleware):
    settings.MIDDLEWARE = [_MIDDLEWARE_PATH, "other.Middleware"]
    settings.ALLSTAK = {"api_key": "ask_test"}

    inserted = ensure_middleware_installed()
    assert inserted is False
    # Not duplicated.
    assert settings.MIDDLEWARE.count(_MIDDLEWARE_PATH) == 1


def test_insertion_is_idempotent_legacy_alias_path(restore_middleware):
    # A developer who wrote the legacy dotted path manually must not get a
    # second (canonical) entry inserted.
    settings.MIDDLEWARE = [_MIDDLEWARE_ALIAS_PATH, "other.Middleware"]
    settings.ALLSTAK = {"api_key": "ask_test"}

    inserted = ensure_middleware_installed()
    assert inserted is False
    assert _MIDDLEWARE_PATH not in settings.MIDDLEWARE
    assert settings.MIDDLEWARE.count(_MIDDLEWARE_ALIAS_PATH) == 1


def test_opt_out_via_block_flag(restore_middleware):
    settings.MIDDLEWARE = ["other.Middleware"]
    settings.ALLSTAK = {"api_key": "ask_test", "auto_middleware": False}

    inserted = ensure_middleware_installed()
    assert inserted is False
    assert _MIDDLEWARE_PATH not in settings.MIDDLEWARE


def test_opt_out_via_top_level_setting(restore_middleware):
    settings.MIDDLEWARE = ["other.Middleware"]
    settings.ALLSTAK = {"api_key": "ask_test"}
    settings.ALLSTAK_AUTO_MIDDLEWARE = False

    inserted = ensure_middleware_installed()
    assert inserted is False
    assert _MIDDLEWARE_PATH not in settings.MIDDLEWARE


def test_app_config_ready_inserts(restore_middleware):
    settings.MIDDLEWARE = []
    settings.ALLSTAK = {"api_key": "ask_test"}

    # Drive the AppConfig.ready() hook (constructed against the app module).
    import allstak

    cfg = AllStakAppConfig("allstak", allstak)
    cfg.ready()
    assert settings.MIDDLEWARE and settings.MIDDLEWARE[0] == _MIDDLEWARE_PATH
