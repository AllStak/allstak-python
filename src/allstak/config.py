"""AllStak SDK configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AllStakConfig:
    """
    SDK configuration.  All settings have sane defaults; only ``api_key``
    and ``host`` are required.
    """

    # --- Required ---
    api_key: str
    """Raw API key sent as ``X-AllStak-Key``.  Never hash it — the backend does that."""

    host: str = "http://localhost:8080"
    """Base URL of the AllStak backend, without trailing slash."""

    # --- Optional context ---
    environment: Optional[str] = None
    """Deployment environment, e.g. ``"production"``, ``"staging"``."""

    release: Optional[str] = None
    """App version or release tag, e.g. ``"v1.4.2"``."""

    # --- Behaviour tuning ---
    flush_interval_ms: int = 5_000
    """How often (ms) the background flush timer fires.  Default: 5 000 ms."""

    buffer_size: int = 500
    """Maximum items held per feature buffer before oldest is evicted."""

    debug: bool = False
    """When True, SDK logs all outgoing payloads and responses to stderr."""

    # --- Network ---
    connect_timeout: float = 3.0
    """TCP connect timeout in seconds."""

    read_timeout: float = 3.0
    """Socket read timeout in seconds."""

    max_retries: int = 5
    """Maximum send attempts before discarding an event."""

    # --- Auto breadcrumbs ---
    auto_breadcrumbs: bool = True
    """When True, automatically instrument ``requests`` library and logging for breadcrumbs."""

    max_breadcrumbs: int = 50
    """Maximum number of breadcrumbs kept in the ring buffer."""

    @classmethod
    def from_env(cls) -> "AllStakConfig":
        """
        Construct config from environment variables::

            ALLSTAK_API_KEY      → api_key  (required)
            ALLSTAK_HOST         → host
            ALLSTAK_ENVIRONMENT  → environment
            ALLSTAK_RELEASE      → release
            ALLSTAK_DEBUG        → debug (any truthy string)
        """
        api_key = os.environ.get("ALLSTAK_API_KEY", "")
        if not api_key:
            raise ValueError(
                "ALLSTAK_API_KEY environment variable is required"
            )
        return cls(
            api_key=api_key,
            host=os.environ.get("ALLSTAK_HOST", "http://localhost:8080"),
            environment=os.environ.get("ALLSTAK_ENVIRONMENT"),
            release=os.environ.get("ALLSTAK_RELEASE"),
            debug=bool(os.environ.get("ALLSTAK_DEBUG", "")),
        )

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("AllStak SDK: api_key must not be empty")
        # Strip trailing slash so we can always do host + "/ingest/..."
        self.host = self.host.rstrip("/")
