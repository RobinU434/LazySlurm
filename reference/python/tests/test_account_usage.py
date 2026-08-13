"""Tests for the account usage panel (issue #13).

The sreport/sshare fixtures keep the real output *shape* — the banner lines,
the parsable header, the empty FairShare on account rows — with invented
account and user names.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

import pytest
from textual.widgets import Static

from lazyslurm import slurm
from lazyslurm.app import LazySlurmApp, UsageScreen
from lazyslurm.models import Config, FairShare, UsageRow
from lazyslurm.widgets.usage_view import UsageTable, format_hours, share_bar


def _run(coro):
    return asyncio.run(coro)


def plain(text) -> str:
    return re.sub(r"\[/?[a-z0-9 #$_]*\]", "", str(text))


# sreport prints a dashed banner and a title before the parsable rows, and the
# column header is itself pipe-separated.
SREPORT_OUT = """--------------------------------------------------------------------------------
Cluster/Account/User Utilization 2026-08-01T00:00:00 - 2026-08-12T16:59:59 (1011600 secs)
Usage reported in CPU Hours
--------------------------------------------------------------------------------
Cluster|Account|Login|Proper Name|Used|Energy
galvani|physics||          |12500|0
galvani|physics|jdoe|Jane Doe|8200|0
galvani|physics|asmith|A Smith|3100|0
galvani|physics|bpatel|B Patel|1,200|0
"""

SSHARE_OUT = """Account|User|RawShares|NormShares|RawUsage|EffectvUsage|FairShare
physics||100|0.100000|9876543|0.210000|
physics|jdoe|5|0.016835|2738593|0.056350|0.436567
"""


# ---------------------------------------------------------------------------
# sreport parsing
# ---------------------------------------------------------------------------


def test_parse_sreport_skips_banner_and_header():
    rows = slurm.parse_sreport(SREPORT_OUT)
    assert [r.user for r in rows] == ["", "jdoe", "asmith", "bpatel"]
    assert rows[1].hours == 8200
    assert rows[1].name == "Jane Doe"
    assert rows[1].account == "physics"


def test_parse_sreport_marks_the_account_total_row():
    rows = slurm.parse_sreport(SREPORT_OUT)
    total = [r for r in rows if r.is_account_total]
    assert len(total) == 1 and total[0].hours == 12500


def test_parse_sreport_handles_thousands_separators():
    rows = {r.user: r.hours for r in slurm.parse_sreport(SREPORT_OUT)}
    assert rows["bpatel"] == 1200


@pytest.mark.parametrize(
    "text",
    ["", "   ", "no pipes here", "-----", "Cluster|Account|Login|Proper Name|Used|Energy"],
)
def test_parse_sreport_on_nothing_useful(text):
    assert slurm.parse_sreport(text) == []


def test_parse_sreport_ignores_rows_whose_hours_are_not_a_number():
    assert slurm.parse_sreport("galvani|physics|jdoe|Jane Doe|n/a|0") == []


# ---------------------------------------------------------------------------
# sshare parsing
# ---------------------------------------------------------------------------


def test_parse_sshare_reads_both_account_and_user_rows():
    shares = slurm.parse_sshare(SSHARE_OUT)
    assert len(shares) == 2
    account, user = shares
    assert account.user == "" and account.fairshare is None  # blank on account rows
    assert user.user == "jdoe"
    assert user.norm_shares == pytest.approx(0.016835)
    assert user.effective_usage == pytest.approx(0.056350)
    assert user.fairshare == pytest.approx(0.436567)


def test_parse_sshare_survives_parent_shares_and_junk():
    shares = slurm.parse_sshare(
        "Account|User|RawShares|NormShares|RawUsage|EffectvUsage|FairShare\n"
        "physics|jdoe|parent|xxx|yyy|0.5|0.25\n"
        "too|few|fields\n"
    )
    assert len(shares) == 1
    assert shares[0].raw_shares == "parent"
    assert shares[0].norm_shares == 0.0        # unparseable -> 0, not a crash
    assert shares[0].fairshare == pytest.approx(0.25)


def test_share_ratio_and_reading():
    share = FairShare("physics", "jdoe", "5", 0.016835, 2738593, 0.056350, 0.436567)
    assert share.share_ratio == pytest.approx(3.347, rel=1e-3)
    assert "over your share" in share.reading
    assert "3.3x your share" in share.reading


@pytest.mark.parametrize(
    "factor,fragment",
    [
        (0.95, "well under your share"),
        (0.60, "a little under"),
        (0.50, "about exactly your share"),
        (0.30, "over your share"),
        (0.05, "heavily deprioritised"),
        (None, "no fairshare factor"),
    ],
)
def test_fairshare_readings(factor, fragment):
    assert fragment in FairShare("a", "u", "1", 0.02, 0, 0.02, factor).reading


def test_share_ratio_without_an_entitlement():
    assert FairShare("a", "u", "0", 0.0, 0, 0.5, 0.1).share_ratio is None


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

TODAY = datetime(2026, 8, 12, 17, 0, 0)


@pytest.mark.parametrize(
    "window,start,label",
    [
        ("month", "2026-08-01", "this month"),
        ("30d", "2026-07-13", "last 30 days"),
        ("year", "2026-01-01", "this year"),
        ("nonsense", "2026-08-01", "this month"),   # falls back to the month
    ],
)
def test_usage_window(window, start, label):
    assert slurm.usage_window(window, TODAY) == (start, "now", label)


def test_next_usage_window_cycles():
    assert slurm.next_usage_window("month") == "30d"
    assert slurm.next_usage_window("30d") == "year"
    assert slurm.next_usage_window("year") == "month"
    assert slurm.next_usage_window("bogus") == "month"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def test_get_account_usage_builds_the_command_and_sorts(monkeypatch):
    calls = {}

    async def _fake(*args):
        calls["args"] = args
        return SREPORT_OUT, "", 0

    monkeypatch.setattr(slurm, "_run_cmd", _fake)
    rows = _run(slurm.get_account_usage("year", account="physics", today=TODAY))
    assert calls["args"][:3] == ("sreport", "cluster", "AccountUtilizationByUser")
    assert "start=2026-01-01" in calls["args"] and "end=now" in calls["args"]
    assert "-t" in calls["args"] and "hours" in calls["args"] and "-P" in calls["args"]
    assert "account=physics" in calls["args"]
    # biggest consumer first, account total last
    assert [r.user for r in rows] == ["jdoe", "asmith", "bpatel", ""]


def test_get_fairshare_asks_for_a_user(monkeypatch):
    calls = {}

    async def _fake(*args):
        calls["args"] = args
        return SSHARE_OUT, "", 0

    monkeypatch.setattr(slurm, "_run_cmd", _fake)
    shares = _run(slurm.get_fairshare("jdoe"))
    assert calls["args"][0] == "sshare" and "-P" in calls["args"]
    assert "-u" in calls["args"] and "jdoe" in calls["args"]
    assert len(shares) == 2


def test_get_fairshare_without_a_user_asks_for_the_caller(monkeypatch):
    calls = {}

    async def _fake(*args):
        calls["args"] = args
        return SSHARE_OUT, "", 0

    monkeypatch.setattr(slurm, "_run_cmd", _fake)
    _run(slurm.get_fairshare())
    assert "-U" in calls["args"]


@pytest.mark.parametrize(
    "stderr",
    ["sreport: command not found", "Slurm accounting_storage is not configured",
     "slurmdbd: error: no connection"],
)
def test_absent_accounting_is_detected_and_remembered(monkeypatch, stderr):
    async def _fail(*args):
        return "", stderr, 1

    monkeypatch.setattr(slurm, "_run_cmd", _fail)
    monkeypatch.setattr(slurm, "_accounting_missing", False)
    assert _run(slurm.get_account_usage("month", today=TODAY)) == []
    assert slurm.accounting_available() is False


def test_an_ordinary_failure_does_not_claim_accounting_is_missing(monkeypatch):
    async def _fail(*args):
        return "", "sreport: error: Problem with query", 1

    monkeypatch.setattr(slurm, "_run_cmd", _fail)
    monkeypatch.setattr(slurm, "_accounting_missing", False)
    assert _run(slurm.get_account_usage("month", today=TODAY)) == []
    assert slurm.accounting_available() is True


# ---------------------------------------------------------------------------
# Table widget
# ---------------------------------------------------------------------------


def test_format_hours():
    assert format_hours(2472) == "2 472"
    assert format_hours(68364) == "68 364"
    assert format_hours(912.5) == "912"
    assert format_hours(9.5) == "9.5"


def test_share_bar():
    assert share_bar(0.5, 16) == "█" * 8 + "░" * 8
    assert share_bar(1.0, 16) == "█" * 16
    assert share_bar(0.0, 16) == "░" * 16
    assert share_bar(0.001, 16).startswith("█")   # a sliver still shows
    assert share_bar(5.0, 16) == "█" * 16         # clamped


def test_usage_table_totals_exclude_the_account_row():
    rows = slurm.parse_sreport(SREPORT_OUT)
    table = UsageTable(user="jdoe")
    table._rows = rows
    assert table.total_hours == 8200 + 3100 + 1200   # not the 12500 account row
    assert table.my_hours == 8200


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------


def _fake_cmd(sreport=SREPORT_OUT, sshare=SSHARE_OUT, rc=0, stderr=""):
    async def _fake(*args):
        if args[0] == "sreport":
            return sreport, stderr, rc
        if args[0] == "sshare":
            return sshare, stderr, rc
        return "", "", 0
    return _fake


async def _open_usage(pilot, app):
    await pilot.press("U")
    await pilot.pause()
    return app.screen


def test_usage_screen_opens_immediately_with_a_placeholder(monkeypatch):
    started = asyncio.Event()

    async def _slow(*args):
        started.set()
        await asyncio.sleep(5)          # sreport being slow
        return SREPORT_OUT, "", 0

    async def scenario():
        monkeypatch.setattr(slurm, "_run_cmd", _slow)
        app = LazySlurmApp(config=Config(refresh=0, no_live=True, no_gpu=True, user="jdoe"))
        async with app.run_test(size=(110, 28)) as pilot:
            await pilot.pause()
            screen = await _open_usage(pilot, app)
            assert isinstance(screen, UsageScreen)
            # The screen is up and says so before any data has arrived.
            assert "loading" in plain(screen.query_one("#usage-bar", Static).render())

    _run(scenario())


def test_usage_screen_fills_in_and_marks_your_row(monkeypatch):
    async def scenario():
        monkeypatch.setattr(slurm, "_run_cmd", _fake_cmd())
        app = LazySlurmApp(config=Config(refresh=0, no_live=True, no_gpu=True, user="jdoe"))
        async with app.run_test(size=(110, 28)) as pilot:
            await pilot.pause()
            screen = await _open_usage(pilot, app)
            for _ in range(40):
                await pilot.pause()
                if "loading" not in plain(screen.query_one("#usage-bar", Static).render()):
                    break
                await asyncio.sleep(0.05)
            bar = plain(screen.query_one("#usage-bar", Static).render())
            assert "this month" in bar
            assert "8 200" in bar                     # my hours
            assert "12 500" in bar                    # account total from sreport
            table = screen.query_one("#usage-table", UsageTable)
            assert table.row_count == 3               # users only
            assert "▸ jdoe" in str(table.get_cell("jdoe", "User"))
            shares = plain(screen.query_one("#usage-fairshare", Static).render())
            assert "0.437" in shares and "over your share" in shares

    _run(scenario())


def test_usage_screen_cycles_the_window(monkeypatch):
    seen: list[str] = []

    async def _record(*args):
        if args[0] == "sreport":
            seen.append(next(a for a in args if a.startswith("start=")))
            return SREPORT_OUT, "", 0
        return SSHARE_OUT, "", 0

    async def scenario():
        monkeypatch.setattr(slurm, "_run_cmd", _record)
        app = LazySlurmApp(config=Config(refresh=0, no_live=True, no_gpu=True, user="jdoe"))
        async with app.run_test(size=(110, 28)) as pilot:
            await pilot.pause()
            screen = await _open_usage(pilot, app)
            for key in ("w", "w"):
                await pilot.press(key)
                for _ in range(20):
                    await pilot.pause()
                    await asyncio.sleep(0.02)
            assert screen.window == "year"
            assert len(seen) >= 3           # one fetch per window
            assert len(set(seen)) >= 3      # each asked for a different start

    _run(scenario())


def test_usage_screen_says_when_accounting_is_absent(monkeypatch):
    async def scenario():
        monkeypatch.setattr(slurm, "_accounting_missing", False)
        monkeypatch.setattr(slurm, "_run_cmd",
                            _fake_cmd(sreport="", sshare="", rc=1,
                                      stderr="sreport: command not found"))
        app = LazySlurmApp(config=Config(refresh=0, no_live=True, no_gpu=True, user="jdoe"))
        async with app.run_test(size=(110, 28)) as pilot:
            await pilot.pause()
            screen = await _open_usage(pilot, app)
            for _ in range(40):
                await pilot.pause()
                text = plain(screen.query_one("#usage-bar", Static).render())
                if "loading" not in text:
                    break
                await asyncio.sleep(0.05)
            assert "no Slurm accounting" in text
            assert screen.query_one("#usage-table", UsageTable).row_count == 0

    _run(scenario())


def test_escape_returns_from_the_usage_screen(monkeypatch):
    async def scenario():
        monkeypatch.setattr(slurm, "_run_cmd", _fake_cmd())
        app = LazySlurmApp(config=Config(refresh=0, no_live=True, no_gpu=True, user="jdoe"))
        async with app.run_test(size=(110, 28)) as pilot:
            await pilot.pause()
            await _open_usage(pilot, app)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, UsageScreen)

    _run(scenario())


def test_usage_is_never_fetched_by_the_poll_loop(monkeypatch):
    """Acceptance criterion: on open and on r, never in the poll loop."""
    calls: list[str] = []

    async def _record(*args):
        calls.append(args[0])
        return "", "", 0

    async def scenario():
        monkeypatch.setattr(slurm, "_run_cmd", _record)
        app = LazySlurmApp(config=Config(refresh=0, no_live=True, no_gpu=True, user="jdoe"))
        async with app.run_test(size=(110, 28)) as pilot:
            await pilot.pause()
            await app._poll_jobs()
            await pilot.pause()
        return calls

    commands = _run(scenario())
    assert "sreport" not in commands
    assert "sshare" not in commands
