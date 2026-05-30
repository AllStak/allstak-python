"""
Django ``AppConfig`` for AllStak — auto-wires inbound request capture.

Add ``"allstak.integrations.django_app.AllStakAppConfig"`` (or simply
``"allstak"`` once :mod:`allstak.apps` re-exports it) to ``INSTALLED_APPS`` and
the SDK inserts :class:`allstak.integrations.django.AllStakMiddleware` at the
front of ``settings.MIDDLEWARE`` automatically during app initialization. No
manual ``MIDDLEWARE`` edit is required::

    INSTALLED_APPS = [
        # ...
        "allstak",
    ]

    ALLSTAK = {
        "api_key": "ask_live_...",
        "environment": "production",
    }

Design notes
------------
* **Default-on, individually toggleable.** Auto-insertion is on by default and
  can be disabled with ``ALLSTAK = {"auto_middleware": False}`` (or the
  top-level ``ALLSTAK_AUTO_MIDDLEWARE = False`` setting) while still keeping the
  app installed for other behaviour. When off, the developer registers the
  middleware manually exactly as before — existing behaviour is preserved.
* **Idempotent.** If the middleware is already present under *either* the
  canonical ``AllStakMiddleware`` path or the legacy ``AllStakDjangoMiddleware``
  alias, nothing is inserted — so an app that both installs this AppConfig and
  lists the middleware manually never gets it twice.
* **Fail-open.** Any error mutating ``settings.MIDDLEWARE`` is swallowed; a
  misconfigured observability layer must never stop Django from booting.
* **Front insertion.** The middleware is inserted at index 0 so it wraps the
  whole stack (it opens the request span and records the outermost timing),
  matching the manual-setup guidance.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("allstak.sdk")

try:
    from django.apps import AppConfig

    _DJANGO_AVAILABLE = True
except ImportError:  # pragma: no cover - only without Django installed
    _DJANGO_AVAILABLE = False
    AppConfig = object  # type: ignore[assignment,misc]


def _auto_middleware_enabled() -> bool:
    """Whether middleware auto-insertion is enabled (default True).

    Honours ``ALLSTAK["auto_middleware"]`` first, then a top-level
    ``ALLSTAK_AUTO_MIDDLEWARE`` setting, defaulting to ``True``. Fail-open: any
    error reading settings returns the default (enabled).
    """
    try:
        from django.conf import settings

        block = getattr(settings, "ALLSTAK", None) or {}
        if isinstance(block, dict) and "auto_middleware" in block:
            return bool(block["auto_middleware"])
        return bool(getattr(settings, "ALLSTAK_AUTO_MIDDLEWARE", True))
    except Exception:
        return True


def ensure_middleware_installed() -> bool:
    """Insert ``AllStakMiddleware`` at the front of ``settings.MIDDLEWARE``.

    Returns ``True`` when the middleware was inserted by this call, ``False``
    when it was already present, auto-insertion is disabled, or insertion was
    skipped/failed. Idempotent and fully fail-open — safe to call repeatedly.
    """
    if not _DJANGO_AVAILABLE:
        return False
    if not _auto_middleware_enabled():
        return False
    try:
        from django.conf import settings

        from .django import _MIDDLEWARE_ALIAS_PATH, _MIDDLEWARE_PATH

        current = list(getattr(settings, "MIDDLEWARE", None) or [])
        # Already wired under either the canonical or the legacy alias path.
        if _MIDDLEWARE_PATH in current or _MIDDLEWARE_ALIAS_PATH in current:
            return False
        settings.MIDDLEWARE = [_MIDDLEWARE_PATH, *current]
        logger.debug(
            "[AllStak] auto-inserted %s into MIDDLEWARE", _MIDDLEWARE_PATH
        )
        return True
    except Exception as exc:  # pragma: no cover - never break Django boot
        logger.debug("[AllStak] auto middleware insertion failed: %s", exc)
        return False


class AllStakAppConfig(AppConfig):
    """AllStak Django application config.

    ``ready()`` runs once during Django startup and auto-wires the request
    middleware so inbound capture needs no manual ``MIDDLEWARE`` edit.
    """

    name = "allstak"
    label = "allstak"
    verbose_name = "AllStak"

    def ready(self) -> None:  # noqa: D401 - Django hook
        ensure_middleware_installed()
