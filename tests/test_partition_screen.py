"""Opening the partition monitor (#72).

The screen used to await three Slurm calls -- one of them a cluster-wide
squeue -- before drawing anything, rebuild itself on every open, and fire a
squeue per row the cursor passed over. These cover what changed.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from lazyslurm import config as persistent_config
from lazyslurm import slurm
from lazyslurm.app import LazySlurmApp, PartitionScreen
from lazyslurm.models import Config
from textual.widgets import Static

_SINFO = (
    "gpu|up|4/2/0/6|10/20/0/30|1-00:00:00|gpu:4\n"
    "cpu|up|1/1/0/2|8/8/0/16|1-00:00:00|(null)\n"
)
_QUEUE_COUNTS = "gpu|RUNNING\ngpu|PENDING\ncpu|RUNNING\n"
_JOBS = "500|alice|train|RUNNING|1:00|2:00|1|8|gpu:1|node01\n"


class _Slurm:
    """_run_cmd stand-in that records commands and can stall one of them."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.block_squeue_counts = asyncio.Event()
        self.block_squeue_counts.set()

    async def __call__(self, *args: str):
        self.commands.append(" ".join(args))
        if args[0] == "sinfo":
            return _SINFO, "", 0
        if args[0] == "squeue" and "--format=%P|%T" in args:
            await self.block_squeue_counts.wait()
            return _QUEUE_COUNTS, "", 0
        if args[0] == "squeue":
            return _JOBS, "", 0
        return "", "", 0

    def count(self, needle: str) -> int:
        return sum(1 for c in self.commands if needle in c)


@pytest.fixture
def fake_slurm(monkeypatch, tmp_path):
    # Starting the app touches the config dir; point it somewhere empty so the
    # tests neither read nor rewrite the developer's own caches.
    monkeypatch.setattr(persistent_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(persistent_config, "CONFIG_FILE", tmp_path / "config.toml")
    monkeypatch.setattr(persistent_config, "LOG_CACHE_FILE", tmp_path / "log_cache.json")
    monkeypatch.setattr(persistent_config, "SCRIPT_CACHE_DIR", tmp_path / "scripts")
    fake = _Slurm()
    monkeypatch.setattr(slurm, "_run_cmd", fake)
    return fake


def _bar(screen) -> str:
    """The summary line, markup stripped."""
    return re.sub(r"\[/?[a-z0-9 #$_]*\]", "", str(screen.query_one("#partition-bar", Static).render()))


def _screen(app) -> PartitionScreen:
    assert isinstance(app.screen, PartitionScreen), f"not open: {app.screen}"
    return app.screen


def test_the_table_is_painted_before_the_cluster_wide_squeue(fake_slurm):
    """The slow call fills the running/pending column in afterwards."""

    async def scenario():
        app = LazySlurmApp(Config())
        async with app.run_test(size=(120, 40)) as pilot:
            fake_slurm.block_squeue_counts.clear()  # counts never answer
            await pilot.press("p")
            await pilot.pause()

            screen = _screen(app)
            table = screen.query_one("#partition-table")
            assert table.row_count == 2  # drawn without waiting for counts
            assert "counting jobs" in _bar(screen)

            fake_slurm.block_squeue_counts.set()
            for _ in range(20):
                await pilot.pause()
                if screen._counts_known:
                    break
            assert screen._counts_known
            assert "2 running" in _bar(screen)   # gpu 1 + cpu 1
            assert "1 pending" in _bar(screen)

    asyncio.run(scenario())


def test_moving_the_cursor_does_not_fire_a_squeue_per_row(fake_slurm):
    async def scenario():
        app = LazySlurmApp(Config())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("p")
            await pilot.pause()
            await asyncio.sleep(PartitionScreen.JOB_DEBOUNCE * 2)
            await pilot.pause()
            before = fake_slurm.count("squeue -p")

            # Sweep the cursor down and back without resting on a row.
            for key in ("down", "up", "down", "up"):
                await pilot.press(key)
            await pilot.pause()
            assert fake_slurm.count("squeue -p") == before

            # Resting on one fetches it, once.
            await asyncio.sleep(PartitionScreen.JOB_DEBOUNCE * 2)
            await pilot.pause()
            assert fake_slurm.count("squeue -p") <= before + 1

    asyncio.run(scenario())


def test_reopening_reuses_the_screen(fake_slurm):
    async def scenario():
        app = LazySlurmApp(Config())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("p")
            await pilot.pause()
            first = _screen(app)
            await pilot.press("escape")
            await pilot.pause()

            await pilot.press("p")
            await pilot.pause()
            assert _screen(app) is first
            # Already drawn on the way in, from what it had last time.
            assert first.query_one("#partition-table").row_count == 2

    asyncio.run(scenario())


def test_a_config_reload_drops_the_kept_screen(fake_slurm):
    async def scenario():
        app = LazySlurmApp(Config())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("p")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.is_screen_installed(app._PARTITION_SCREEN)

            app._reload_config()
            assert not app.is_screen_installed(app._PARTITION_SCREEN)

    asyncio.run(scenario())


def test_a_hidden_screen_does_not_poll(fake_slurm):
    async def scenario():
        app = LazySlurmApp(Config(refresh=0.05))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("p")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            before = fake_slurm.count("--format=%P|%T")
            await asyncio.sleep(0.3)
            await pilot.pause()
            # The screen is kept alive and keeps its timers; it must not poll
            # Slurm for a table nobody is looking at.
            assert fake_slurm.count("--format=%P|%T") == before

    asyncio.run(scenario())


def test_the_job_list_keeps_polling_while_the_monitor_is_open(fake_slurm):
    # App.query_one looks at the visible screen, so the poll used to die with
    # NoMatches the moment a full-screen monitor was pushed over it.
    async def scenario():
        app = LazySlurmApp(Config(refresh=0.05))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("p")
            await pilot.pause()
            await app._poll_jobs()
            assert app._job_screen.query_one("#active-jobs") is not None

    asyncio.run(scenario())
