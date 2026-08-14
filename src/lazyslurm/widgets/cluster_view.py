"""The table behind the cluster browser.

One row per cluster this install has connected to, carrying what is worth
knowing before committing to opening one: who you are there, when you last
looked, and whether a connection to it is still alive.
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.message import Message
from textual.widgets import DataTable

from lazyslurm.widgets.keyed_table import KeyedTable

__all__ = ["ClusterTable", "ClusterChosen", "format_last_seen"]


# How a session reads in the table. "Detached" is deliberately its own state
# rather than being folded into "not connected": the difference is whether
# coming back costs a fresh login, which on a 2FA cluster is the whole point.
ATTACHED = "attached"
DETACHED = "connected (detached)"
CLOSED = "not connected"

_SESSION_STYLES = {ATTACHED: "green", DETACHED: "yellow", CLOSED: "dim"}


def format_last_seen(when: float, now: float | None = None) -> str:
    """`2 minutes ago`, `yesterday 17:40`, `3 weeks ago`, `never`."""
    if not when:
        return "never"
    now = time.time() if now is None else now
    seconds = max(0.0, now - when)
    if seconds < 90:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)} minutes ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)} hour{'' if int(hours) == 1 else 's'} ago"
    days = hours / 24
    if days < 2:
        return time.strftime("yesterday %H:%M", time.localtime(when))
    if days < 14:
        return f"{int(days)} days ago"
    weeks = int(days / 7)
    if weeks < 9:
        return f"{weeks} weeks ago"
    return time.strftime("%Y-%m-%d", time.localtime(when))


class ClusterChosen(Message):
    """Posted when a cluster row is activated."""

    def __init__(self, host: str) -> None:
        super().__init__()
        self.host = host


class ClusterTable(KeyedTable):
    """Every cluster connected to before, and the state of its session."""

    COLUMNS = ("Cluster", "SSH target", "User", "Last seen", "Session")

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._entries: list[dict] = []

    def on_mount(self) -> None:
        for col in self.COLUMNS:
            self.add_column(col, key=col)
        self.cursor_type = "row"
        self.zebra_stripes = True

    def update_clusters(
        self, entries: list[dict], attached: str = "", open_hosts: tuple[str, ...] = (),
    ) -> None:
        self._entries = entries
        self.refill(
            (entry["host"], self._row_for(entry, attached, open_hosts))
            for entry in entries
        )

    def get_entry(self, host: str) -> dict | None:
        return next((e for e in self._entries if e.get("host") == host), None)

    def selected_host(self) -> str | None:
        return self.selected_key()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.row_key and event.row_key.value:
            self.post_message(ClusterChosen(str(event.row_key.value)))

    @staticmethod
    def session_state(host: str, attached: str, open_hosts) -> str:
        if host == attached:
            return ATTACHED
        return DETACHED if host in open_hosts else CLOSED

    def _row_for(self, entry: dict, attached: str, open_hosts) -> tuple:
        host = entry.get("host", "")
        state = self.session_state(host, attached, open_hosts)
        marker = "●" if state != CLOSED else "○"
        return (
            Text(entry.get("cluster") or "—",
                 style="bold" if state == ATTACHED else ""),
            host,
            entry.get("user") or "—",
            format_last_seen(float(entry.get("last_seen") or 0)),
            Text(f"{marker} {state}", style=_SESSION_STYLES[state]),
        )
