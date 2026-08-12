"""Tests for the pending-job property editor (EditJobScreen).

Driven headlessly through Textual's Pilot, so they cover the real layout and
key handling without a terminal or a Slurm cluster.
"""

from __future__ import annotations

import asyncio
import re

from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from lazyslurm.app import EditJobScreen


class _Harness(App):
    """Minimal host app: EditJobScreen is a modal, so it needs a base screen."""

    result: dict | None = None

    def compose(self) -> ComposeResult:
        yield Static("host")


_CURRENT = {
    "time_limit": "1:00:00",
    "partition": "cpu",
    "nodes": "1",
    "cpus": "8",
    "memory": "40G",
}


async def _open(pilot, app, job_ids=("123",), current=None):
    app.push_screen(
        EditJobScreen(list(job_ids), dict(current) if current else None),
        callback=lambda r: setattr(app, "result", r),
    )
    await pilot.pause()
    return app.screen


def test_fields_are_one_line_tall_and_show_their_value():
    """The focused field must stay flat and keep rendering its text.

    Textual's own ``Input:focus`` rule re-adds a tall border; if it wins, the
    focused line's content height collapses to 0 and the value disappears.
    """

    async def scenario():
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await _open(pilot, app, current=_CURRENT)
            for field in screen.query(Input):
                assert field.size.height == 1, (field.id, field.size)
            rendered = re.sub(r"<[^>]+>", "", app.export_screenshot()).replace("&#160;", " ")
            assert "1:00:00" in rendered  # focused field
            assert "cpu" in rendered      # unfocused field

    asyncio.run(scenario())


def test_arrow_and_tab_navigation_wraps():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            await _open(pilot, app, current=_CURRENT)
            assert app.focused.id == "edit-time_limit"
            await pilot.press("down")
            assert app.focused.id == "edit-partition"
            await pilot.press("tab")
            assert app.focused.id == "edit-nodes"
            await pilot.press("up")
            assert app.focused.id == "edit-partition"
            await pilot.press("shift+tab")
            assert app.focused.id == "edit-time_limit"
            await pilot.press("up")  # wraps to the last line
            assert app.focused.id == "edit-memory"
            await pilot.press("down")  # and back to the first
            assert app.focused.id == "edit-time_limit"

    asyncio.run(scenario())


def test_left_right_still_edit_within_a_line():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await _open(pilot, app, current=_CURRENT)
            await pilot.press("end", "left", "backspace")
            assert screen.query_one("#edit-time_limit", Input).value == "1:00:0"
            assert app.focused.id == "edit-time_limit"  # never left the line

    asyncio.run(scenario())


def test_only_changed_fields_are_returned():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await _open(pilot, app, current=_CURRENT)
            screen.query_one("#edit-partition", Input).value = "gpu"
            screen.query_one("#edit-cpus", Input).value = "8"  # unchanged
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.result == {"partition": "gpu"}

    asyncio.run(scenario())


def test_escape_returns_nothing():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await _open(pilot, app, current=_CURRENT)
            screen.query_one("#edit-partition", Input).value = "gpu"
            await pilot.press("escape")
            await pilot.pause()
            assert app.result == {}

    asyncio.run(scenario())


def test_multi_job_edit_starts_blank():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await _open(pilot, app, job_ids=("1", "2", "3"))
            for field in screen.query(Input):
                assert field.value == ""
            screen.query_one("#edit-time_limit", Input).value = "8:00:00"
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert app.result == {"time_limit": "8:00:00"}

    asyncio.run(scenario())
