"""A DataTable whose cursor survives a rebuild.

Every table in the full-screen panels is refreshed by clearing it and adding
its rows again, and ``clear()`` puts the cursor back on row 0. On a table that
is polled on a timer that makes the panel unbrowsable: each refresh undoes the
scroll before the user has finished reading. Remembering the row *key* — not
the index, which moves as jobs come and go — and restoring it afterwards is
what keeps them usable.
"""

from __future__ import annotations

from typing import Iterable

from textual.widgets import DataTable
from textual.widgets.data_table import CellDoesNotExist, RowDoesNotExist


class KeyedTable(DataTable):
    """DataTable that tracks its cursor by row key across a full rebuild."""

    def selected_key(self) -> str | None:
        """The row key under the cursor, or None if there is no usable row."""
        if self.row_count == 0:
            return None
        try:
            row_key, _ = self.coordinate_to_cell_key(self.cursor_coordinate)
        except CellDoesNotExist:
            return None  # cursor is outside the table (empty or mid-rebuild)
        return str(row_key.value)

    def restore_cursor(self, key: str | None) -> bool:
        """Move the cursor back to ``key``. False when that row is now gone.

        A job that ended between two refreshes takes its row with it; there is
        nowhere to put the cursor back, so it stays where clear() left it.
        """
        if not key:
            return False
        try:
            self.move_cursor(row=self.get_row_index(key))
        except RowDoesNotExist:
            return False
        return True

    def refill(self, rows: Iterable[tuple[str, tuple]]) -> None:
        """Replace every row with ``(key, cells)``, keeping the cursor put."""
        selected = self.selected_key()
        self.clear()
        for key, cells in rows:
            self.add_row(*cells, key=key)
        self.restore_cursor(selected)
