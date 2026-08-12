"""Tests for "why is this job pending" (issue #8).

Covers the sprio parser, the start-time formatter, the reason vocabulary, and
the Pending tab that shows them — including every path where Slurm gives us
nothing to work with.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static, TabbedContent

from lazyslurm import slurm
from lazyslurm.models import Config, JobDetail, PriorityInfo
from lazyslurm.widgets.metadata_view import MetadataView, priority_bar


def _run(coro):
    return asyncio.run(coro)


# Real shape of `sprio -p gpu --noheader --format=%i|%Y|%A|%F|%J|%P|%Q`
SPRIO_OUT = "\n".join([
    "2728921|45155|43448|1242|60|100|0",
    "2729034|26685|24977|1242|60|100|0",
    "2729035|26570|24862|1242|60|100|0",
    "2729038|9000|7598|1242|60|100|0",
])


# ---------------------------------------------------------------------------
# sprio parsing
# ---------------------------------------------------------------------------


def test_parse_sprio_extracts_factors_and_rank():
    info = slurm.parse_sprio(SPRIO_OUT, "2729035")
    assert info is not None
    assert (info.total, info.age, info.fairshare) == (26570, 24862, 1242)
    assert (info.job_size, info.partition, info.qos) == (60, 100, 0)
    assert info.queued == 4
    assert info.rank == 3      # two jobs have a higher priority
    assert info.ahead == 2


def test_parse_sprio_top_of_queue():
    info = slurm.parse_sprio(SPRIO_OUT, "2728921")
    assert (info.rank, info.ahead) == (1, 0)


def test_parse_sprio_skips_a_header_line():
    info = slurm.parse_sprio("JOBID|PRIORITY|AGE|FAIRSHARE|JOBSIZE|PARTITION|QOS\n"
                             + SPRIO_OUT, "2728921")
    assert info is not None and info.queued == 4


def test_parse_sprio_returns_none_when_the_job_is_absent():
    assert slurm.parse_sprio(SPRIO_OUT, "999999") is None


def test_parse_sprio_ignores_malformed_rows():
    out = SPRIO_OUT + "\ngarbage\n|||\n2729099|notanumber|x|y|z|w|v"
    info = slurm.parse_sprio(out, "2729038")
    assert info is not None
    assert info.queued == 5           # the unparseable-priority row still queues
    assert info.rank == 4             # ...but scores 0, so it does not outrank us


def test_priority_factors_are_ordered_and_drop_zeros():
    info = PriorityInfo("1", total=100, age=10, fairshare=80, job_size=0,
                        partition=10, qos=0)
    assert info.factors == [("Fair-share", 80), ("Age", 10), ("Partition", 10)]


def test_get_job_priority_asks_for_the_partition(monkeypatch):
    calls = {}

    async def _fake(*args):
        calls["args"] = args
        return SPRIO_OUT, "", 0

    monkeypatch.setattr(slurm, "_run_cmd", _fake)
    info = _run(slurm.get_job_priority("2729034", "gpu"))
    assert info is not None and info.rank == 2
    assert calls["args"][0] == "sprio"
    assert "-p" in calls["args"] and "gpu" in calls["args"]


def test_get_job_priority_degrades_when_sprio_is_missing(monkeypatch):
    async def _missing(*args):
        return "", "sprio: command not found", 127

    monkeypatch.setattr(slurm, "_run_cmd", _missing)
    monkeypatch.setattr(slurm, "_sprio_missing", False)
    assert _run(slurm.get_job_priority("1", "gpu")) is None
    assert slurm.sprio_available() is False


def test_get_job_priority_survives_an_empty_queue(monkeypatch):
    async def _empty(*args):
        return "", "", 0

    monkeypatch.setattr(slurm, "_run_cmd", _empty)
    assert _run(slurm.get_job_priority("1", "gpu")) is None


# ---------------------------------------------------------------------------
# Estimated start
# ---------------------------------------------------------------------------

NOW = datetime(2026, 8, 12, 12, 10, 0)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-08-12T21:20:09", "~21:20 (in 9h10m)"),
        ("2026-08-12T12:40:00", "~12:40 (in 30m)"),
        ("2026-08-13T09:05:00", "~Aug 13 09:05 (in 20h55m)"),
        ("2026-08-15T09:05:00", "~Aug 15 09:05 (in 2d20h)"),
        ("2026-08-12T12:10:30", "~12:10 (in <1m)"),
        ("2026-08-12T12:09:00", "~12:09 (due now)"),   # estimate already passed
    ],
)
def test_format_start_estimate(raw, expected):
    assert slurm.format_start_estimate(raw, NOW) == expected


@pytest.mark.parametrize("raw", ["Unknown", "N/A", "", "   ", "(null)", "None"])
def test_format_start_estimate_when_slurm_cannot_say(raw):
    assert "not estimated" in slurm.format_start_estimate(raw, NOW)


def test_format_start_estimate_passes_through_unparseable_values():
    assert slurm.format_start_estimate("sometime", NOW) == "sometime"


# ---------------------------------------------------------------------------
# Reason, in words
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,fragment",
    [
        ("Resources", "free nodes"),
        ("Priority", "ahead of it"),
        ("QOSMaxGRESPerUser", "QOS limit for GPUs"),
        ("QOSMaxCpuPerUserLimit", "QOS limit for CPUs"),
        ("QOSMaxWallDurationPerJobLimit", "longer than the QOS allows"),
        ("AssocGrpCpuLimit", "account group"),
        ("AssocMaxJobsLimit", "account's limit"),
        ("BeginTime", "requested start time"),
        ("JobHeldUser", "held by you"),
        ("ReqNodeNotAvail", "down, drained or reserved"),
        ("PartitionTimeLimit", "longer than the partition"),
        ("DependencyNeverSatisfied", "never be satisfied"),
        ("Reservation", "reservation"),
    ],
)
def test_explain_reason_speaks_plainly(code, fragment):
    assert fragment in slurm.explain_reason(code)


def test_explain_reason_counts_jobs_ahead():
    priority = PriorityInfo("1", total=10, rank=8, queued=40)
    assert slurm.explain_reason("Priority", None, priority) == "7 jobs ahead of it in the queue"


def test_explain_reason_uses_singular_for_one_job_ahead():
    priority = PriorityInfo("1", total=10, rank=2, queued=40)
    assert "1 job ahead" in slurm.explain_reason("Priority", None, priority)


def test_explain_reason_falls_back_when_nothing_is_ahead():
    priority = PriorityInfo("1", total=10, rank=1, queued=40)
    assert slurm.explain_reason("Priority", None, priority) == "other jobs are ahead of it in the queue"


def test_explain_reason_names_the_dependency():
    raw = {"Dependency": "afterok:4815162(unfulfilled)"}
    assert slurm.explain_reason("Dependency", raw) == "waiting on afterok:4815162(unfulfilled)"


def test_explain_reason_ignores_a_null_dependency():
    assert slurm.explain_reason("Dependency", {"Dependency": "(null)"}) == \
        "waiting for another job to finish"


def test_explain_reason_keeps_unknown_codes_verbatim():
    assert slurm.explain_reason("SomeFutureCode") == "Slurm says: SomeFutureCode"


@pytest.mark.parametrize("code", ["", "   ", "N/A"])
def test_explain_reason_with_nothing_to_explain(code):
    assert slurm.explain_reason(code) == "no reason reported"


def test_explain_reason_strips_slurm_parentheses():
    assert "free nodes" in slurm.explain_reason("Resources(something)")


# ---------------------------------------------------------------------------
# Priority bar
# ---------------------------------------------------------------------------


def test_priority_bar_scales_to_the_share():
    assert priority_bar(50, 100, width=10) == "█████░░░░░"
    assert priority_bar(100, 100, width=10) == "██████████"
    assert priority_bar(1, 1000, width=10) == "█░░░░░░░░░"   # never rounds to empty
    assert priority_bar(0, 100) == ""
    assert priority_bar(5, 0) == ""


# ---------------------------------------------------------------------------
# The Pending tab
# ---------------------------------------------------------------------------


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield Static("host")


def _detail(state="PENDING", **raw) -> JobDetail:
    base = {
        "JobId": "4815201", "JobState": state, "Partition": "gpu",
        "Reason": "Priority", "StartTime": "2026-08-12T21:20:09",
        "SubmitTime": "2026-08-12T03:11:44", "NumCPUs": "4",
    }
    base.update(raw)
    return JobDetail(job_id="4815201", raw=base)


async def _mount(app) -> MetadataView:
    view = MetadataView(id="metadata-view")
    await app.mount(view)
    return view


def _pending_text(view) -> str:
    return str(view.query_one("#meta-pending", Static).render())


def _tab_visible(view) -> bool:
    return view.query_one("#meta-tabs", TabbedContent).get_tab("tab-pending").display


def test_pending_tab_shows_reason_start_and_breakdown():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(100, 30)) as pilot:
            view = await _mount(app)
            await pilot.pause()
            view.load_detail(_detail(), slurm.parse_sprio(SPRIO_OUT, "2729035"))
            await pilot.pause()
            text = _pending_text(view)
            assert _tab_visible(view)
            assert "ahead of it" in text          # plain-language reason
            assert "reason code: Priority" in text  # the raw code is still there
            assert "~21:20" in text                 # estimated start
            assert "#3 of 4 pending in gpu" in text
            assert "Fair-share" in text and "Age" in text

    _run(scenario())


@pytest.mark.parametrize("state", ["RUNNING", "COMPLETED", "FAILED", "CANCELLED"])
def test_pending_tab_hidden_for_jobs_that_are_not_pending(state):
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(100, 30)) as pilot:
            view = await _mount(app)
            await pilot.pause()
            view.load_detail(_detail(state=state))
            await pilot.pause()
            assert not _tab_visible(view)

    _run(scenario())


def test_pending_tab_appears_and_disappears_as_selection_changes():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(100, 30)) as pilot:
            view = await _mount(app)
            await pilot.pause()
            view.load_detail(_detail())
            await pilot.pause()
            assert _tab_visible(view)
            view.load_detail(_detail(state="RUNNING"))
            await pilot.pause()
            assert not _tab_visible(view)
            # ...and the hidden tab is never left as the active one
            assert view.query_one("#meta-tabs", TabbedContent).active != "tab-pending"

    _run(scenario())


def test_pending_tab_without_sprio_says_why_it_is_missing():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(100, 30)) as pilot:
            view = await _mount(app)
            await pilot.pause()
            view.load_detail(_detail(), None, priority_available=False)
            await pilot.pause()
            text = _pending_text(view)
            assert "does not run sprio" in text
            assert "~21:20" in text        # the rest still works

    _run(scenario())


def test_pending_tab_without_a_start_estimate():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(100, 30)) as pilot:
            view = await _mount(app)
            await pilot.pause()
            view.load_detail(_detail(StartTime="Unknown", Reason="Dependency",
                                     Dependency="afterok:4815100"),
                             None)
            await pilot.pause()
            text = _pending_text(view)
            assert "not estimated" in text
            assert "afterok:4815100" in text

    _run(scenario())


def test_switch_tab_skips_the_hidden_pending_tab():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(100, 30)) as pilot:
            view = await _mount(app)
            await pilot.pause()
            view.load_detail(_detail(state="RUNNING"))
            await pilot.pause()
            tabs = view.query_one("#meta-tabs", TabbedContent)
            tabs.active = "tab-submission"
            view.switch_tab(1)
            await pilot.pause()
            assert tabs.active == "tab-raw"   # not the hidden Pending tab

    _run(scenario())


def test_switch_tab_includes_pending_when_shown():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(100, 30)) as pilot:
            view = await _mount(app)
            await pilot.pause()
            view.load_detail(_detail(), slurm.parse_sprio(SPRIO_OUT, "2729035"))
            await pilot.pause()
            tabs = view.query_one("#meta-tabs", TabbedContent)
            tabs.active = "tab-submission"
            view.switch_tab(1)
            await pilot.pause()
            assert tabs.active == "tab-pending"

    _run(scenario())


def test_no_job_selected_clears_and_hides():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(100, 30)) as pilot:
            view = await _mount(app)
            await pilot.pause()
            view.load_detail(_detail())
            await pilot.pause()
            view.load_detail(None)
            await pilot.pause()
            assert not _tab_visible(view)

    _run(scenario())


# ---------------------------------------------------------------------------
# Selection path: one extra call, and only for pending jobs
# ---------------------------------------------------------------------------


def test_only_pending_jobs_cost_an_sprio_call(monkeypatch):
    from lazyslurm.app import LazySlurmApp

    calls: list[tuple] = []

    async def _fake_run(*args):
        calls.append(args)
        if args[0] == "sprio":
            return SPRIO_OUT, "", 0
        return "", "", 0

    async def _detail_for(state):
        return _detail(state=state)

    async def scenario(state):
        calls.clear()
        monkeypatch.setattr(slurm, "_run_cmd", _fake_run)
        monkeypatch.setattr(slurm, "get_job_detail", lambda *a, **k: _detail_for(state))
        monkeypatch.setattr(slurm, "get_job_stats", lambda *a, **k: _none())
        monkeypatch.setattr(slurm, "read_log_file", lambda *a, **k: _text())
        app = LazySlurmApp(config=Config(refresh=0, no_live=True, no_gpu=True))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._selected_job_id = "4815201"
            await app._load_job_details("4815201")
            await pilot.pause()
        return [c for c in calls if c and c[0] == "sprio"]

    async def _none():
        return None

    async def _text():
        return ""

    assert len(_run(scenario("PENDING"))) == 1     # exactly one extra call
    assert _run(scenario("RUNNING")) == []         # none for a running job
