"""Tests for the date:/age: search-bar filters (issue #78)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from lazyslurm.models import CompletedJob, RunningJob
from lazyslurm.widgets.job_table import (
    ActiveJobTable,
    CompletedJobTable,
    Term,
    parse_age,
    parse_day,
    parse_query,
    set_display_config,
    time_term_reaches,
)


def _run(coro):
    return asyncio.run(coro)


def _ago(**delta) -> str:
    return (datetime.now() - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%S")


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield Static("host")


RUNNING = [
    RunningJob("100", "fresh", "0:10", "gpu", "RUNNING", submit=_ago(minutes=20)),
    RunningJob("101", "queued", "0:00", "gpu", "PENDING", submit=_ago(hours=30)),
    RunningJob("102", "nodate", "0:00", "gpu", "PENDING", submit="Unknown"),
]

COMPLETED = [
    CompletedJob("90", "oct9", "COMPLETED", submit="2026-10-09T23:59:59"),
    CompletedJob("91", "oct10a", "FAILED", submit="2026-10-10T00:00:00"),
    CompletedJob("92", "oct10b", "COMPLETED", submit="2026-10-10T18:30:00"),
    CompletedJob("93", "oct11", "TIMEOUT", submit="2026-10-11T08:00:00"),
]


@pytest.fixture(autouse=True)
def _default_display():
    set_display_config(collapse_arrays=False, history_days=7)
    yield
    set_display_config()


def _filter(query: str, jobs: list | None = None):
    """Apply `query` to both tables. Returns (active ids, completed ids, titles)."""
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 24)) as pilot:
            active, completed = ActiveJobTable(id="a"), CompletedJobTable(id="c")
            await app.mount(active)
            await app.mount(completed)
            active.update_jobs(RUNNING)
            completed.update_jobs(jobs if jobs is not None else COMPLETED)
            await pilot.pause()
            active.apply_filter(query)
            completed.apply_filter(query)
            await pilot.pause()
            placeholder = (
                str(completed.get_cell("__no_match__", "Job ID"))
                if completed.row_count and not completed.get_row_order() else ""
            )
            return (active.get_row_order(), completed.get_row_order(),
                    str(active.border_title), str(completed.border_title),
                    placeholder)

    return _run(scenario())


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_date_and_age_keys_and_aliases():
    assert parse_query("date:2026-10-10 age:<10h") == [
        Term("date", "~", "2026-10-10"), Term("age", "<", "10h"),
    ]
    assert parse_query("day:today since:2D") == [
        Term("date", "~", "today"), Term("age", "~", "2d"),
    ]


@pytest.mark.parametrize("text, expected", [
    ("30m", timedelta(minutes=30)),
    ("10h", timedelta(hours=10)),
    ("2d", timedelta(days=2)),
    ("1w", timedelta(weeks=1)),
    ("10", None),        # no unit: hours or days? refuse rather than guess
    ("h", None),
    ("1.5h", None),
    ("", None),
])
def test_parse_age(text, expected):
    assert parse_age(text) == expected


def test_parse_day():
    today = date(2026, 10, 24)
    assert parse_day("2026-10-10", today) == date(2026, 10, 10)
    assert parse_day("today", today) == today
    assert parse_day("yesterday", today) == date(2026, 10, 23)
    assert parse_day("10-10-2026", today) is None
    assert parse_day("2026-13-01", today) is None


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def test_date_matches_the_whole_day_and_nothing_else():
    _, completed, *_ = _filter("date:2026-10-10")
    assert sorted(completed) == ["91", "92"]


@pytest.mark.parametrize("query, expected", [
    ("date:>2026-10-10", ["93"]),
    ("date:>=2026-10-10", ["91", "92", "93"]),
    ("date:<2026-10-10", ["90"]),
    ("date:!=2026-10-10", ["90", "93"]),
])
def test_date_comparisons(query, expected):
    _, completed, *_ = _filter(query)
    assert sorted(completed) == expected


def test_age_means_within_the_last():
    active, _, *_ = _filter("age:1h")
    assert active == ["100"]
    active, _, *_ = _filter("age:<2d")
    assert sorted(active) == ["100", "101"]


def test_age_older_than():
    active, _, *_ = _filter("age:>1h")
    assert active == ["101"]


def test_job_without_a_submit_time_never_matches_a_time_term():
    active, _, *_ = _filter("age:>0m")
    assert "102" not in active


def test_junk_values_match_nothing():
    active, completed, *_ = _filter("age:soon")
    assert active == [] and completed == []
    active, completed, *_ = _filter("date:10-10-2026")
    assert active == [] and completed == []


def test_time_terms_and_with_the_rest():
    _, completed, *_ = _filter("date:2026-10-10 state:fail")
    assert completed == ["91"]


# ---------------------------------------------------------------------------
# The history window
# ---------------------------------------------------------------------------


def test_reaches():
    now = datetime(2026, 10, 24, 12, 0)
    assert time_term_reaches(Term("age", "<", "10h"), now) == datetime(2026, 10, 24, 2, 0)
    assert time_term_reaches(Term("age", ">", "1h"), now) is None
    assert time_term_reaches(Term("date", "~", "2026-10-10"), now) == datetime(2026, 10, 10)
    assert time_term_reaches(Term("date", ">", "2026-10-10"), now) == datetime(2026, 10, 11)
    assert time_term_reaches(Term("date", "<", "2026-10-10"), now) is None


def test_asking_before_the_loaded_window_says_so():
    old = (date.today() - timedelta(days=30)).isoformat()
    _, completed, _, title, placeholder = _filter(f"date:{old}")
    assert completed == []
    assert "only the last 7 days are loaded" in placeholder
    assert title.endswith("(last 7d loaded)")


def test_a_recent_filter_does_not_warn():
    recent = [CompletedJob("95", "now", "COMPLETED", submit=_ago(hours=1))]
    _, completed, _, title, _ = _filter("age:<10h", recent)
    assert completed == ["95"]
    assert title == "Terminated Jobs — 1/1 match"


def test_the_active_table_never_warns():
    # squeue has no history window: every queued job is there, however old.
    _, _, active_title, _, _ = _filter("age:>30d")
    assert "loaded" not in active_title
