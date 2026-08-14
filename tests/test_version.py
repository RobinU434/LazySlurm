"""The version shown in the footer, and where it comes from.

The interesting cases are all about provenance: a release knows only its
number, a checkout can be asked for its commit, and a pip-from-git install has
neither a work tree nor a plain version -- just the hash the installer wrote
down. These drive each path with the file layouts and command output the real
thing sees.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest

from lazyslurm import __version__, slurm, version as version_mod
from lazyslurm.app import LazySlurmApp
from lazyslurm.models import Config
from lazyslurm.version import commit_hash, version_string
from lazyslurm.widgets.version_footer import VersionFooter, VersionLabel


@pytest.fixture(autouse=True)
def _clear_caches():
    """Both entry points memoise; a stale cache would leak between tests."""
    commit_hash.cache_clear()
    version_string.cache_clear()
    yield
    commit_hash.cache_clear()
    version_string.cache_clear()


# --- provenance ------------------------------------------------------------


def test_a_release_reports_the_bare_version(monkeypatch):
    monkeypatch.setattr(version_mod, "_git_commit", lambda: "")
    monkeypatch.setattr(version_mod, "_recorded_commit", lambda: "")
    assert version_string() == __version__


def test_a_checkout_appends_the_short_commit(monkeypatch):
    monkeypatch.setattr(version_mod, "_git_commit", lambda: "1a2b3c4")
    monkeypatch.setattr(version_mod, "_recorded_commit", lambda: "")
    assert version_string() == f"{__version__}+g1a2b3c4"


def test_the_work_tree_wins_over_the_recorded_commit(monkeypatch):
    # An editable install has both. The checkout is the one that moves under
    # you when you switch branches, so it is the one worth reporting.
    monkeypatch.setattr(version_mod, "_git_commit", lambda: "aaaaaaa")
    monkeypatch.setattr(version_mod, "_recorded_commit", lambda: "bbbbbbb")
    assert commit_hash() == "aaaaaaa"


def test_pip_install_from_git_uses_the_recorded_commit(monkeypatch):
    monkeypatch.setattr(version_mod, "_git_commit", lambda: "")
    monkeypatch.setattr(version_mod, "_recorded_commit", lambda: "9f8e7d6")
    assert version_string() == f"{__version__}+g9f8e7d6"


# --- reading the work tree -------------------------------------------------


def _fake_run(stdout: str = "", returncode: int = 0):
    def run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    return run


def test_git_commit_reads_rev_parse(monkeypatch, tmp_path):
    monkeypatch.setattr(version_mod, "_checkout_root", lambda: tmp_path)
    monkeypatch.setattr(subprocess, "run", _fake_run("1a2b3c4\n"))
    assert version_mod._git_commit() == "1a2b3c4"


def test_a_failing_git_is_not_an_error(monkeypatch, tmp_path):
    # A repository with no commits yet, or a HEAD that cannot be resolved.
    monkeypatch.setattr(version_mod, "_checkout_root", lambda: tmp_path)
    monkeypatch.setattr(subprocess, "run", _fake_run("", returncode=128))
    assert version_mod._git_commit() == ""


def test_a_missing_git_binary_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(version_mod, "_checkout_root", lambda: tmp_path)

    def boom(cmd, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", boom)
    assert version_mod._git_commit() == ""


def test_a_hanging_git_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(version_mod, "_checkout_root", lambda: tmp_path)

    def slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 2.0)

    monkeypatch.setattr(subprocess, "run", slow)
    assert version_mod._git_commit() == ""


def test_git_is_not_run_at_all_outside_a_checkout(monkeypatch):
    monkeypatch.setattr(version_mod, "_checkout_root", lambda: None)

    def boom(cmd, **kwargs):  # pragma: no cover - the point is it never runs
        raise AssertionError("git should not be invoked without a work tree")

    monkeypatch.setattr(subprocess, "run", boom)
    assert version_mod._git_commit() == ""


def test_checkout_root_finds_the_repository(monkeypatch, tmp_path):
    package = tmp_path / "src" / "lazyslurm"
    package.mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(version_mod, "__file__", str(package / "version.py"))
    assert version_mod._checkout_root() == tmp_path


def test_a_linked_worktree_is_still_a_checkout(monkeypatch, tmp_path):
    # `git worktree add` leaves a .git *file* pointing at the real gitdir.
    package = tmp_path / "src" / "lazyslurm"
    package.mkdir(parents=True)
    (tmp_path / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    monkeypatch.setattr(version_mod, "__file__", str(package / "version.py"))
    assert version_mod._checkout_root() == tmp_path


def test_site_packages_inside_a_repository_is_not_a_checkout(monkeypatch, tmp_path):
    # A venv created inside some unrelated git repository. Walking up the tree
    # would find that .git and report a commit from a different project
    # entirely; the layout check is what rules it out.
    (tmp_path / ".git").mkdir()
    package = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages" / "lazyslurm"
    package.mkdir(parents=True)
    monkeypatch.setattr(version_mod, "__file__", str(package / "version.py"))
    assert version_mod._checkout_root() is None


def test_a_source_tree_without_git_is_not_a_checkout(monkeypatch, tmp_path):
    package = tmp_path / "src" / "lazyslurm"
    package.mkdir(parents=True)
    monkeypatch.setattr(version_mod, "__file__", str(package / "version.py"))
    assert version_mod._checkout_root() is None


# --- reading the installer's record ----------------------------------------


class _FakeDistribution:
    def __init__(self, text: str | None) -> None:
        self._text = text

    def read_text(self, name: str) -> str | None:
        return self._text if name == "direct_url.json" else None


def _with_direct_url(monkeypatch, text):
    monkeypatch.setattr(
        version_mod.metadata,
        "distribution",
        lambda name: _FakeDistribution(text),
    )


def test_recorded_commit_reads_direct_url(monkeypatch):
    _with_direct_url(
        monkeypatch,
        json.dumps(
            {
                "url": "https://github.com/RobinU434/LazySlurm.git",
                "vcs_info": {
                    "vcs": "git",
                    "requested_revision": "main",
                    "commit_id": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b",
                },
            }
        ),
    )
    assert version_mod._recorded_commit() == "1a2b3c4"


def test_an_editable_install_records_no_commit(monkeypatch):
    # What pip writes for `pip install -e .`: a path, no vcs_info. The work
    # tree answers this case instead.
    _with_direct_url(
        monkeypatch,
        json.dumps({"url": "file:///home/me/LazySlurm", "dir_info": {"editable": True}}),
    )
    assert version_mod._recorded_commit() == ""


def test_malformed_direct_url_is_not_an_error(monkeypatch):
    _with_direct_url(monkeypatch, "{not json")
    assert version_mod._recorded_commit() == ""


def test_no_direct_url_at_all(monkeypatch):
    _with_direct_url(monkeypatch, None)
    assert version_mod._recorded_commit() == ""


def test_an_uninstalled_package_is_not_an_error(monkeypatch):
    def missing(name):
        raise version_mod.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(version_mod.metadata, "distribution", missing)
    assert version_mod._recorded_commit() == ""


# --- the footer ------------------------------------------------------------


def _app(monkeypatch):
    async def _empty(*a, **k):
        return []

    async def _none(*a, **k):
        return None

    monkeypatch.setattr(slurm, "get_running_jobs", _empty)
    monkeypatch.setattr(slurm, "get_completed_jobs", _empty)
    monkeypatch.setattr(slurm, "get_partition_availability", _empty)
    monkeypatch.setattr(slurm, "get_job_detail", _none)
    monkeypatch.setattr(slurm, "get_job_stats", _none)
    return LazySlurmApp(config=Config(refresh=0, no_gpu=True, no_live=True))


def _footer_line(app) -> str:
    strips = app.screen._compositor.render_strips(app.screen.size)
    return "".join(strip.text for strip in strips[-1:])


def test_the_footer_shows_the_version(monkeypatch):
    async def scenario():
        app = _app(monkeypatch)
        async with app.run_test(size=(100, 24)) as pilot:
            # Footer.compose() yields nothing until the bindings arrive, then
            # recomposes; the label only exists after that second pass.
            await pilot.pause()
            await pilot.pause()
            label = app.query_one(VersionLabel)
            assert str(label.content) == f"v{version_string()}"

    asyncio.run(scenario())


def test_the_version_sits_at_the_right_edge_and_keeps_the_keys(monkeypatch):
    async def scenario():
        app = _app(monkeypatch)
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            await pilot.pause()
            line = _footer_line(app)
            # The bindings are still there; the version has not displaced them.
            assert "Quit" in line
            assert line.rstrip().endswith(f"v{version_string()}")
            assert app.query_one(VersionFooter).region.width == 100

    asyncio.run(scenario())


def test_a_narrow_terminal_still_shows_the_version(monkeypatch):
    async def scenario():
        app = _app(monkeypatch)
        async with app.run_test(size=(60, 24)) as pilot:
            await pilot.pause()
            await pilot.pause()
            assert _footer_line(app).rstrip().endswith(f"v{version_string()}")

    asyncio.run(scenario())
