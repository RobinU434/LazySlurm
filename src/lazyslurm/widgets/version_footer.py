"""The standard footer with the running version pinned to its right edge."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Footer, Label

from lazyslurm.version import version_string

__all__ = ["VersionFooter", "VersionLabel"]


class VersionLabel(Label):
    """The ``v0.3.0+g1a2b3c4`` in the corner."""


class VersionFooter(Footer):
    """Footer that also answers "which build am I looking at?".

    Useful when several clusters, a login node and a laptop each have their own
    install: a bug report that names the commit is worth a great deal more than
    one that names the version.
    """

    DEFAULT_CSS = """
    VersionFooter {
        VersionLabel {
            dock: right;
            padding: 0 1;
            color: $text-muted;
            background: transparent;
        }
    }
    """

    def compose(self) -> ComposeResult:
        yield from super().compose()
        # Footer.compose() yields nothing until the bindings arrive and then
        # recomposes; a version floating over an otherwise empty bar would
        # flicker on startup.
        if self._bindings_ready:
            yield VersionLabel(f"v{version_string()}")
