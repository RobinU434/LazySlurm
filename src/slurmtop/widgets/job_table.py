"""DataTable-based widget for displaying Slurm job lists."""

from __future__ import annotations

from textual.message import Message
from textual.widgets import DataTable
from rich.text import Text

from slurmtop.models import CompletedJob, RunningJob

# Color mapping for terminated job states
_STATE_STYLES: dict[str, str] = {
    "COMPLETED": "green",
    "FAILED": "red bold",
    "TIMEOUT": "yellow",
    "CANCELLED": "dim",
    "CANCELLED+": "dim",
    "OUT_OF_MEMORY": "red",
    "NODE_FAIL": "red",
    "PREEMPTED": "yellow dim",
}

# Deterministic partition colors
# Color mapping for active job states (applied to Job ID column)
_ACTIVE_STATE_STYLES: dict[str, str] = {
    "RUNNING": "green",
    "PENDING": "yellow",
    "COMPLETING": "dark_orange",
    "REQUEUED": "yellow dim",
    "SUSPENDED": "dim",
    "PREEMPTED": "yellow dim",
}

_PARTITION_COLORS = [
    "cyan", "magenta", "yellow", "green", "blue",
    "red", "bright_cyan", "bright_magenta", "bright_green",
]

# Module-level display settings, set via set_display_config()
_custom_partition_colors: dict[str, str] = {}
_max_name_width: int = 16
_max_partition_width: int = 16
_abbreviate_states: bool = False

# State abbreviations
_STATE_ABBREV: dict[str, str] = {
    "COMPLETED": "COMP",
    "FAILED": "FAIL",
    "TIMEOUT": "TIME",
    "CANCELLED": "CAN",
    "CANCELLED+": "CAN+",
    "OUT_OF_MEMORY": "OOM",
    "NODE_FAIL": "NFAIL",
    "PREEMPTED": "PREEMPT",
    "RUNNING": "RUN",
    "PENDING": "PEND",
    "COMPLETING": "CG",
    "SUSPENDED": "SUSP",
    "REQUEUED": "REQ",
}


def set_partition_colors(colors: dict[str, str] | None) -> None:
    """Set custom partition→color mapping (from config file)."""
    global _custom_partition_colors
    _custom_partition_colors = colors or {}


def set_display_config(
    max_name: int = 16,
    max_partition: int = 16,
    abbreviate: bool = False,
) -> None:
    """Set column width and abbreviation settings."""
    global _max_name_width, _max_partition_width, _abbreviate_states
    _max_name_width = max_name
    _max_partition_width = max_partition
    _abbreviate_states = abbreviate


def _truncate(text: str, max_width: int) -> str:
    """Truncate text to max_width, adding … if truncated."""
    if max_width <= 0 or len(text) <= max_width:
        return text
    return text[:max_width - 1] + "…"


def _partition_style(partition: str) -> str:
    if not partition:
        return ""
    if partition in _custom_partition_colors:
        return _custom_partition_colors[partition]
    return _PARTITION_COLORS[sum(ord(c) for c in partition) % len(_PARTITION_COLORS)]


def _styled_state(state: str) -> Text:
    """Return a Rich Text object with color based on job state."""
    base_state = state.split(" ")[0] if " " in state else state
    style = _STATE_STYLES.get(base_state, "")
    display = _STATE_ABBREV.get(base_state, state) if _abbreviate_states else state
    return Text(display, style=style)


def _row_keys(table: DataTable) -> list[str]:
    """Return the current row keys (job IDs) in display order."""
    order: list[str] = []
    for i in range(table.row_count):
        try:
            row_key, _ = table.coordinate_to_cell_key(
                table.cursor_coordinate._replace(row=i, column=0)
            )
            order.append(str(row_key.value))
        except Exception:
            break
    return order


def _apply_diff(table: DataTable, new_data: dict[str, tuple], force: bool = False) -> None:
    """Apply a diff to a DataTable, preserving scroll when only cells change."""
    existing_order = _row_keys(table)
    existing_keys = set(existing_order)
    new_order = list(new_data.keys())
    new_keys = set(new_order)

    # Full rebuild when rows are added/removed, reordered (e.g. a bookmark was
    # pinned to the top), or a rebuild was forced (display settings changed).
    # Same-key same-order polls take the cheap in-place path below.
    if existing_keys != new_keys or existing_order != new_order or force:
        old_selected = table.get_selected_job_id()
        table.clear(columns=force)  # clear columns too on force to reset widths
        if force:
            for col in table.COLUMNS:
                table.add_column(col, key=col)
        for key, values in new_data.items():
            table.add_row(*values, key=key)
        if old_selected and old_selected in new_keys:
            try:
                idx = table.get_row_index(old_selected)
                table.move_cursor(row=idx)
            except Exception:
                pass
        return

    # No structural changes — just update changed cells in place
    for key, values in new_data.items():
        for col_key, value in zip(table.COLUMNS, values):
            try:
                current = table.get_cell(key, col_key)
                # Compare repr to catch style changes (e.g. yellow→green)
                # str() only compares text content, not Rich styles
                if repr(current) != repr(value):
                    table.update_cell(key, col_key, value)
            except Exception:
                pass


class JobSelected(Message):
    """Posted when the user moves the cursor to a different job."""

    def __init__(self, job_id: str, source_table: str) -> None:
        super().__init__()
        self.job_id = job_id
        self.source_table = source_table


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class _BaseJobTable(DataTable):
    """Common behavior for the active and completed job tables.

    Subclasses provide `COLUMNS`, `SOURCE` (the JobSelected tag), whether the
    cursor is shown before first focus, and two row-shaping hooks:
    `_filter_match(job, text)` and `_row_for(job)`.
    """

    COLUMNS: tuple[str, ...] = ()
    SOURCE: str = ""
    SHOW_CURSOR_INITIAL: bool = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._all_jobs: list = []
        self._filter_text: str = ""
        self._bookmarked: set[str] = set()
        self._multiselected: set[str] = set()
        self._force_next: bool = False

    def on_mount(self) -> None:
        for col in self.COLUMNS:
            self.add_column(col, key=col)
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.show_cursor = self.SHOW_CURSOR_INITIAL

    def watch_has_focus(self, focused: bool) -> None:
        self.show_cursor = focused
        if focused:
            job_id = self.get_selected_job_id()
            if job_id:
                self.post_message(JobSelected(job_id, self.SOURCE))

    def update_jobs(self, jobs: list) -> None:
        self._all_jobs = jobs
        self._rebuild()

    def apply_filter(self, text: str) -> None:
        self._filter_text = text.lower()
        self._rebuild()

    def set_bookmarks(self, ids: set[str]) -> None:
        self._bookmarked = ids
        self._rebuild()

    def set_multiselected(self, ids: set[str]) -> None:
        self._multiselected = ids
        self._rebuild()

    def force_rebuild(self) -> None:
        """Force a full table rebuild (e.g. after display settings change)."""
        self._force_next = True
        self._rebuild()

    def get_row_order(self) -> list[str]:
        """Return current row order (job IDs) as they appear in the table."""
        return _row_keys(self)

    def get_selected_job_id(self) -> str | None:
        if self.row_count == 0:
            return None
        try:
            row_key, _ = self.coordinate_to_cell_key(self.cursor_coordinate)
            return str(row_key.value)
        except Exception:
            return None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        job_id = str(event.row_key.value) if event.row_key else self.get_selected_job_id()
        if job_id:
            self.post_message(JobSelected(job_id, self.SOURCE))

    def _markers(self, job_id: str) -> str:
        sel = "◉ " if job_id in self._multiselected else ""
        bm = "★ " if job_id in self._bookmarked else ""
        return sel + bm

    def _rebuild(self) -> None:
        """Rebuild from _all_jobs, applying filter and pinning bookmarks to top."""
        filtered = self._all_jobs
        if self._filter_text:
            filtered = [j for j in filtered if self._filter_match(j, self._filter_text)]

        bookmarked = [j for j in filtered if j.job_id in self._bookmarked]
        rest = [j for j in filtered if j.job_id not in self._bookmarked]

        new_data = {job.job_id: self._row_for(job) for job in bookmarked + rest}

        force = self._force_next
        self._force_next = False
        _apply_diff(self, new_data, force=force)

    # --- subclass hooks ---
    def _filter_match(self, job, text: str) -> bool:
        raise NotImplementedError

    def _row_for(self, job) -> tuple:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Active Jobs Table
# ---------------------------------------------------------------------------


class ActiveJobTable(_BaseJobTable):
    """Upper-left panel: currently running / pending jobs."""

    COLUMNS = ("Job ID", "Name", "Elapsed", "Partition")
    SOURCE = "active"

    def _filter_match(self, job: RunningJob, text: str) -> bool:
        return (
            text in job.job_id.lower()
            or text in job.name.lower()
            or text in job.partition.lower()
        )

    def _row_for(self, job: RunningJob) -> tuple:
        name = _truncate(f"{self._markers(job.job_id)}{job.name}", _max_name_width)
        id_text = Text(job.job_id, style=_ACTIVE_STATE_STYLES.get(job.state, ""))
        part_text = Text(
            _truncate(job.partition, _max_partition_width),
            style=_partition_style(job.partition),
        )
        return (id_text, name, job.elapsed, part_text)


# ---------------------------------------------------------------------------
# Completed Jobs Table
# ---------------------------------------------------------------------------


class CompletedJobTable(_BaseJobTable):
    """Lower-left panel: past completed / failed / cancelled jobs."""

    COLUMNS = ("Job ID", "Name", "State", "Partition", "Elapsed")
    SOURCE = "completed"
    SHOW_CURSOR_INITIAL = False

    def _filter_match(self, job: CompletedJob, text: str) -> bool:
        return (
            text in job.job_id.lower()
            or text in job.name.lower()
            or text in job.partition.lower()
            or text in job.state.lower()
        )

    def _row_for(self, job: CompletedJob) -> tuple:
        name = _truncate(f"{self._markers(job.job_id)}{job.name}", _max_name_width)
        part_text = Text(
            _truncate(job.partition, _max_partition_width),
            style=_partition_style(job.partition),
        )
        return (job.job_id, name, _styled_state(job.state), part_text, job.elapsed)
