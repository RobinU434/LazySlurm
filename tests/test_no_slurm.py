"""Running off a cluster must fail politely, not with a traceback.

LazySlurm shells out to squeue/sacct for everything, so on a machine without
Slurm the very first poll used to raise FileNotFoundError and take the whole
TUI down. Two behaviours are covered here: the startup check that explains what
to do instead, and the command layer degrading to "no data" rather than raising
if a binary goes missing while the app is running.
"""

from __future__ import annotations

import asyncio

import pytest

from lazyslurm import slurm
from lazyslurm.__main__ import (
    REQUIRED_COMMANDS,
    _no_slurm_message,
    missing_commands,
)
from lazyslurm.app import LazySlurmApp
from lazyslurm.models import Config


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The command layer degrades instead of raising
# ---------------------------------------------------------------------------


def test_a_missing_binary_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(slurm, "_config", Config())
    monkeypatch.setattr(slurm, "_missing_commands", set())

    async def _boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "squeue")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)

    stdout, stderr, rc = _run(slurm._run_cmd("squeue", "-u", "me"))
    assert rc == 127                      # the shell's "command not found"
    assert "squeue" in stderr
    assert stdout == ""


def test_job_queries_come_back_empty_rather_than_exploding(monkeypatch):
    monkeypatch.setattr(slurm, "_config", Config())

    async def _boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "squeue")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)

    assert _run(slurm.get_running_jobs(Config())) == []
    assert _run(slurm.get_completed_jobs(Config())) == []
    assert _run(slurm.get_partitions(Config())) == []


def test_the_missing_binary_is_mentioned_once_not_every_poll(monkeypatch):
    notices: list[tuple[str, str]] = []
    monkeypatch.setattr(slurm, "_config", Config())
    monkeypatch.setattr(slurm, "_missing_commands", set())
    slurm.set_notice_callback(lambda action, detail: notices.append((action, detail)))

    async def _boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "squeue")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    try:
        for _ in range(5):                # five polls
            _run(slurm._run_cmd("squeue"))
    finally:
        slurm.set_notice_callback(None)

    assert len(notices) == 1, notices     # not once per poll
    assert "squeue" in notices[0][1]


def test_the_app_starts_and_polls_with_no_slurm_at_all(monkeypatch):
    """The exact scenario: launch on a laptop, nothing installed."""
    monkeypatch.setattr(slurm, "_config", Config())

    async def _boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "squeue")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)

    async def scenario():
        app = LazySlurmApp(config=Config(refresh=0, no_live=True, no_gpu=True))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app._poll_jobs()        # used to raise FileNotFoundError
            await pilot.pause()
            return app.query_one("#active-jobs").row_count

    assert _run(scenario()) == 0


# ---------------------------------------------------------------------------
# The startup check
# ---------------------------------------------------------------------------


def test_missing_commands_lists_what_is_absent(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert missing_commands() == list(REQUIRED_COMMANDS)


def test_missing_commands_is_quiet_when_slurm_is_installed(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    assert missing_commands() == []


def test_remote_mode_needs_ssh_locally_not_slurm(monkeypatch):
    # The Slurm commands run on the cluster; only ssh has to be here.
    monkeypatch.setattr("shutil.which", lambda name: None if name == "ssh" else "/x")
    assert missing_commands("me@login.hpc") == ["ssh"]

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/ssh" if name == "ssh" else None)
    assert missing_commands("me@login.hpc") == []


@pytest.mark.parametrize("remote", ["", "me@login.hpc"])
def test_the_message_says_what_to_do_next(remote):
    text = _no_slurm_message(["squeue"] if not remote else ["ssh"], remote)
    assert "lazyslurm:" in text
    if remote:
        assert "ssh" in text
    else:
        # The whole point: point at --remote rather than just reporting a fault.
        assert "--remote" in text
