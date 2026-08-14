"""Shared test setup."""

import pytest

from lazyslurm import config as persistent_config
from lazyslurm import slurm


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):
    """Never let a test read or write the real ~/.config/lazyslurm.

    The paths are module-level, so anything reaching them without saying so
    lands in the developer's own config — which is not merely untidy: a cache
    written by a newer LazySlurm decides whether unrelated tests pass, and
    since #67 the config file itself would be rewritten.
    """
    config_dir = tmp_path / "lazyslurm-config"
    config_dir.mkdir()
    monkeypatch.setattr(persistent_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(persistent_config, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(persistent_config, "BACKUP_FILE", config_dir / "config.toml.bak")
    monkeypatch.setattr(persistent_config, "LOG_CACHE_FILE", config_dir / "log_cache.json")
    monkeypatch.setattr(persistent_config, "SCRIPT_CACHE_DIR", config_dir / "scripts")


@pytest.fixture(autouse=True)
def _reset_slurm_caches():
    """Start every test with nothing remembered from the last one.

    The poll caches live on the module, and the module is imported once for the
    whole session -- so without this a test that stubs `_run_cmd` can be served
    the answer a previous test's stub gave.
    """
    slurm.reset_caches()
    yield
    slurm.reset_caches()
