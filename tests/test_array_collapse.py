"""Tests for collapsing job arrays into a single expandable row (issue #7)."""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from lazyslurm import slurm
from lazyslurm.app import LazySlurmApp
from lazyslurm.models import CompletedJob, Config, RunningJob
from lazyslurm.widgets.job_table import (
    ActiveJobTable,
    CompletedJobTable,
    array_task_count,
    elapsed_seconds,
    group_jobs,
    set_display_config,
)


def _run(coro):
    return asyncio.run(coro)


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield Static("host")


RUNNING = [
    RunningJob("4815300", "solo-job", "0:42", "cpu", "RUNNING"),
    RunningJob("4815201_0", "sweep-lr", "1:12:04", "gpu", "RUNNING"),
    RunningJob("4815201_1", "sweep-lr", "1:11:58", "gpu", "RUNNING"),
    RunningJob("4815201_2", "sweep-lr", "0:00", "gpu", "PENDING"),
    RunningJob("4815201_[3-11]", "sweep-lr", "0:00", "gpu", "PENDING"),
    RunningJob("4815100", "other", "3:00", "cpu", "RUNNING"),
]


@pytest.fixture(autouse=True)
def _default_display():
    set_display_config(max_name=16, max_partition=16, abbreviate=False,
                       collapse_arrays=True)
    yield
    set_display_config()


async def _table(app, cls, jobs):
    table = cls(id="jobs")
    await app.mount(table)
    table.update_jobs(jobs)
    return table


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "job_id,expected",
    [
        ("123", 1),
        ("123_4", 1),
        ("123_[12-40]", 29),
        ("123_[1,3,5]", 3),
        ("123_[1-4%2]", 4),       # % only throttles concurrency
        ("123_[7]", 1),
        ("123_[]", 1),
    ],
)
def test_array_task_count(job_id, expected):
    assert array_task_count(job_id) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("0:42", 42),
        ("3:00", 180),
        ("1:12:04", 4324),
        ("2-06:00:00", 194400),
        ("N/A", 0),
        ("", 0),
        ("garbage", 0),
    ],
)
def test_elapsed_seconds(text, expected):
    assert elapsed_seconds(text) == expected


def test_group_jobs_keeps_order_and_groups_by_base():
    groups = group_jobs(RUNNING)
    assert [base for base, _ in groups] == ["4815300", "4815201", "4815100"]
    assert len(dict(groups)["4815201"]) == 4
    assert len(dict(groups)["4815300"]) == 1  # non-array: its own group


# ---------------------------------------------------------------------------
# Table behaviour
# ---------------------------------------------------------------------------


def test_array_occupies_one_row_until_expanded():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 24)) as pilot:
            table = await _table(app, ActiveJobTable, RUNNING)
            await pilot.pause()
            assert table.row_count == 3  # solo + array + other

            label = str(table.get_cell("4815201", "Job ID"))
            assert "4815201_[0-11]" in label   # id range across all four rows
            assert "×12" in label              # 2 running + 1 + 9 from the range
            assert label.lstrip().startswith("▸")

            assert table.toggle_expand("4815201")
            await pilot.pause()
            assert table.row_count == 7        # 3 + its 4 rows
            assert str(table.get_cell("4815201", "Job ID")).lstrip().startswith("▾")

            table.toggle_expand("4815201")
            await pilot.pause()
            assert table.row_count == 3

    _run(scenario())


def test_tally_counts_tasks_not_rows():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 24)) as pilot:
            table = await _table(app, ActiveJobTable, RUNNING)
            await pilot.pause()
            tally = str(table.get_cell("4815201", "Elapsed"))
            # 2 running; pending = 1 task + the 9 behind "[3-11]"
            assert "2" in tally and "10" in tally
            assert "run" in tally.lower() and "pend" in tally.lower()

    _run(scenario())


def test_expansion_survives_a_poll_and_a_filter():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 24)) as pilot:
            table = await _table(app, ActiveJobTable, RUNNING)
            await pilot.pause()
            table.toggle_expand("4815201")
            await pilot.pause()
            assert table.row_count == 7

            table.update_jobs(RUNNING)          # a poll
            await pilot.pause()
            assert table.row_count == 7

            table.apply_filter("sweep")         # only the array matches
            await pilot.pause()
            assert table.row_count == 5         # group row + 4 members
            table.apply_filter("")
            await pilot.pause()
            assert table.row_count == 7

    _run(scenario())


def test_non_array_jobs_are_untouched():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 24)) as pilot:
            plain = [RunningJob("1", "a", "0:10", "gpu", "RUNNING"),
                     RunningJob("2", "b", "0:20", "cpu", "PENDING")]
            table = await _table(app, ActiveJobTable, plain)
            await pilot.pause()
            assert table.row_count == 2
            assert table.selected_group() is None
            assert str(table.get_cell("1", "Job ID")) == "1"

    _run(scenario())


def test_collapsing_can_be_switched_off():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 24)) as pilot:
            set_display_config(collapse_arrays=False)
            table = await _table(app, ActiveJobTable, RUNNING)
            await pilot.pause()
            assert table.row_count == len(RUNNING)
            assert table.selected_group() is None

    _run(scenario())


def test_selection_resolves_to_a_real_task_id():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 24)) as pilot:
            table = await _table(app, ActiveJobTable, RUNNING)
            await pilot.pause()
            table.move_cursor(row=1)            # the collapsed array
            await pilot.pause()
            base, members = table.selected_group()
            assert base == "4815201" and len(members) == 4
            # The base id is not a job Slurm can describe; the first task is.
            assert table.get_selected_job_id() == "4815201_0"

    _run(scenario())


def test_expand_ids_maps_group_keys_to_members():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 24)) as pilot:
            table = await _table(app, ActiveJobTable, RUNNING)
            await pilot.pause()
            out = table.expand_ids({"4815201", "4815300"})
            assert set(out) == {
                "4815201_0", "4815201_1", "4815201_2", "4815201_[3-11]", "4815300",
            }

    _run(scenario())


def test_bookmarking_an_array_pins_the_group():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 24)) as pilot:
            table = await _table(app, ActiveJobTable, RUNNING)
            await pilot.pause()
            table.set_bookmarks({"4815201"})
            await pilot.pause()
            assert table.get_row_order()[0] == "4815201"

    _run(scenario())


def test_completed_table_groups_and_shows_longest_run():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 24)) as pilot:
            done = [
                CompletedJob("900_0", "arr", "COMPLETED", elapsed="0:30", partition="gpu"),
                CompletedJob("900_1", "arr", "FAILED", elapsed="2:00:00", partition="gpu"),
                CompletedJob("900_2", "arr", "COMPLETED", elapsed="1:00", partition="gpu"),
            ]
            table = await _table(app, CompletedJobTable, done)
            await pilot.pause()
            assert table.row_count == 1
            assert str(table.get_cell("900", "Elapsed")) == "2:00:00"  # longest
            tally = str(table.get_cell("900", "State"))
            assert "2" in tally and "1" in tally

    _run(scenario())


# ---------------------------------------------------------------------------
# Actions on a collapsed row
# ---------------------------------------------------------------------------


def _app_with_jobs(monkeypatch, jobs=RUNNING):
    async def _running(*a, **k):
        return jobs

    async def _empty(*a, **k):
        return []

    monkeypatch.setattr(slurm, "get_running_jobs", _running)
    monkeypatch.setattr(slurm, "get_completed_jobs", _empty)
    monkeypatch.setattr(slurm, "get_partition_availability", _empty)

    async def _no_detail(*a, **k):
        return None  # detail panels stay empty; not what these tests are about

    monkeypatch.setattr(slurm, "get_job_detail", _no_detail)
    return LazySlurmApp(config=Config(refresh=0, no_live=True, no_gpu=True))


def test_cancel_on_a_collapsed_array_cancels_the_base_id(monkeypatch):
    cancelled = []

    async def _cancel(job_id, force=False):
        cancelled.append((job_id, force))
        return True, "ok"

    monkeypatch.setattr(slurm, "cancel_job", _cancel)

    async def scenario():
        app = _app_with_jobs(monkeypatch)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._poll_jobs()
            table = app.query_one("#active-jobs", ActiveJobTable)
            table.move_cursor(row=1)
            await pilot.pause()
            app.action_cancel_job()
            await pilot.pause()
            await app._on_cancel_confirmed(True)
            await pilot.pause()

    _run(scenario())
    # One scancel for the whole array, on the base id — not four.
    assert cancelled == [("4815201", False)]


def test_edit_on_a_collapsed_array_targets_its_pending_tasks(monkeypatch):
    async def scenario():
        app = _app_with_jobs(monkeypatch)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app._poll_jobs()
            table = app.query_one("#active-jobs", ActiveJobTable)
            table.move_cursor(row=1)
            app._selected_source = "active"
            await pilot.pause()
            app.action_edit_job()
            await pilot.pause()
            return list(app._edit_job_ids)

    ids = _run(scenario())
    # The two RUNNING tasks are skipped; the pending ones are editable.
    assert ids == ["4815201_2", "4815201_[3-11]"]


# ---------------------------------------------------------------------------
# Array index spec parsing (review of #14: "%" throttle leaked into the label)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "job_id,span",
    [
        ("123_[1-4%10]", (1, 4)),   # %10 throttles concurrency; it is not an index
        ("123_[12-40]", (12, 40)),
        ("123_[1,3,5]", (1, 5)),
        ("123_[5]", (5, 5)),
        ("123_7", (7, 7)),
        ("123", None),              # not an array
        ("123_[]", None),
        ("123_abc", None),          # unparseable decoration is ignored, not scraped
    ],
)
def test_array_index_span(job_id, span):
    from lazyslurm.models import array_index_span
    assert array_index_span([job_id]) == span


def test_array_index_span_across_members():
    from lazyslurm.models import array_index_span
    assert array_index_span(["200_0", "200_1", "200_[2-11%4]"]) == (0, 11)


def test_throttled_array_label_shows_the_real_index_range():
    """`[1-4%10]` must render as [1-4] ×4, never [1-10]."""
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 24)) as pilot:
            table = await _table(app, ActiveJobTable, [
                RunningJob("700_0", "throttled", "1:00", "gpu", "RUNNING"),
                RunningJob("700_[1-4%10]", "throttled", "0:00", "gpu", "PENDING"),
            ])
            await pilot.pause()
            label = str(table.get_cell("700", "Job ID"))
            assert "700_[0-4]" in label
            assert "×5" in label          # task 0 plus tasks 1-4
            assert "10" not in label      # the throttle never reaches the label

    _run(scenario())
