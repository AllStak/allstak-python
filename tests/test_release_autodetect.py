"""Tests for runtime release auto-detection.

Resolution order under test (highest first):
  1. Explicit ``release`` in config wins.
  2. Release env var (ALLSTAK_RELEASE etc.).
  3. Local git via ``git describe`` (cached, guarded).
  4. SDK version constant fallback.

The git logic is exercised through ``detect_release_from_git`` with a fake
runner so tests never depend on a real repository on disk.
"""

import pytest

import allstak.config as config_mod
from allstak.config import AllStakConfig, detect_release_from_git, _SDK_VERSION


@pytest.fixture(autouse=True)
def _clean_release_env(monkeypatch):
    # Strip every release-affecting env var so each test controls its own inputs.
    for var in (
        "ALLSTAK_RELEASE",
        "VERCEL_GIT_COMMIT_SHA",
        "RAILWAY_GIT_COMMIT_SHA",
        "RENDER_GIT_COMMIT",
    ):
        monkeypatch.delenv(var, raising=False)
    # Reset the process-wide git cache so one test's stub can't leak.
    monkeypatch.setattr(config_mod, "_git_release_cache", config_mod._NOT_RESOLVED)
    yield
    monkeypatch.setattr(config_mod, "_git_release_cache", config_mod._NOT_RESOLVED)


# --- detect_release_from_git: parsing & guarding ---------------------------

def test_describe_output_is_used_verbatim():
    runner = lambda args: "v1.4.2-3-gabc1234-dirty\n"
    assert detect_release_from_git(runner) == "v1.4.2-3-gabc1234-dirty"


def test_falls_back_to_short_sha_when_describe_empty_clean_tree():
    def runner(args):
        if args[0] == "describe":
            return ""  # describe yields nothing
        if args[0] == "rev-parse":
            return "abc1234\n"
        if args[0] == "status":
            return ""  # clean tree
        raise AssertionError(args)

    assert detect_release_from_git(runner) == "abc1234"


def test_short_sha_gets_dirty_suffix_when_tree_dirty():
    def runner(args):
        if args[0] == "describe":
            raise RuntimeError("no tags")
        if args[0] == "rev-parse":
            return "abc1234\n"
        if args[0] == "status":
            return " M src/allstak/config.py\n"  # dirty
        raise AssertionError(args)

    assert detect_release_from_git(runner) == "abc1234-dirty"


def test_returns_none_when_runner_raises():
    def runner(args):
        raise FileNotFoundError("git not installed")

    assert detect_release_from_git(runner) is None


def test_returns_none_when_everything_empty():
    assert detect_release_from_git(lambda args: "") is None


# --- AllStakConfig resolution order ----------------------------------------

def test_explicit_release_beats_everything(monkeypatch):
    monkeypatch.setenv("ALLSTAK_RELEASE", "from-env")
    monkeypatch.setattr(config_mod, "detect_release_from_git", lambda *a, **k: "from-git")
    cfg = AllStakConfig(api_key="k", release="explicit-1.0")
    assert cfg.release == "explicit-1.0"


def test_env_beats_git(monkeypatch):
    monkeypatch.setenv("ALLSTAK_RELEASE", "env-release")
    monkeypatch.setattr(config_mod, "detect_release_from_git", lambda *a, **k: "git-release")
    cfg = AllStakConfig(api_key="k")
    assert cfg.release == "env-release"


def test_git_beats_version(monkeypatch):
    monkeypatch.setattr(config_mod, "detect_release_from_git", lambda *a, **k: "v9.9.9-git")
    cfg = AllStakConfig(api_key="k")
    assert cfg.release == "v9.9.9-git"


def test_version_fallback_when_no_git(monkeypatch):
    monkeypatch.setattr(config_mod, "detect_release_from_git", lambda *a, **k: None)
    cfg = AllStakConfig(api_key="k")
    # sdk_version is auto-set from package metadata; when unavailable it falls
    # back to the constant. Either way release is non-empty.
    assert cfg.release in (cfg.sdk_version, _SDK_VERSION)
    assert cfg.release


def test_opt_out_disables_git_and_version(monkeypatch):
    monkeypatch.setattr(config_mod, "detect_release_from_git", lambda *a, **k: "git-release")
    cfg = AllStakConfig(api_key="k", auto_detect_release=False)
    assert cfg.release is None


def test_opt_out_still_honors_explicit_and_env(monkeypatch):
    monkeypatch.setenv("ALLSTAK_RELEASE", "env-release")
    monkeypatch.setattr(config_mod, "detect_release_from_git", lambda *a, **k: "git-release")
    cfg = AllStakConfig(api_key="k", auto_detect_release=False)
    assert cfg.release == "env-release"


def test_git_runner_raising_is_graceful_in_full_config(monkeypatch):
    # No env, real detect path, but the runner blows up — must not raise and
    # must fall through to the version constant.
    def boom(args):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(config_mod, "_default_git_runner", boom)
    cfg = AllStakConfig(api_key="k")
    assert cfg.release  # version fallback, never empty
