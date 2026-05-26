"""AllStak integrations for popular Python frameworks.

Install helpers are exposed lazily so importing this package never requires an
optional dependency (celery, requests, httpx, ...) to be present.
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "install_celery",
    "install_logging",
    "AllStakLoggingHandler",
    "install_requests",
    "install_httpx",
]


def __getattr__(name: str) -> Any:  # PEP 562 lazy attribute access
    if name == "install_celery":
        from .celery import install_celery

        return install_celery
    if name in ("install_logging", "AllStakLoggingHandler"):
        from . import logging as _logging

        return getattr(_logging, name)
    if name == "install_requests":
        from .requests import install_requests

        return install_requests
    if name == "install_httpx":
        from .httpx import install_httpx

        return install_httpx
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
