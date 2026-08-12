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
