"""Keyboard flow around the filter bar and panel focus (#38).

Filtering is only useful if the cursor can then reach the rows it found, so
these drive the real key handling through Textual's Pilot.
"""

from __future__ import annotations

import asyncio

from textual.widgets import Input

from lazyslurm import slurm
from lazyslurm.app import LazySlurmApp
from lazyslurm.models import Config, RunningJob
from lazyslurm.widgets.job_table import ActiveJobTable, CompletedJobTable


def _job(job_id: str, name: str) -> RunningJob:
    return RunningJob(
        job_id=job_id, name=name, elapsed="1:00", partition="gpu",
        state="RUNNING", time_limit="2:00:00", nodes="1", cpus="8",
        memory="4G", gres="gpu:1", work_dir="/w",
    )


_JOBS = [_job("1", "train"), _job("2", "eval"), _job("3", "train-big")]


def _app(monkeypatch):
    async def _running(*a, **k):
        return _JOBS

    async def _empty(*a, **k):
        return []

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(slurm, "get_running_jobs", _running)
    monkeypatch.setattr(slurm, "get_completed_jobs", _empty)
    monkeypatch.setattr(slurm, "get_partition_availability", _empty)
    monkeypatch.setattr(slurm, "get_job_detail", _none)
    monkeypatch.setattr(slurm, "get_job_stats", _none)
    return LazySlurmApp(config=Config(refresh=0, no_live=True, no_gpu=True))


def test_enter_keeps_the_filter_and_moves_the_cursor_to_the_matches(monkeypatch):
    async def scenario():
        app = _app(monkeypatch)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._poll_jobs()

            await pilot.press("slash")
            await pilot.pause()
            search = app.query_one("#search-input", Input)
            assert search.has_focus

            for ch in "train":
                await pilot.press(ch)
            await pilot.pause()
            active = app.query_one("#active-jobs", ActiveJobTable)
            assert active.row_count == 2          # "train" and "train-big"

            await pilot.press("enter")
            await pilot.pause()

            # Bar closed, filter still in force, cursor on the filtered rows.
            assert search.display is False
            assert search.value == "train"
            assert active.row_count == 2
            assert active.has_focus

    asyncio.run(scenario())


def test_escape_still_abandons_the_filter(monkeypatch):
    async def scenario():
        app = _app(monkeypatch)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._poll_jobs()

            await pilot.press("slash")
            for ch in "train":
                await pilot.press(ch)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

            search = app.query_one("#search-input", Input)
            active = app.query_one("#active-jobs", ActiveJobTable)
            assert search.display is False
            assert search.value == ""
            assert active.row_count == 3          # every job back
            assert active.has_focus

    asyncio.run(scenario())


def test_tab_cycles_back_to_the_job_table(monkeypatch):
    """Focus that reached the right-hand panels must be able to come back."""

    async def scenario():
        app = _app(monkeypatch)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._poll_jobs()
            app.query_one("#active-jobs", ActiveJobTable).focus()
            await pilot.pause()

            seen = []
            for _ in range(4):
                await pilot.press("tab")
                await pilot.pause()
                seen.append(app._right_focus)

            # jobs -> detail -> metadata -> jobs -> detail
            assert seen == ["detail", "metadata", "jobs", "detail"]

    asyncio.run(scenario())


def test_shift_tab_cycles_the_other_way(monkeypatch):
    async def scenario():
        app = _app(monkeypatch)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._poll_jobs()
            app.query_one("#active-jobs", ActiveJobTable).focus()
            await pilot.pause()

            await pilot.press("shift+tab")
            await pilot.pause()
            assert app._right_focus == "metadata"

    asyncio.run(scenario())


def test_enter_with_no_active_rows_lands_on_the_completed_table(monkeypatch):
    async def scenario():
        app = _app(monkeypatch)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._poll_jobs()

            await pilot.press("slash")
            for ch in "nomatch":
                await pilot.press(ch)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            # Nothing matched anywhere; focus still has to leave the input.
            assert app.query_one("#search-input", Input).display is False
            assert not app.query_one("#search-input", Input).has_focus

    asyncio.run(scenario())
