"""Keeping poll answers in RAM (#63).

sacct is ~69% of a refresh and returns a byte-identical answer every few
seconds, because a job that has ended does not change again. These cover the
part that is actually delicate: deciding what *can* still have changed, and
making sure the cheap path cannot show something the expensive one would not.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from lazyslurm import slurm
from lazyslurm.models import CompletedJob, Config
from lazyslurm.slurm import get_completed_jobs, get_partition_availability


def _row(job_id: str, state: str = "COMPLETED", end: str = "", name: str = "train") -> str:
    end = end or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    start = "2026-08-14T09:00:00"
    return f"{job_id}|{name}|{state}|0:0|{start}|{end}|00:10:00|gpu"


class _Sacct:
    """Stands in for _run_cmd, recording the --starttime of each query."""

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    async def __call__(self, *args: str):
        self.calls.append(list(args))
        if not self.responses:
            return "", "", 0
        out = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return out, "", 0

    @property
    def commands(self) -> list[str]:
        return [args[0] for args in self.calls]

    def starttime(self, index: int) -> datetime:
        for token in self.calls[index]:
            if token.startswith("--starttime="):
                return datetime.fromisoformat(token.split("=", 1)[1])
        raise AssertionError("no --starttime in " + " ".join(self.calls[index]))


def _completed(config: Config | None = None, **kwargs):
    return asyncio.run(get_completed_jobs(config or Config(days=7), **kwargs))


# --- the expensive query runs once -----------------------------------------


def test_the_first_call_reads_the_whole_window(monkeypatch):
    fake = _Sacct(_row("100"))
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    jobs = _completed()

    assert [j.job_id for j in jobs] == ["100"]
    expected = (datetime.now() - timedelta(days=7)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    assert fake.starttime(0) == expected


def test_the_second_call_only_reads_what_can_have_changed(monkeypatch):
    fake = _Sacct(_row("100"), _row("101"))
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    _completed()
    before = datetime.now()
    _completed()

    # A narrow trailing window, not the whole seven days again.
    assert fake.starttime(1) > before - timedelta(minutes=5)
    assert fake.starttime(1) < before


def test_a_narrow_window_still_leaves_the_earlier_jobs_on_screen(monkeypatch):
    # The point of the cache: the second query returns only job 101, but the
    # table must still show 100. This is the regression that would make the
    # optimisation user-visible.
    fake = _Sacct(_row("100"), _row("101"))
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    _completed()
    jobs = _completed()

    assert [j.job_id for j in jobs] == ["101", "100"]


def test_a_job_that_ends_between_polls_appears(monkeypatch):
    # It was RUNNING (and so not shown) at the first poll; the trailing window
    # catches it because --starttime selects jobs in any state during it, not
    # jobs that started in it.
    fake = _Sacct(_row("100", state="RUNNING"), _row("100", state="FAILED"))
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    assert _completed() == []
    jobs = _completed()
    assert [(j.job_id, j.state) for j in jobs] == [("100", "FAILED")]


def test_a_revised_row_replaces_the_cached_one(monkeypatch):
    fake = _Sacct(_row("100", state="COMPLETED"), _row("100", state="CANCELLED"))
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    _completed()
    jobs = _completed()

    assert [(j.job_id, j.state) for j in jobs] == [("100", "CANCELLED")]


def test_a_job_older_than_the_window_is_dropped(monkeypatch):
    old = (datetime.now() - timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%S")
    fake = _Sacct(_row("100", end=old), _row("101"))
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    assert [j.job_id for j in _completed()] == ["100"]
    # Second poll: 100 has aged out, exactly as it would have stopped being
    # returned by a full query.
    assert [j.job_id for j in _completed()] == ["101"]


def test_a_job_with_no_end_time_survives_the_pruning(monkeypatch):
    # sacct prints "Unknown" for a job it has no end time for. Reading that as
    # "ended at the epoch" would silently drop it.
    fake = _Sacct(_row("100", end="Unknown"), _row("101"))
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    _completed()
    assert [j.job_id for j in _completed()] == ["101", "100"]


# --- when the whole window is read again -----------------------------------


def test_force_rereads_the_whole_window(monkeypatch):
    fake = _Sacct(_row("100"))
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    _completed()
    _completed(full=True)

    assert fake.starttime(1) == fake.starttime(0)


def test_a_forced_read_forgets_a_job_sacct_no_longer_returns(monkeypatch):
    fake = _Sacct(_row("100"), "")
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    assert [j.job_id for j in _completed()] == ["100"]
    assert _completed(full=True) == []


def test_the_window_is_reread_periodically(monkeypatch):
    fake = _Sacct(_row("100"), _row("101"))
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    _completed()
    # Age the cache past the resync interval rather than sleeping for it.
    slurm._completed_cache.full_at -= slurm._SACCT_RESYNC + timedelta(seconds=1)
    _completed()

    assert fake.starttime(1) == fake.starttime(0)


@pytest.mark.parametrize(
    "changed",
    [
        {"days": 30},
        {"user": "someone-else"},
        {"partition": "cpu"},
    ],
    ids=["days", "user", "partition"],
)
def test_a_different_question_is_asked_in_full(monkeypatch, changed):
    fake = _Sacct(_row("100", name="a"))
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    _completed(Config(days=7))
    _completed(Config(**{"days": 7, **changed}))

    # Not a trailing window: the cached answers were about something else.
    assert fake.starttime(1) < datetime.now() - timedelta(hours=1)


def test_a_config_reload_drops_the_cache(monkeypatch):
    fake = _Sacct(_row("100"))
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    _completed(Config(days=7))
    slurm.set_config(Config(days=7))
    _completed(Config(days=7))

    assert fake.starttime(1) == fake.starttime(0)


# --- failure ---------------------------------------------------------------


def test_a_failed_sacct_keeps_the_table_populated(monkeypatch):
    calls = []

    async def flaky(*args):
        calls.append(args)
        if len(calls) == 1:
            return _row("100"), "", 0
        return "", "sacct: error", 1

    monkeypatch.setattr(slurm, "_run_cmd", flaky)

    assert [j.job_id for j in _completed()] == ["100"]
    # A transient failure must not blank the table -- the next poll merges on
    # top of what is still there.
    assert [j.job_id for j in _completed()] == ["100"]


def test_a_failure_with_nothing_cached_is_empty(monkeypatch):
    async def broken(*args):
        return "", "sacct: error", 1

    monkeypatch.setattr(slurm, "_run_cmd", broken)
    assert _completed() == []


# --- the cluster bar -------------------------------------------------------

_SINFO = "gpu|up|4/2/0/6|10/20/0/30|1-00:00:00|gpu:4\n"


def _availability(**kwargs):
    return asyncio.run(get_partition_availability(Config(), **kwargs))


def test_the_cluster_bar_is_not_refetched_every_tick(monkeypatch):
    fake = _Sacct(_SINFO)
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    first = _availability()
    assert _availability() == first
    assert fake.commands == ["sinfo"]


def test_the_cluster_bar_refreshes_once_the_ttl_passes(monkeypatch):
    fake = _Sacct(_SINFO)
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    _availability()
    key, fetched, result = slurm._partition_cache
    slurm._partition_cache = (key, fetched - slurm._SINFO_TTL, result)
    _availability()

    assert fake.commands == ["sinfo", "sinfo"]


def test_forcing_bypasses_the_ttl(monkeypatch):
    fake = _Sacct(_SINFO)
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    _availability()
    _availability(force=True)

    assert fake.commands == ["sinfo", "sinfo"]


def test_the_table_is_not_rebuilt_for_an_unchanged_poll(monkeypatch):
    # The rows are what cost the time, and building 4000 of them to have
    # _apply_diff discover nothing changed is pure waste.
    from textual.app import App, ComposeResult
    from lazyslurm.widgets.job_table import CompletedJobTable

    jobs = [
        CompletedJob(job_id="100", name="train", state="COMPLETED"),
        CompletedJob(job_id="101", name="eval", state="FAILED"),
    ]

    class _Harness(App):
        def compose(self) -> ComposeResult:
            yield CompletedJobTable(id="jobs")

    async def scenario():
        app = _Harness()
        async with app.run_test() as pilot:
            table = app.query_one("#jobs", CompletedJobTable)
            table.update_jobs(jobs)
            await pilot.pause()

            rebuilds = []
            original = table._rebuild
            monkeypatch.setattr(
                table, "_rebuild",
                lambda: (rebuilds.append(1), original())[1],
            )

            table.update_jobs(list(jobs))          # same data, new list object
            assert rebuilds == []

            table.update_jobs(jobs + [CompletedJob(job_id="102", name="x", state="TIMEOUT")])
            assert len(rebuilds) == 1
            assert table.row_count == 3

            # A display change still rebuilds, even though the jobs are equal.
            table.force_rebuild()
            assert len(rebuilds) == 2

    asyncio.run(scenario())


def test_an_empty_sinfo_is_not_cached(monkeypatch):
    # Usually means sinfo failed. Remembering it for the TTL would hide the
    # recovery behind a blank cluster bar.
    fake = _Sacct("", _SINFO)
    monkeypatch.setattr(slurm, "_run_cmd", fake)

    assert _availability() == []
    assert _availability() != []
