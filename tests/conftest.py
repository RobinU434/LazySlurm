"""Shared test setup."""

import pytest

from lazyslurm import config as persistent_config
from lazyslurm import slurm


@pytest.fixture(autouse=True)
def _reset_slurm_caches():
    """Start every test with nothing remembered from the last one.

    The poll caches live on the module, and the module is imported once for the
    whole session -- so without this a test that stubs `_run_cmd` can be served
    the answer a previous test's stub gave.
    """
    persistent_config.set_cluster(persistent_config.DEFAULT_CLUSTER)
    slurm.reset_caches()
    yield
    slurm.reset_caches()
