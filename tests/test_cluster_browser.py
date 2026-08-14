"""The cluster browser, and what detach means (#62).

The distinction these are really about is detach vs. disconnect: one keeps the
SSH master alive so coming back is instant, the other closes it so coming back
means authenticating again. On a cluster with two-factor login that difference
is the whole feature.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from lazyslurm import config as persistent_config
from lazyslurm import slurm
from lazyslurm.app import ClusterScreen, LazySlurmApp
from lazyslurm.models import Config
from lazyslurm.widgets.cluster_view import (
    ATTACHED,
    CLOSED,
    DETACHED,
    ClusterTable,
    format_last_seen,
)


@pytest.fixture(autouse=True)
def _clusters_file(tmp_path, monkeypatch):
    monkeypatch.setattr(persistent_config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(persistent_config, "CLUSTERS_FILE", tmp_path / "clusters.json")
    monkeypatch.setattr(slurm, "_sessions", {})
    monkeypatch.setattr(slurm, "_attached", "")
    return tmp_path


class _FakeSession:
    """Stands in for SSHSession: connects without a network, records closes."""

    def __init__(self, host, prompt_cb=None, **kwargs):
        self.host = host
        self.connected = False
        self.closed = False
        self.connects = 0

    async def connect(self):
        self.connects += 1
        self.connected = True
        return True, f"Connected to {self.host}"

    async def close(self):
        self.closed = True
        self.connected = False

    async def run(self, command, timeout=None):
        # Enough for get_cluster_name; nothing here should reach a network.
        if "scontrol" in command:
            return "ClusterName = test-cluster\n", "", 0
        return "", "", 0


# --- remembering -----------------------------------------------------------


def test_a_cluster_is_remembered_once_per_target():
    persistent_config.remember_cluster("me@a.edu", "alpha", "me")
    persistent_config.remember_cluster("me@a.edu", "alpha", "me")

    assert [e["host"] for e in persistent_config.known_clusters()] == ["me@a.edu"]


def test_the_most_recently_seen_comes_first():
    persistent_config.remember_cluster("me@a.edu", "alpha")
    time.sleep(0.01)
    persistent_config.remember_cluster("me@b.edu", "beta")

    assert [e["host"] for e in persistent_config.known_clusters()] == [
        "me@b.edu", "me@a.edu",
    ]


def test_forgetting_one_leaves_the_others():
    persistent_config.remember_cluster("me@a.edu")
    persistent_config.remember_cluster("me@b.edu")

    assert persistent_config.forget_cluster("me@a.edu") is True
    assert persistent_config.forget_cluster("me@a.edu") is False
    assert [e["host"] for e in persistent_config.known_clusters()] == ["me@b.edu"]


def test_a_corrupt_cluster_list_is_not_fatal(_clusters_file):
    (_clusters_file / "clusters.json").write_text("{ not json")
    assert persistent_config.known_clusters() == []


# --- attaching, detaching, disconnecting -----------------------------------


def test_detaching_keeps_the_session_open(monkeypatch):
    monkeypatch.setattr(slurm, "SSHSession", _FakeSession)

    ok, _ = asyncio.run(slurm.connect_remote(config=Config(remote="me@a.edu")))
    assert ok and slurm.attached_host() == "me@a.edu"

    assert slurm.detach_remote() == "me@a.edu"
    assert slurm.attached_host() == ""
    assert slurm.open_hosts() == ["me@a.edu"]        # still connected
    assert slurm.get_session() is None               # but nothing runs there


def test_reattaching_does_not_log_in_again(monkeypatch):
    """The point of detach: no second verification code."""
    monkeypatch.setattr(slurm, "SSHSession", _FakeSession)

    asyncio.run(slurm.connect_remote(config=Config(remote="me@a.edu")))
    session = slurm._sessions["me@a.edu"]
    slurm.detach_remote()
    ok, msg = asyncio.run(slurm.connect_remote(config=Config(remote="me@a.edu")))

    assert ok and "Reattached" in msg
    assert session.connects == 1                     # not connected a second time


def test_disconnecting_closes_it(monkeypatch):
    monkeypatch.setattr(slurm, "SSHSession", _FakeSession)

    asyncio.run(slurm.connect_remote(config=Config(remote="me@a.edu")))
    session = slurm._sessions["me@a.edu"]
    asyncio.run(slurm.disconnect_remote("me@a.edu"))

    assert session.closed
    assert slurm.open_hosts() == []
    assert slurm.attached_host() == ""


def test_two_clusters_can_be_open_at_once(monkeypatch):
    monkeypatch.setattr(slurm, "SSHSession", _FakeSession)

    asyncio.run(slurm.connect_remote(config=Config(remote="me@a.edu")))
    asyncio.run(slurm.connect_remote(config=Config(remote="me@b.edu")))

    assert slurm.open_hosts() == ["me@a.edu", "me@b.edu"]
    assert slurm.attached_host() == "me@b.edu"
    # Switching back is a reattach, not a login.
    asyncio.run(slurm.connect_remote(config=Config(remote="me@a.edu")))
    assert slurm.attached_host() == "me@a.edu"
    assert slurm._sessions["me@a.edu"].connects == 1


def test_quitting_closes_every_session_including_detached(monkeypatch):
    """A master outliving the app is a surprise, and there is no UI to find it."""
    monkeypatch.setattr(slurm, "SSHSession", _FakeSession)

    asyncio.run(slurm.connect_remote(config=Config(remote="me@a.edu")))
    asyncio.run(slurm.connect_remote(config=Config(remote="me@b.edu")))
    slurm.detach_remote()
    sessions = list(slurm._sessions.values())

    asyncio.run(slurm.disconnect_all())

    assert all(s.closed for s in sessions)
    assert slurm.open_hosts() == []


def test_a_failed_connection_is_not_remembered_as_open(monkeypatch):
    class _Failing(_FakeSession):
        async def connect(self):
            return False, "Permission denied"

    monkeypatch.setattr(slurm, "SSHSession", _Failing)
    ok, _ = asyncio.run(slurm.connect_remote(config=Config(remote="me@a.edu")))

    assert not ok
    assert slurm.open_hosts() == []
    assert slurm.attached_host() == ""


def test_switching_clusters_drops_the_poll_caches(monkeypatch):
    """Those answers are about the cluster just left."""
    monkeypatch.setattr(slurm, "SSHSession", _FakeSession)

    asyncio.run(slurm.connect_remote(config=Config(remote="me@a.edu")))
    slurm._sinfo_cache = object()
    slurm._job_count_cache = object()
    asyncio.run(slurm.connect_remote(config=Config(remote="me@b.edu")))

    assert slurm._sinfo_cache is None
    assert slurm._job_count_cache is None


# --- the table -------------------------------------------------------------


@pytest.mark.parametrize(
    "host,attached,open_hosts,expected",
    [
        ("a", "a", ("a",), ATTACHED),
        ("a", "", ("a",), DETACHED),
        ("a", "", (), CLOSED),
        ("a", "b", ("a", "b"), DETACHED),
    ],
)
def test_session_state(host, attached, open_hosts, expected):
    assert ClusterTable.session_state(host, attached, open_hosts) == expected


@pytest.mark.parametrize(
    "ago,expected",
    [
        (0, "just now"),
        (60 * 10, "10 minutes ago"),
        (3600 * 3, "3 hours ago"),
        (86400 * 3, "3 days ago"),
        (86400 * 21, "3 weeks ago"),
    ],
)
def test_format_last_seen(ago, expected):
    now = time.time()
    assert format_last_seen(now - ago, now) == expected


def test_never_seen_reads_as_never():
    assert format_last_seen(0) == "never"


# --- the screen ------------------------------------------------------------


def _app(monkeypatch, **config):
    async def _empty(*a, **k):
        return []

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(slurm, "get_running_jobs", _empty)
    monkeypatch.setattr(slurm, "get_completed_jobs", _empty)
    monkeypatch.setattr(slurm, "get_partition_availability", _empty)
    monkeypatch.setattr(slurm, "get_job_detail", _none)
    monkeypatch.setattr(slurm, "get_job_stats", _none)
    return LazySlurmApp(config=Config(refresh=0, no_live=True, **config))


def test_the_browser_lists_what_is_remembered(monkeypatch):
    persistent_config.remember_cluster("me@a.edu", "alpha", "me")
    persistent_config.remember_cluster("me@b.edu", "beta", "me")

    async def scenario():
        app = _app(monkeypatch)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app.push_screen(ClusterScreen(app.config))
            await pilot.pause()
            table = app.screen.query_one("#cluster-table", ClusterTable)
            assert table.row_count == 2
            assert table.get_entry("me@a.edu")["cluster"] == "alpha"

    asyncio.run(scenario())


def test_an_empty_browser_says_how_to_get_a_cluster(monkeypatch):
    async def scenario():
        app = _app(monkeypatch)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app.push_screen(ClusterScreen(app.config))
            await pilot.pause()
            from textual.widgets import Static

            hint = str(app.screen.query_one("#cluster-bar-top", Static).content)
            assert "--remote" in hint

    asyncio.run(scenario())


def test_a_connected_cluster_is_not_forgotten_out_from_under_you(monkeypatch):
    monkeypatch.setattr(slurm, "SSHSession", _FakeSession)
    persistent_config.remember_cluster("me@a.edu", "alpha")

    async def scenario():
        app = _app(monkeypatch)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await slurm.connect_remote(config=Config(remote="me@a.edu"))
            await app.push_screen(ClusterScreen(app.config))
            await pilot.pause()
            app.screen.action_forget()
            await pilot.pause()
            assert persistent_config.known_clusters()          # still there

            await slurm.disconnect_remote("me@a.edu")
            app.screen.action_forget()
            await pilot.pause()
            assert persistent_config.known_clusters() == []

    asyncio.run(scenario())


def test_detaching_empties_the_job_view_and_stops_polling(monkeypatch):
    monkeypatch.setattr(slurm, "SSHSession", _FakeSession)

    async def scenario():
        app = _app(monkeypatch, remote="me@a.edu")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await slurm.connect_remote(config=app.config)
            app._selected_job_id = "4815"
            app._polling = True

            app.detach_cluster()
            await pilot.pause()

            assert app._selected_job_id is None
            assert app._polling is False
            assert slurm.open_hosts() == ["me@a.edu"]   # detached, not closed

    asyncio.run(scenario())
