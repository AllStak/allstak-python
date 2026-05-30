"""
Django app autodiscovery shim for AllStak.

Listing ``"allstak"`` in ``INSTALLED_APPS`` makes Django look for a single
``AppConfig`` subclass in ``allstak.apps`` — this module re-exports
:class:`allstak.integrations.django_app.AllStakAppConfig` as that config so the
SDK can be installed as a Django app with one line::

    INSTALLED_APPS = [
        # ...
        "allstak",
    ]

The AppConfig's ``ready()`` auto-inserts :class:`AllStakMiddleware` into
``settings.MIDDLEWARE`` (default-on, toggleable via ``auto_middleware``), so no
manual middleware registration is required.

This module imports lazily and never raises at import time when Django is not
installed — importing :mod:`allstak` in a non-Django project stays safe.
"""

from __future__ import annotations

try:  # pragma: no branch - simple availability guard
    from .integrations.django_app import AllStakAppConfig

    # Django looks for exactly one AppConfig subclass in the app's ``apps``
    # module; exposing it here is what makes ``"allstak"`` a valid app label.
    __all__ = ["AllStakAppConfig"]
except Exception:  # pragma: no cover - only when Django is absent
    AllStakAppConfig = None  # type: ignore[assignment,misc]
    __all__ = []
