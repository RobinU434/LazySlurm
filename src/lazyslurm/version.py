"""Which LazySlurm is running: a release, or somebody's checkout.

The number in ``__init__`` is the release version and the only one PyPI knows
about. It says nothing about a working copy, where the interesting question is
which commit you are looking at -- so when the running code came from git we
append the short hash.

There are two ways to arrive from git, and they leave different traces:

* an editable install or a plain ``python -m lazyslurm`` in a clone, where the
  source sits in a work tree and ``git`` can be asked directly;
* ``pip install git+https://...``, where the work tree is long gone but the
  installer recorded the commit in ``direct_url.json`` (PEP 610).

Both end up as a PEP 440 local version, ``0.3.0+g1a2b3c4``.
"""

from __future__ import annotations

import json
import subprocess
from functools import lru_cache
from importlib import metadata
from pathlib import Path

from lazyslurm import __version__

__all__ = ["commit_hash", "version_string"]

DISTRIBUTION = "lazyslurm-py"

# Enough to be unambiguous in any repository this size, short enough to sit in
# the footer without crowding out a key binding.
_HASH_LENGTH = 7

# git normally answers instantly; the ceiling is only here so that a wedged
# filesystem cannot hold up the whole TUI on startup.
_GIT_TIMEOUT = 2.0


def _checkout_root() -> Path | None:
    """The repository this module was imported from, if that is where it lives.

    Deliberately not a walk up the tree looking for any ``.git``: a virtualenv
    created inside an unrelated repository would find that one and report a
    commit that has nothing to do with LazySlurm. The source layout is known
    (``<root>/src/lazyslurm``), so require exactly it.
    """
    package = Path(__file__).resolve().parent
    if package.parent.name != "src":
        return None
    root = package.parent.parent
    # A file rather than a directory in a linked worktree, so no is_dir() here.
    return root if (root / ".git").exists() else None


def _git_commit() -> str:
    root = _checkout_root()
    if root is None:
        return ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", f"--short={_HASH_LENGTH}", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        # No git binary, or it died. Falling back to the bare version is the
        # only sane answer; the version line is decoration, never load-bearing.
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _recorded_commit() -> str:
    """The commit pip installed from, per PEP 610's ``direct_url.json``."""
    try:
        raw = metadata.distribution(DISTRIBUTION).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return ""
    if not raw:
        return ""
    try:
        vcs = json.loads(raw).get("vcs_info") or {}
    except (ValueError, AttributeError):
        return ""
    commit = vcs.get("commit_id") or ""
    return commit[:_HASH_LENGTH] if isinstance(commit, str) else ""


@lru_cache(maxsize=1)
def commit_hash() -> str:
    """Short hash of the commit this install came from, or ``""`` for a release."""
    # The work tree wins: with an editable install both sources exist, and the
    # checkout is the one that moves under you as you switch branches.
    return _git_commit() or _recorded_commit()


@lru_cache(maxsize=1)
def version_string() -> str:
    """``0.3.0`` from PyPI, ``0.3.0+g1a2b3c4`` from a checkout."""
    # __version__ rather than importlib.metadata: hatch builds the distribution
    # version from it, and an editable install keeps serving the version
    # recorded at install time long after the source has moved on.
    commit = commit_hash()
    return f"{__version__}+g{commit}" if commit else __version__
