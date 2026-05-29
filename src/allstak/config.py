"""AllStak SDK configuration."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


# SDK version constant — the step-4 release fallback. Kept in lockstep with
# pyproject.toml / __init__.__version__.
_SDK_VERSION = "0.1.2"


# A "git runner" takes a list of git arguments (without the leading "git") and
# returns the command's stdout as a string. Splitting it out this way keeps the
# parsing logic pure and seamable: tests inject a fake runner instead of relying
# on a real repository on disk.
GitRunner = Callable[[list], str]

# Cache the git-derived release for the lifetime of the process so we only shell
# out once regardless of how many configs are constructed. ``_NOT_RESOLVED`` is a
# distinct sentinel so a real ``None`` result (no repo) is still cached and not
# re-attempted.
_NOT_RESOLVED = object()
_git_release_cache: Any = _NOT_RESOLVED


def _cached_git_release() -> Optional[str]:
    """Resolve the git release once and cache it for the process lifetime."""
    global _git_release_cache
    if _git_release_cache is _NOT_RESOLVED:
        try:
            _git_release_cache = detect_release_from_git()
        except Exception:
            _git_release_cache = None
    return _git_release_cache


def _default_git_runner(args: list) -> str:
    """Shell out to the real ``git`` binary from the process working directory.

    Uses a short timeout and raises on any non-zero exit so the caller can treat
    every failure mode (git missing, no repo, timeout) uniformly. Never used in
    unit tests — they pass a fake runner.
    """
    completed = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        timeout=2.0,
        check=True,
    )
    return completed.stdout


def detect_release_from_git(runner: GitRunner = _default_git_runner) -> Optional[str]:
    """Best-effort release string derived from the local git checkout.

    Resolution:
      1. ``git describe --tags --always --dirty`` (preferred — gives the nearest
         tag, or an abbreviated SHA, with a ``-dirty`` suffix on a dirty tree).
      2. If that fails, ``git rev-parse --short HEAD`` and append ``-dirty`` when
         ``git status --porcelain`` reports any uncommitted changes.

    Fully guarded: if the runner raises (git missing, no ``.git``, timeout) or
    returns empty for both strategies, returns ``None``. Never raises.

    NOTE on production honesty: a deployed artifact (wheel / container layer)
    usually has no ``.git`` directory, so this returns ``None`` there — the
    version-constant fallback becomes the effective release. Runtime git
    detection mainly helps source/dev deployments that run inside a checkout.
    """
    try:
        described = runner(["describe", "--tags", "--always", "--dirty"]).strip()
        if described:
            return described
    except Exception:
        pass

    try:
        sha = runner(["rev-parse", "--short", "HEAD"]).strip()
        if not sha:
            return None
        try:
            status = runner(["status", "--porcelain"])
        except Exception:
            status = ""
        if status.strip():
            return f"{sha}-dirty"
        return sha
    except Exception:
        return None


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

    auto_detect_release: bool = True
    """When True (default), and no explicit ``release`` or release env var is
    found, the SDK tries local git (``git describe``) once at init and finally
    falls back to the SDK version constant so ``release`` is never empty. Set
    False to disable the git + version-constant fallbacks (explicit value and
    release env vars still apply)."""

    auto_register_release: bool = True
    """When True (default), the SDK registers the resolved release with
    AllStak at runtime startup via ``/ingest/v1/releases``. This is best-effort
    and does not require CI/CD integration."""

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

    # --- Release-health session tracking ---
    enable_auto_session_tracking: bool = True
    """When True (default), the SDK opens one release-health session for the
    running process at init (POST ``/ingest/v1/sessions/start``) and closes it
    on graceful shutdown (POST ``/ingest/v1/sessions/end``) with the final
    crash-free status. Set False to opt out entirely. Session tracking is
    always fail-open and is automatically skipped under a unit-test runtime."""

    # --- Offline / persistent event queue (survive restart + outage) ---
    offline_storage: bool = True
    """When True (default), telemetry that cannot be delivered (network down,
    retries exhausted, or buffered at shutdown) is written PII-scrubbed to a
    filesystem spool and replayed on the next SDK init — the server analogue of
    Sentry's offline envelope cache. Only error/log/span/http/db telemetry is
    persisted; session lifecycle calls are live-only. Always fail-open: if the
    spool directory is unwritable (read-only FS, serverless, sandbox) the SDK
    silently falls back to its in-memory behaviour. Set False to disable."""

    offline_queue_dir: Optional[str] = None
    """Override the spool directory. When None (default) a per-backend directory
    under the system temp dir is used (``<tmp>/allstak-spool/<host-hash>``)."""

    offline_max_events: int = 100
    """Maximum number of persisted events kept on disk. Oldest are dropped when
    full."""

    offline_max_bytes: int = 5 * 1024 * 1024
    """Maximum total bytes of the spool (default ~5 MiB). Oldest are dropped
    when over budget."""

    offline_max_age_s: float = 48 * 3600
    """Maximum age (seconds) of a persisted event before it is dropped on the
    next bound-enforcement pass (default 48h)."""

    # --- Uncaught exception capture ---
    install_excepthook: bool = True
    """When True, install ``sys.excepthook`` to capture uncaught exceptions
    on the main thread (scripts, top-level code outside any request)."""

    install_threading_excepthook: bool = True
    """When True, install ``threading.excepthook`` to capture uncaught
    exceptions raised inside background threads (Python 3.8+)."""

    # --- Privacy / data scrubbing ---
    send_default_pii: bool = False
    """Sentry-parity PII toggle. Default ``False`` (privacy-preserving).

    Layer-1 key-name redaction (password/token/cookie/...) and the ALWAYS-ON
    value scrubbers (credit-card numbers validated by Luhn, US SSNs) run
    regardless of this flag — high-risk financial/identity data is never
    shipped in telemetry.

    When ``False`` (default): e-mail addresses and IPv4 addresses found in
    free-text string values (error/log messages, metadata/extra/contexts
    values, breadcrumb message+data, captured HTTP/DB fields) are replaced with
    ``[REDACTED]``, and any auto-collected client IP the SDK attaches is
    dropped/masked.

    When ``True``: the operator has opted into PII, so the e-mail / IPv4 value
    scrubbers are disabled and auto-collected client IP is allowed.

    Note: ``send_default_pii`` does NOT strip data on the explicitly-set user
    object (``set_user(id=..., email=..., ip=...)``) — that identification is
    intentional and ships as before, matching Sentry."""

    # --- Event processing & sampling ---
    before_send: Optional[Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]] = None
    """Optional hook called once just before an error/message event is handed
    to the transport. Receives the structured event payload dict and returns a
    (possibly modified) event dict, or ``None`` to drop the event entirely.

    Runs for both exception and message capture. If the callback raises, the
    SDK logs the failure and falls back to sending the original event
    (fail-open) — a user callback must never crash capture."""

    sample_rate: float = 1.0
    """Probabilistic sample rate for error/message events in ``[0.0, 1.0]``.
    ``1.0`` keeps everything (default); ``0.0`` drops everything. The drop
    decision (``random.random() >= sample_rate``) happens before
    ``before_send`` runs, so dropped events never reach the callback."""

    traces_sample_rate: Optional[float] = None
    """Probabilistic sample rate for spans/transactions in ``[0.0, 1.0]``.
    When ``None`` (default), tracing is always-on (backward compatible). When
    set, span/transaction creation is sampled and the sampled decision drives
    the propagated ``traceparent`` sampled flag (``-01`` sampled / ``-00``
    not sampled)."""

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
        # Clamp sample rates into [0.0, 1.0] so a bad value can't silently
        # drop everything or wrap around.
        try:
            self.sample_rate = max(0.0, min(1.0, float(self.sample_rate)))
        except (TypeError, ValueError):
            self.sample_rate = 1.0
        if self.traces_sample_rate is not None:
            try:
                self.traces_sample_rate = max(0.0, min(1.0, float(self.traces_sample_rate)))
            except (TypeError, ValueError):
                self.traces_sample_rate = None
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
                # 2. Existing env-var detection (unchanged).
                self.release = (
                    os.environ.get("ALLSTAK_RELEASE")
                    or os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:12] or None
                    or os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:12] or None
                    or os.environ.get("RENDER_GIT_COMMIT", "")[:12] or None
                )
            if not self.release and self.auto_detect_release:
                # 3. Local git at init (cached, fully guarded — see
                #    detect_release_from_git for the production-honesty note).
                self.release = _cached_git_release()
            if not self.release and self.auto_detect_release:
                # 4. Final fallback: the SDK's own version so release is never
                #    empty. In a deployed artifact without a .git this is the
                #    effective release.
                self.release = self.sdk_version or _SDK_VERSION
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
