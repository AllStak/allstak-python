"""
Feature flags module.

NOTE: Feature flag evaluation uses the management API (/api/v1/flags/evaluate)
which requires an OAuth2 Bearer JWT token — NOT an API key.

This module is designed for server-side SDKs that have access to a
management JWT.  Client-side SDKs (browser, mobile) should proxy flag
evaluation through their own backend.

If no bearer_token is provided, this module operates in a degraded mode
that returns default values for all flags.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, Optional

import httpx

from ..config import AllStakConfig

logger = logging.getLogger("allstak.sdk")

_EVALUATE_ALL_PATH = "/api/v1/flags/evaluate"
_EVALUATE_ONE_PATH = "/api/v1/flags/{key}/evaluate"
_CACHE_TTL_SECONDS = 60


class FlagResult:
    """Result of a single feature flag evaluation."""

    def __init__(self, key: str, enabled: bool, value: str, rule_applied: Optional[str] = None) -> None:
        self.key = key
        self.enabled = enabled
        self._value = value
        self.rule_applied = rule_applied

    def as_bool(self) -> bool:
        """Return value cast to bool."""
        return self._value.lower() in ("true", "1", "yes")

    def as_str(self) -> str:
        """Return value as string."""
        return self._value

    def as_int(self) -> int:
        """Return value cast to int (raises ValueError if not numeric)."""
        return int(self._value)

    def as_float(self) -> float:
        """Return value cast to float."""
        return float(self._value)

    def __repr__(self) -> str:
        return (
            f"FlagResult(key={self.key!r}, enabled={self.enabled}, "
            f"value={self._value!r}, rule={self.rule_applied!r})"
        )


class FeatureFlagModule:
    """
    Evaluates feature flags via the AllStak management API.

    Requires ``bearer_token`` (OAuth2 JWT) — not the ingestion API key.
    Results are cached for ``_CACHE_TTL_SECONDS`` seconds.

    If no bearer_token is configured, all flag evaluations return the
    ``default`` value provided by the caller (fail-open mode).
    """

    def __init__(
        self,
        config: AllStakConfig,
        bearer_token: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> None:
        self._config = config
        self._bearer_token = bearer_token
        self._project_id = project_id
        self._cache: Dict[str, Any] = {}
        self._cache_ts: float = 0.0
        self._lock = threading.Lock()
        self._timeout = httpx.Timeout(connect=3.0, read=3.0, write=3.0, pool=3.0)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_all(
        self,
        *,
        user_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, FlagResult]:
        """
        Evaluate all feature flags for the project.

        Uses a 60-second in-memory cache.
        Returns an empty dict on error (fail-open).

        :param user_id: User identifier for rollout targeting.
        :param attributes: Targeting attributes as a dict.
        """
        if not self._bearer_token or not self._project_id:
            logger.debug("[AllStak] Flags.get_all: no bearer_token/project_id configured")
            return {}
        try:
            raw = self._fetch_all(user_id=user_id, attributes=attributes or {})
            return {
                key: FlagResult(
                    key=key,
                    enabled=v.get("enabled", False),
                    value=str(v.get("value", "")),
                )
                for key, v in raw.items()
            }
        except Exception as exc:
            logger.debug("[AllStak] Flags.get_all failed: %s", exc)
            return {}

    def get(
        self,
        key: str,
        *,
        user_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
        default: Optional[str] = None,
    ) -> FlagResult:
        """
        Evaluate a single feature flag.

        Falls back to ``default`` on error.

        :param key: The flag key (e.g. ``"new-checkout-flow"``).
        :param user_id: User identifier for rollout targeting.
        :param attributes: Targeting attributes dict.
        :param default: Default value string if flag not found or error.
        """
        if not self._bearer_token or not self._project_id:
            logger.debug("[AllStak] Flags.get: no bearer_token/project_id configured")
            return FlagResult(key=key, enabled=False, value=default or "")

        try:
            return self._fetch_one(
                key=key, user_id=user_id, attributes=attributes or {}
            )
        except Exception as exc:
            logger.debug("[AllStak] Flags.get('%s') failed: %s", key, exc)
            return FlagResult(key=key, enabled=False, value=default or "")

    def invalidate_cache(self) -> None:
        """Force the next get_all() call to re-fetch from backend."""
        with self._lock:
            self._cache = {}
            self._cache_ts = 0.0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fetch_all(
        self,
        user_id: Optional[str],
        attributes: Dict[str, Any],
    ) -> Dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._cache and (now - self._cache_ts) < _CACHE_TTL_SECONDS:
                return self._cache

        params: Dict[str, Any] = {"projectId": self._project_id}
        if user_id:
            params["userId"] = user_id
        if attributes:
            params["attributes"] = json.dumps(attributes)

        data = self._management_get(_EVALUATE_ALL_PATH, params)
        flags = data.get("flags", {}) if isinstance(data, dict) else {}
        with self._lock:
            self._cache = flags
            self._cache_ts = time.monotonic()
        return flags

    def _fetch_one(
        self,
        key: str,
        user_id: Optional[str],
        attributes: Dict[str, Any],
    ) -> FlagResult:
        params: Dict[str, Any] = {"projectId": self._project_id}
        if user_id:
            params["userId"] = user_id
        if attributes:
            params["attributes"] = json.dumps(attributes)
        path = _EVALUATE_ONE_PATH.format(key=key)
        data = self._management_get(path, params)
        return FlagResult(
            key=data.get("key", key),
            enabled=bool(data.get("enabled", False)),
            value=str(data.get("value", "")),
            rule_applied=data.get("ruleApplied"),
        )

    def _management_get(
        self, path: str, params: Dict[str, Any]
    ) -> Dict[str, Any]:
        url = f"{self._config.host}{path}"
        headers = {"Authorization": f"Bearer {self._bearer_token}"}
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        return body.get("data", body)
