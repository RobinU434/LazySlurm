"""The help screen must match the real bindings, and follow the panel.

The point of these tests is drift: the help was hand-written prose before, and
it silently fell behind the bindings twice. Here every panel's declared
BINDINGS are cross-checked against `lazyslurm.help` in both directions, so
adding a binding without documenting it (or documenting one that no longer
exists) fails the suite.
"""

from __future__ import annotations

import asyncio

import pytest

from lazyslurm import help as help_topics
from lazyslurm.app import (
    HelpScreen,
    LazySlurmApp,
    NodeScreen,
    PartitionScreen,
    UsageScreen,
)
from lazyslurm.models import Config


def _run(coro):
    return asyncio.run(coro)


def documented_keys(context: str) -> set[str]:
    """Every Textual key name the help documents for a context, plus globals."""
    keys = {k for entry in help_topics.panel_for(context).keys for k in entry.keys}
    return keys | {k for entry in help_topics.GLOBAL for k in entry.keys}


def bound_keys(bindings) -> set[str]:
    return {b.key for b in bindings}


# Screens whose bindings belong to one help context.
SCREEN_CONTEXTS = [
    (PartitionScreen, help_topics.PARTITIONS),
    (NodeScreen, help_topics.NODES),
    (UsageScreen, help_topics.USAGE),
]


# ---------------------------------------------------------------------------
# Nothing undocumented
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("screen,context", SCREEN_CONTEXTS,
                         ids=[c for _, c in SCREEN_CONTEXTS])
def test_every_screen_binding_is_documented(screen, context):
    missing = bound_keys(screen.BINDINGS) - documented_keys(context)
    assert not missing, (
        f"{screen.__name__} binds {sorted(missing)} but the help for '{context}' "
        f"does not mention it — add it to lazyslurm/help.py"
    )


def test_every_app_binding_is_documented_somewhere():
    """A main-screen key must appear under some panel, or under Anywhere."""
    everywhere = {k for entry in help_topics.GLOBAL for k in entry.keys}
    for panel in help_topics.PANELS:
        everywhere |= {k for entry in panel.keys for k in entry.keys}

    missing = bound_keys(LazySlurmApp.BINDINGS) - everywhere
    assert not missing, (
        f"LazySlurmApp binds {sorted(missing)} with no help entry — "
        f"add it to lazyslurm/help.py"
    )


# ---------------------------------------------------------------------------
# Nothing documented that does not exist
# ---------------------------------------------------------------------------


def test_documented_keys_all_exist():
    """Every key the help claims must be bound somewhere it applies."""
    real = bound_keys(LazySlurmApp.BINDINGS)
    for screen, _ in SCREEN_CONTEXTS:
        real |= bound_keys(screen.BINDINGS)

    claimed = {k for entry in help_topics.GLOBAL for k in entry.keys}
    for panel in help_topics.PANELS:
        claimed |= {k for entry in panel.keys for k in entry.keys}

    stale = claimed - real
    assert not stale, f"help documents {sorted(stale)}, which nothing binds any more"


def test_entries_without_bindings_are_deliberate():
    """`keys=()` means "handled by a widget" — each such entry is listed in IMPLICIT."""
    implicit = [entry for panel in help_topics.PANELS for entry in panel.keys
                if not entry.keys]
    assert implicit, "expected some implicitly-handled keys (Up/Down, Enter)"
    assert help_topics.IMPLICIT, "IMPLICIT should explain why they have no Binding"
    for entry in implicit:
        assert entry.display and entry.text


# ---------------------------------------------------------------------------
# The rendered text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("panel", help_topics.PANELS, ids=lambda p: p.context)
def test_render_leads_with_the_panel_and_lists_the_others(panel):
    text = help_topics.render(panel.context)
    assert panel.title in text.splitlines()[0]
    assert "Anywhere" in text
    assert "Other panels" in text
    for other in help_topics.PANELS:
        if other.context != panel.context:
            assert other.title in text          # discoverable from anywhere


def test_render_escapes_bracket_keys():
    """`[ / ]` is a key, not Rich markup."""
    text = help_topics.render(help_topics.DETAIL)
    assert "\\[ / ]" in text or "\\[ / \\]" in text


def test_unknown_context_falls_back_to_the_job_tables():
    assert help_topics.panel_for("nonsense").context == help_topics.JOBS
    assert "Job tables" in help_topics.render("nonsense")


def test_filter_syntax_is_documented_where_it_can_be_read():
    """The filter bar has no ? of its own, so its syntax lives with the tables."""
    text = help_topics.render(help_topics.JOBS)
    assert "state:pend" in text and "gpu:>=2" in text


@pytest.mark.parametrize(
    "context,fragment",
    [
        (help_topics.JOBS, "expand or collapse a job array"),
        (help_topics.DETAIL, "Efficiency"),
        (help_topics.METADATA, "Pending tab appears only"),
        (help_topics.PARTITIONS, "every user's jobs"),
        (help_topics.USAGE, "fair-share factor"),
    ],
)
def test_panels_explain_their_feature(context, fragment):
    assert fragment in help_topics.render(context)


# ---------------------------------------------------------------------------
# The screen follows the panel
# ---------------------------------------------------------------------------


async def _help_context(app, pilot) -> str:
    await pilot.press("question_mark")
    await pilot.pause()
    assert isinstance(app.screen, HelpScreen), "? did not open the help"
    context = app.screen.context
    await pilot.press("escape")
    await pilot.pause()
    return context


def _app() -> LazySlurmApp:
    return LazySlurmApp(config=Config(refresh=0, no_live=True, no_gpu=True))


def test_help_follows_focus_in_the_main_view(monkeypatch):
    async def _quiet(*args):
        return "", "", 0

    async def scenario():
        monkeypatch.setattr("lazyslurm.slurm._run_cmd", _quiet)
        app = _app()
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            assert await _help_context(app, pilot) == help_topics.JOBS
            # Tab walks: completed table, then the right-hand panels.
            await pilot.press("tab")
            await pilot.pause()
            assert await _help_context(app, pilot) == help_topics.JOBS
            await pilot.press("tab")
            await pilot.pause()
            assert await _help_context(app, pilot) == help_topics.DETAIL

    _run(scenario())


def test_help_on_the_metadata_panel(monkeypatch):
    """Four Tabs from the job table reach the metadata panel."""
    async def _quiet(*args):
        return "", "", 0

    async def scenario():
        monkeypatch.setattr("lazyslurm.slurm._run_cmd", _quiet)
        app = _app()
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            for _ in range(4):
                await pilot.press("tab")
                await pilot.pause()
            assert app._help_context() == help_topics.METADATA
            assert await _help_context(app, pilot) == help_topics.METADATA

    _run(scenario())


def test_help_on_the_full_screen_panels(monkeypatch):
    async def _quiet(*args):
        return "", "", 0

    async def scenario():
        monkeypatch.setattr("lazyslurm.slurm._run_cmd", _quiet)
        app = _app()
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            app.push_screen(PartitionScreen(app.config))
            await pilot.pause()
            assert await _help_context(app, pilot) == help_topics.PARTITIONS
            app.push_screen(UsageScreen(app.config))
            await pilot.pause()
            assert await _help_context(app, pilot) == help_topics.USAGE

    _run(scenario())


def test_help_returns_to_the_panel_it_was_opened_from(monkeypatch):
    async def _quiet(*args):
        return "", "", 0

    async def scenario():
        monkeypatch.setattr("lazyslurm.slurm._run_cmd", _quiet)
        app = _app()
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause()
            app.push_screen(PartitionScreen(app.config))
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, PartitionScreen)   # not dumped to the job view

    _run(scenario())
