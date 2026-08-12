"""Table for the account usage screen: who in the account burned what."""

from __future__ import annotations

from textual.widgets import DataTable
from rich.text import Text

from lazyslurm.models import UsageRow

_BAR_WIDTH = 16


def share_bar(fraction: float, width: int = _BAR_WIDTH) -> str:
    """`████████░░░░░░░░` — one consumer's slice of the account's hours."""
    fraction = min(max(fraction, 0.0), 1.0)
    filled = round(fraction * width)
    if fraction > 0:
        filled = max(1, filled)
    return "█" * filled + "░" * (width - filled)


def format_hours(hours: float) -> str:
    """Hours as `2 472` / `68 364` / `912.5` — thin spaces beat unreadable digits."""
    if hours >= 1000:
        return f"{hours:,.0f}".replace(",", " ")
    if hours >= 10:
        return f"{hours:.0f}"
    return f"{hours:.1f}"


class UsageTable(DataTable):
    """One row per user in the account, biggest consumer first."""

    COLUMNS = ("User", "Name", "CPU hours", "Share", "%")

    def __init__(self, *args, user: str = "", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.user = user
        self._rows: list[UsageRow] = []

    def on_mount(self) -> None:
        for col in self.COLUMNS:
            self.add_column(col, key=col)
        self.cursor_type = "row"
        self.zebra_stripes = True

    def update_rows(self, rows: list[UsageRow]) -> None:
        """Show the per-user rows; the account total is the denominator."""
        self._rows = rows
        users = [r for r in rows if not r.is_account_total]
        total = sum(r.hours for r in users) or 1.0
        self.clear()
        for row in users:
            share = row.hours / total
            mine = self.user and row.user == self.user
            self.add_row(
                Text(("▸ " if mine else "") + row.user,
                     style="bold cyan" if mine else ""),
                Text(row.name or "", style="" if mine else "dim"),
                format_hours(row.hours),
                Text(share_bar(share), style="cyan" if mine else "dim"),
                f"{share * 100:4.1f}%",
                key=row.user or row.account,
            )

    @property
    def total_hours(self) -> float:
        return sum(r.hours for r in self._rows if not r.is_account_total)

    @property
    def my_hours(self) -> float:
        return sum(r.hours for r in self._rows
                   if not r.is_account_total and r.user == self.user)
