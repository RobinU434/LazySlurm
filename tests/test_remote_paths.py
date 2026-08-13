"""Quoting of remote paths that contain spaces or apostrophes (#39, #40).

A remote path passes through more shells than a local one, and each hop needs
its own round of quoting. These assert the command strings rather than running
them: what matters is that the path survives as a single word on the far side.
"""

from __future__ import annotations

import asyncio
import shlex

import pytest

from lazyslurm import slurm
from lazyslurm.app import LazySlurmApp
from lazyslurm.models import Config

# Paths that are entirely legitimate and used to break.
AWKWARD = [
    "/work/my runs/slurm-4815.out",       # space
    "/work/bens'runs/slurm-4815.out",     # apostrophe
    "/work/a b's c/slurm-4815.out",       # both
]


def _app(monkeypatch, remote="me@login.hpc"):
    async def _none(*a, **k):
        return None

    async def _empty(*a, **k):
        return []

    monkeypatch.setattr(slurm, "get_running_jobs", _empty)
    monkeypatch.setattr(slurm, "get_completed_jobs", _empty)
    monkeypatch.setattr(slurm, "get_partition_availability", _empty)
    monkeypatch.setattr(slurm, "get_job_detail", _none)
    monkeypatch.setattr(slurm, "get_job_stats", _none)
    return LazySlurmApp(config=Config(refresh=0, no_live=True, no_gpu=True, remote=remote))


@pytest.mark.parametrize("path", AWKWARD)
def test_scp_survives_both_shells(monkeypatch, path):
    """The local shell strips one layer; the remote shell must still see one word."""
    calls: list[str] = []

    def _fake_system(cmd):
        calls.append(cmd)
        return 1  # pretend the copy failed, so no editor is launched

    monkeypatch.setattr("os.system", _fake_system)
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/vim")

    async def scenario():
        app = _app(monkeypatch)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app._open_in_editor(path, "stdout")
            await pilot.pause()

    asyncio.run(scenario())

    assert calls, "scp was never invoked"
    # What the local shell hands to scp.
    local_words = shlex.split(calls[0])
    assert local_words[0] == "scp"
    remote_arg = next(w for w in local_words if w.startswith("me@login.hpc:"))
    remote_path = remote_arg.split(":", 1)[1]
    # ...and what the far-side shell then makes of the path.
    assert shlex.split(remote_path) == [path]


def test_scp_quotes_the_host_too(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("os.system", lambda cmd: (calls.append(cmd), 1)[1])
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/vim")

    async def scenario():
        app = _app(monkeypatch, remote="me@login.hpc")
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app._open_in_editor("/work/plain.out", "stdout")
            await pilot.pause()

    asyncio.run(scenario())
    assert shlex.split(calls[0])[0] == "scp"
    assert any(w.startswith("me@login.hpc:") for w in shlex.split(calls[0]))


@pytest.mark.parametrize("path", AWKWARD)
def test_remote_tail_sends_the_path_as_one_word(monkeypatch, path):
    sent: dict[str, str] = {}

    async def _fake_remote(cmd, timeout=None):
        sent["cmd"] = cmd
        return "contents\n", "", 0

    monkeypatch.setattr(slurm, "_run_remote", _fake_remote)
    monkeypatch.setattr(slurm, "_config", Config(remote="me@login.hpc"))

    assert asyncio.run(slurm.read_log_file(path, tail_lines=5)) == "contents\n"
    assert shlex.split(sent["cmd"]) == ["tail", "-n", "5", path]
