"""Tests for the structured search-bar filters (issue #12)."""

from __future__ import annotations

import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from lazyslurm.models import CompletedJob, RunningJob
from lazyslurm.widgets.job_table import (
    ActiveJobTable,
    CompletedJobTable,
    Term,
    parse_query,
    set_display_config,
)


def _run(coro):
    return asyncio.run(coro)


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield Static("host")


RUNNING = [
    RunningJob("100", "train-a", "1:00", "gpu", "RUNNING", gres="gres/gpu:2"),
    RunningJob("101", "train-b", "0:00", "gpu", "PENDING", gres="gres/gpu:1"),
    RunningJob("102", "prep", "2:00", "cpu", "RUNNING", gres="None"),
    RunningJob("103", "train-c", "0:00", "cpu", "PENDING", gres="None"),
]

COMPLETED = [
    CompletedJob("90", "train-old", "COMPLETED", partition="gpu", elapsed="1:00"),
    CompletedJob("91", "sweep", "FAILED", partition="cpu", elapsed="0:10"),
    CompletedJob("92", "sweep", "OUT_OF_MEMORY", partition="gpu", elapsed="0:20"),
]


@pytest.fixture(autouse=True)
def _default_display():
    set_display_config(collapse_arrays=False)  # one row per job, easier to assert
    yield
    set_display_config()


async def _tables(app):
    active, completed = ActiveJobTable(id="a"), CompletedJobTable(id="c")
    await app.mount(active)
    await app.mount(completed)
    active.update_jobs(RUNNING)
    completed.update_jobs(COMPLETED)
    return active, completed


def _filter(query: str):
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 24)) as pilot:
            active, completed = await _tables(app)
            await pilot.pause()
            active.apply_filter(query)
            completed.apply_filter(query)
            await pilot.pause()
            return (active.get_row_order(), completed.get_row_order(),
                    str(active.border_title))

    return _run(scenario())


# ---------------------------------------------------------------------------
# parse_query
# ---------------------------------------------------------------------------


def test_parse_query_splits_fields_and_bare_words():
    assert parse_query("train state:run gpu:>0") == [
        Term(None, "~", "train"),
        Term("state", "~", "run"),
        Term("gpu", ">", "0"),
    ]


@pytest.mark.parametrize(
    "text,field",
    [("state:x", "state"), ("st:x", "state"), ("s:x", "state"),
     ("part:x", "partition"), ("partition:x", "partition"), ("p:x", "partition"),
     ("name:x", "name"), ("n:x", "name"),
     ("id:x", "id"), ("job:x", "id"),
     ("gpu:1", "gpu"), ("gpus:1", "gpu"), ("gres:1", "gpu")],
)
def test_parse_query_aliases(text, field):
    assert parse_query(text)[0].field == field


@pytest.mark.parametrize("op", [">=", "<=", "!=", ">", "<", "="])
def test_parse_query_comparisons(op):
    assert parse_query(f"gpu:{op}2")[0] == Term("gpu", op, "2")


def test_parse_query_keeps_unknown_keys_as_text():
    """Nothing a user types may break the filter."""
    assert parse_query("foo:bar") == [Term(None, "~", "foo:bar")]
    assert parse_query("12:30") == [Term(None, "~", "12:30")]


def test_parse_query_survives_a_half_typed_quote():
    """Mid-typing input must not raise; shlex would, so we fall back to split()."""
    terms = parse_query('name:"my job')
    assert terms == [Term("name", "~", '"my'), Term(None, "~", "job")]
    # Closed quote parses as one value.
    assert parse_query('name:"my job"') == [Term("name", "~", "my job")]


def test_parse_query_empty():
    assert parse_query("") == []
    assert parse_query("   ") == []


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_state_and_partition_terms_and_together():
    active, completed, _ = _filter("state:pend part:gpu")
    assert active == ["101"]
    assert completed == []


def test_bare_words_still_search_the_row():
    active, _, _ = _filter("train")
    assert active == ["100", "101", "103"]


def test_mixing_bare_and_field_terms():
    active, _, _ = _filter("train state:run")
    assert active == ["100"]


def test_state_is_a_prefix_match():
    _, completed, _ = _filter("state:fail")
    assert completed == ["91"]
    _, completed, _ = _filter("state:out")
    assert completed == ["92"]


def test_state_matching_is_case_insensitive():
    _, completed, _ = _filter("state:FaIl")
    assert completed == ["91"]


@pytest.mark.parametrize(
    "query,expected",
    [("gpu:>0", ["100", "101"]),
     ("gpu:>=2", ["100"]),
     ("gpu:2", ["100"]),
     ("gpu:0", ["102", "103"]),
     ("gpu:<1", ["102", "103"])],
)
def test_gpu_comparisons(query, expected):
    active, _, _ = _filter(query)
    assert active == expected


def test_gpu_term_matches_nothing_in_the_terminated_table():
    """sacct rows carry no GRES, so there is nothing to compare against."""
    _, completed, _ = _filter("gpu:>0")
    assert completed == []


def test_gpu_term_with_junk_value_matches_nothing():
    active, _, _ = _filter("gpu:>abc")
    assert active == []


def test_id_and_name_terms():
    active, _, _ = _filter("id:10")
    assert active == ["100", "101", "102", "103"]
    active, _, _ = _filter("id:101")
    assert active == ["101"]
    active, _, _ = _filter("name:prep")
    assert active == ["102"]


def test_unknown_key_falls_back_to_substring():
    active, _, _ = _filter("foo:bar")
    assert active == []          # matched as text, and nothing contains it


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


def test_no_match_shows_a_message_rather_than_an_empty_table():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 24)) as pilot:
            active, _ = await _tables(app)
            await pilot.pause()
            active.apply_filter("zzz")
            await pilot.pause()
            assert active.row_count == 1                 # the placeholder
            assert active.get_row_order() == []          # but it is not a job
            assert active.get_selected_job_id() is None  # and cannot be acted on
            assert active.selected_group() is None
            cell = str(active.get_cell("__no_match__", "Job ID"))
            assert "no jobs match" in cell

    _run(scenario())


def test_border_title_reports_the_match_count():
    _, _, title = _filter("state:pend")
    assert title == "Active Jobs — 2/4 match"
    _, _, title = _filter("")
    assert title == "Active Jobs"


def test_filtering_an_array_keeps_the_group_together():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 24)) as pilot:
            set_display_config(collapse_arrays=True)
            table = ActiveJobTable(id="a")
            await app.mount(table)
            table.update_jobs([
                RunningJob("200_0", "sweep", "1:00", "gpu", "RUNNING"),
                RunningJob("200_1", "sweep", "0:00", "gpu", "PENDING"),
                RunningJob("300", "other", "1:00", "cpu", "RUNNING"),
            ])
            await pilot.pause()
            table.apply_filter("state:run")
            await pilot.pause()
            # Only the running task survives, so the array is no longer a group.
            assert table.get_row_order() == ["200_0", "300"]
            table.apply_filter("name:sweep")
            await pilot.pause()
            assert table.get_row_order() == ["200"]      # collapsed group row

    _run(scenario())
