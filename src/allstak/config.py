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

    host: str = "https://api.allstak.sa"
    """Base URL of the AllStak backend, without trailing slash."""

    # --- Optional context ---
    environment: Optional[str] = None
    """Deployment environment, e.g. ``"production"``, ``"staging"``."""

    release: Optional[str] = None
    """App version or release tag, e.g. ``"v1.4.2"``."""

    # --- Release-tracking metadata (optional, auto-detected when possible) ---
    dist: Optional[str] = None
    """Build distribution tag (e.g. ``"linux"``, ``"darwin"``)."""

    commit_sha: Optional[str] = None
    """Git commit SHA the running build was built from."""

    branch: Optional[str] = None
    """Git branch the running build was built from."""

    platform: Optional[str] = None
    """Runtime platform — auto-set to ``"python"`` if left None."""

    sdk_name: Optional[str] = None
    """SDK package name — auto-set to ``"allstak-python"``."""

    sdk_version: Optional[str] = None
    """SDK semver — auto-set from package metadata when left None."""

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
            host=os.environ.get("ALLSTAK_HOST", "https://api.allstak.sa"),
            environment=os.environ.get("ALLSTAK_ENVIRONMENT"),
            release=os.environ.get("ALLSTAK_RELEASE"),
            debug=bool(os.environ.get("ALLSTAK_DEBUG", "")),
        )

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("AllStak SDK: api_key must not be empty")
        # Strip trailing slash so we can always do host + "/ingest/..."
        self.host = self.host.rstrip("/")
        self._apply_release_autodetect()

    def _apply_release_autodetect(self) -> None:
        """
        Best-effort population of release-tracking metadata from CI/runtime
        env vars. Explicit user values always win — we only fill in fields
        the caller left unset. Never raises; if env access fails (e.g. in a
        sandboxed embedder), we leave the field as ``None``.

        Auto-detect order mirrors the JS SDK so behaviour is consistent
        regardless of language.
        """
        try:
            if not self.platform:
                self.platform = "python"
            if not self.sdk_name:
                self.sdk_name = "allstak-python"
            if not self.sdk_version:
                try:
                    from importlib.metadata import version as _v
                    self.sdk_version = _v("allstak")
                except Exception:
                    self.sdk_version = None
            if not self.release:
                self.release = (
                    os.environ.get("ALLSTAK_RELEASE")
                    or os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:12] or None
                    or os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:12] or None
                    or os.environ.get("RENDER_GIT_COMMIT", "")[:12] or None
                )
            if not self.commit_sha:
                self.commit_sha = (
                    os.environ.get("ALLSTAK_COMMIT_SHA")
                    or os.environ.get("GIT_COMMIT")
                    or os.environ.get("VERCEL_GIT_COMMIT_SHA")
                    or os.environ.get("RAILWAY_GIT_COMMIT_SHA")
                    or os.environ.get("RENDER_GIT_COMMIT")
                )
            if not self.branch:
                self.branch = (
                    os.environ.get("ALLSTAK_BRANCH")
                    or os.environ.get("GIT_BRANCH")
                    or os.environ.get("VERCEL_GIT_COMMIT_REF")
                    or os.environ.get("RAILWAY_GIT_BRANCH")
                )
            if not self.environment:
                self.environment = (
                    os.environ.get("ALLSTAK_ENVIRONMENT")
                    or os.environ.get("APP_ENV")
                    or "production"
                )
        except Exception:
            # Auto-detection is best-effort; never break SDK init.
            pass

    def release_tags(self) -> dict:
        """Return the release-metadata dict to merge into outgoing event payloads."""
        out: dict = {}
        if self.sdk_name: out["sdk.name"] = self.sdk_name
        if self.sdk_version: out["sdk.version"] = self.sdk_version
        if self.platform: out["platform"] = self.platform
        if self.dist: out["dist"] = self.dist
        if self.commit_sha: out["commit.sha"] = self.commit_sha
        if self.branch: out["commit.branch"] = self.branch
        return out
