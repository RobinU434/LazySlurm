"""Tests for the remote-mode UI: the SSH prompt modal and the ssh/scp helpers."""

from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from lazyslurm import slurm
from lazyslurm.app import LazySlurmApp, SSHPromptScreen
from lazyslurm.models import Config


class _Harness(App):
    result: str | None = "unset"

    def compose(self) -> ComposeResult:
        yield Static("host")


def _run(coro):
    return asyncio.run(coro)


def test_prompt_modal_masks_secrets_and_returns_the_answer():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(
                SSHPromptScreen("me@login", "Verification code:", secret=True),
                callback=lambda r: setattr(app, "result", r),
            )
            await pilot.pause()
            field = app.screen.query_one("#ssh-answer", Input)
            assert field.password is True  # never echoed to the screen
            assert field.has_focus
            await pilot.press("1", "2", "3", "4", "5", "6", "enter")
            await pilot.pause()
            assert app.result == "123456"

    _run(scenario())


def test_prompt_modal_shows_the_servers_own_prompt_text():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(SSHPromptScreen("me@login", "Duo passcode or option (1-3):"))
            await pilot.pause()
            label = app.screen.query_one(".prompt", Static)
            assert "Duo passcode" in str(label.render())

    _run(scenario())


def test_host_key_question_is_not_masked():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(
                SSHPromptScreen("me@login", "(yes/no/[fingerprint])?", secret=False)
            )
            await pilot.pause()
            assert app.screen.query_one("#ssh-answer", Input).password is False

    _run(scenario())


def test_escape_cancels_the_prompt():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(
                SSHPromptScreen("me@login", "Password:"),
                callback=lambda r: setattr(app, "result", r),
            )
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.result is None

    _run(scenario())


class _StubSession:
    control_path = "/home/u/.ssh/cm-lazyslurm/abc123.sock"


def test_ssh_helpers_reuse_the_live_control_socket(monkeypatch):
    app = LazySlurmApp(config=Config(remote="me@login.hpc.edu"))
    monkeypatch.setattr(slurm, "get_session", lambda: _StubSession())

    opt = app._ssh_control_opt()
    assert "ControlPath=/home/u/.ssh/cm-lazyslurm/abc123.sock" in opt

    proxy = app._proxy_command()
    assert proxy.startswith("ssh ")
    assert "-W %h:%p" in proxy
    assert "me@login.hpc.edu" in proxy
    assert "ControlPath=" in proxy  # rides the authenticated connection
    assert "-J" not in proxy        # never a second login-node connection


def test_ssh_helpers_degrade_without_a_session(monkeypatch):
    app = LazySlurmApp(config=Config(remote="me@login.hpc.edu"))
    monkeypatch.setattr(slurm, "get_session", lambda: None)
    assert app._ssh_control_opt() == ""
    # Still a usable ProxyCommand, just without socket reuse.
    assert "-W %h:%p" in app._proxy_command()


# ---------------------------------------------------------------------------
# Pager integration ("l")
# ---------------------------------------------------------------------------


def _capture_system(monkeypatch, app):
    """Capture the command line the pager step would run, without running it."""
    calls: list[str] = []
    monkeypatch.setattr("lazyslurm.app.os.system", lambda cmd: calls.append(cmd) or 0)

    class _NoSuspend:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(type(app), "suspend", lambda self: _NoSuspend())
    monkeypatch.setattr(type(app), "_log", lambda self, *a, **k: None)
    return calls


def test_pager_opens_the_local_log_at_the_end(monkeypatch, tmp_path):
    log = tmp_path / "job.out"
    log.write_text("hello\n")
    app = LazySlurmApp(config=Config(pager="less"))
    calls = _capture_system(monkeypatch, app)
    monkeypatch.setattr("lazyslurm.app.shutil.which", lambda name: "/usr/bin/less")

    _run(app._page_file(str(log), "stdout"))
    assert len(calls) == 1
    # -R keeps the log's colors, +G opens at the newest lines.
    assert calls[0] == f"less -R +G {log}"


def test_pager_runs_on_the_cluster_in_remote_mode(monkeypatch):
    app = LazySlurmApp(config=Config(remote="me@login", pager="less"))
    calls = _capture_system(monkeypatch, app)
    monkeypatch.setattr("lazyslurm.app.shutil.which", lambda name: "/usr/bin/ssh")
    monkeypatch.setattr(slurm, "get_session", lambda: _StubSession())

    _run(app._page_file("/scratch/logs/big.err", "stderr"))
    sent = calls[0]
    # The file is never copied down: less runs remotely, over the live socket.
    assert sent.startswith("ssh -t ")
    assert "ControlPath=/home/u/.ssh/cm-lazyslurm/abc123.sock" in sent
    assert "me@login" in sent
    assert "less -R +G /scratch/logs/big.err" in sent


def test_pager_reports_a_missing_pager(monkeypatch, tmp_path):
    log = tmp_path / "job.out"
    log.write_text("x\n")
    app = LazySlurmApp(config=Config(pager="nosuchpager"))
    calls = _capture_system(monkeypatch, app)
    monkeypatch.setattr("lazyslurm.app.shutil.which", lambda name: None)

    _run(app._page_file(str(log), "stdout"))
    assert calls == []  # nothing was run


def test_pager_without_a_log_path_does_nothing(monkeypatch):
    app = LazySlurmApp(config=Config())
    calls = _capture_system(monkeypatch, app)
    _run(app._page_file(None, "stdout"))
    assert calls == []
