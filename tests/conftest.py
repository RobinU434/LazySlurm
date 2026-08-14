"""Shared test setup."""

import pytest

from lazyslurm import slurm


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
