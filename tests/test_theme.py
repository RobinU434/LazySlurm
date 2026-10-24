"""Light mode that follows the terminal / system (issue #77)."""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from lazyslurm import theme
from lazyslurm.__main__ import parse_theme
from lazyslurm.app import LazySlurmApp
from lazyslurm.models import Config


@pytest.fixture(autouse=True)
def _forget_detection(monkeypatch):
    monkeypatch.setattr(theme, "_detected", None)


@pytest.mark.parametrize("reply, expected", [
    ("\033]11;rgb:ffff/ffff/ffff\033\\", "light"),
    ("\033]11;rgb:1e1e/1e1e/1e1e\033\\", "dark"),
    ("\033]11;rgb:fd/f6/e3\a", "light"),            # two-digit channels, BEL end
    ("\033]11;rgba:0000/2b2b/3636/ffff\033\\", "dark"),  # solarized dark, with alpha
    ("", None),                                      # terminal said nothing
    ("garbage", None),
])
def test_parse_osc11(reply, expected):
    assert theme.parse_osc11(reply) == expected


@pytest.mark.parametrize("value, expected", [
    ("15;0", "dark"),
    ("0;15", "light"),
    ("0;7", "light"),
    ("15;8", "dark"),        # bright black is grey
    ("0;default;15", "light"),
    ("", None),
    ("15;default", None),
])
def test_parse_colorfgbg(value, expected):
    assert theme.parse_colorfgbg(value) == expected


def test_detection_falls_through_in_order(monkeypatch):
    monkeypatch.setattr(theme, "_from_terminal", lambda: None)
    monkeypatch.setenv("COLORFGBG", "0;15")
    monkeypatch.setattr(theme, "_from_macos", lambda: "dark")
    assert theme.detect() == "light"            # COLORFGBG before macOS

    monkeypatch.delenv("COLORFGBG")
    assert theme.detect() == "dark"             # then macOS

    monkeypatch.setattr(theme, "_from_terminal", lambda: "light")
    assert theme.detect() == "light"            # the terminal beats both


def test_macos_appearance(monkeypatch):
    def fake_run(stdout, rc):
        return lambda *a, **k: subprocess.CompletedProcess(a, rc, stdout=stdout, stderr="")

    monkeypatch.setattr(theme.sys, "platform", "darwin")
    monkeypatch.setattr(theme.subprocess, "run", fake_run("Dark\n", 0))
    assert theme._from_macos() == "dark"
    # Light mode has no AppleInterfaceStyle key at all, so the read fails.
    monkeypatch.setattr(theme.subprocess, "run", fake_run("", 1))
    assert theme._from_macos() == "light"
    monkeypatch.setattr(theme.sys, "platform", "linux")
    assert theme._from_macos() is None


def test_no_terminal_no_query(monkeypatch):
    # Under pytest, piped output or cron there is no terminal to ask.
    monkeypatch.setattr(theme.sys.stdin, "isatty", lambda: False)
    assert theme._from_terminal() is None


def test_textual_theme():
    assert theme.textual_theme("light") == "textual-light"
    assert theme.textual_theme("dark") == "textual-dark"
    assert theme.textual_theme("auto") == "textual-dark"   # nothing detected
    theme._detected = "light"
    assert theme.textual_theme("auto") == "textual-light"
    assert theme.textual_theme("dark") == "textual-dark"   # explicit wins


def test_parse_theme():
    assert parse_theme("light") == ("light", "")
    assert parse_theme(" Dark ") == ("dark", "")
    value, warning = parse_theme("solarized")
    assert value == "auto"
    assert "theme" in warning and "auto | dark | light" in warning


def test_app_starts_in_the_configured_theme():
    async def scenario(setting):
        app = LazySlurmApp(config=Config(theme=setting, refresh=0, no_live=True))
        return app.theme

    assert asyncio.run(scenario("light")) == "textual-light"
    assert asyncio.run(scenario("dark")) == "textual-dark"
    theme._detected = "light"
    assert asyncio.run(scenario("auto")) == "textual-light"
