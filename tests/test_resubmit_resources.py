"""End-to-end test for Shift+S — resubmit a terminated job with new resources.

Driven through Textual's Pilot with the Slurm layer monkeypatched, so it covers
the whole path the user takes: prefill from the terminated job, the suggestion
after a TIMEOUT, and what reaches ``sbatch``.
"""

from __future__ import annotations

import asyncio

from textual.widgets import Input

from lazyslurm import slurm
from lazyslurm.app import EditJobScreen, LazySlurmApp
from lazyslurm.models import CompletedJob, Config, JobDetail


def _run(coro):
    asyncio.run(coro)


_TIMED_OUT = CompletedJob(
    job_id="4815",
    name="train",
    state="TIMEOUT",
    exit_code="0:0",
    start="2026-08-13T09:00:00",
    end="2026-08-13T11:00:00",
    elapsed="2:00:00",
    partition="gpu",
)

_DETAIL = JobDetail(
    job_id="4815",
    raw={
        "SubmitLine": "sbatch --time=2:00:00 --mem=4G train.sh",
        "TimeLimit": "2:00:00",
        "Partition": "gpu",
        "NumNodes": "1",
        "NumCPUs": "8",
        "MinMemoryNode": "4G",
        "JobState": "TIMEOUT",
    },
    work_dir="/work",
    source="scontrol",
)


def _app(monkeypatch, submitted):
    async def _empty(*a, **k):
        return []

    async def _completed(*a, **k):
        return [_TIMED_OUT]

    async def _detail(*a, **k):
        return _DETAIL

    async def _resubmit(command, work_dir, job_id=None, overrides=None):
        submitted.append((command, work_dir, job_id, overrides))
        return True, "Submitted batch job 9999"

    monkeypatch.setattr(slurm, "get_running_jobs", _empty)
    monkeypatch.setattr(slurm, "get_completed_jobs", _completed)
    monkeypatch.setattr(slurm, "get_partition_availability", _empty)
    monkeypatch.setattr(slurm, "get_job_detail", _detail)
    async def _no_stats(*a, **k):
        return None

    monkeypatch.setattr(slurm, "get_job_stats", _no_stats)
    monkeypatch.setattr(slurm, "resubmit_job", _resubmit)
    return LazySlurmApp(config=Config(refresh=0, no_live=True, no_gpu=True))


def test_shift_s_prefills_from_the_job_and_suggests_double_after_timeout(monkeypatch):
    submitted: list = []

    async def scenario():
        app = _app(monkeypatch, submitted)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._poll_jobs()
            app._selected_job_id = "4815"
            app._selected_source = "completed"

            await app.action_resubmit_job_edit()
            await pilot.pause()

            screen = app.screen
            assert isinstance(screen, EditJobScreen)
            # The job's own allocation, with the runtime doubled after TIMEOUT.
            assert screen.query_one("#edit-time_limit", Input).value == "04:00:00"
            assert screen.query_one("#edit-memory", Input).value == "4G"
            assert screen.query_one("#edit-partition", Input).value == "gpu"
            assert screen.query_one("#edit-cpus", Input).value == "8"

            await pilot.press("ctrl+s")
            await pilot.pause()

    _run(scenario())

    assert len(submitted) == 1
    command, work_dir, job_id, overrides = submitted[0]
    assert command == "sbatch --time=2:00:00 --mem=4G train.sh"
    assert work_dir == "/work"
    assert job_id == "4815"
    # Only the suggested field changed; the untouched ones stay out of it.
    assert overrides == {"time_limit": "04:00:00"}


def test_escape_on_the_resubmit_editor_submits_nothing(monkeypatch):
    submitted: list = []

    async def scenario():
        app = _app(monkeypatch, submitted)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._poll_jobs()
            app._selected_job_id = "4815"
            app._selected_source = "completed"

            await app.action_resubmit_job_edit()
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()

    _run(scenario())
    assert submitted == []


def test_shift_s_refuses_a_job_that_is_still_running(monkeypatch):
    submitted: list = []

    async def scenario():
        app = _app(monkeypatch, submitted)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._selected_job_id = "4815"
            app._selected_source = "active"     # not terminated
            await app.action_resubmit_job_edit()
            await pilot.pause()
            assert not isinstance(app.screen, EditJobScreen)

    _run(scenario())
    assert submitted == []
