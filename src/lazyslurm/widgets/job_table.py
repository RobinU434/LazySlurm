"""DataTable-based widget for displaying Slurm job lists."""

from __future__ import annotations

import shlex
from typing import NamedTuple

from textual.message import Message
from textual.widgets import DataTable
from rich.text import Text

from lazyslurm.config import base_job_id
from lazyslurm.models import (
    CompletedJob,
    RunningJob,
    array_index_span,
    array_task_count,
    gres_count,
)

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
_collapse_arrays: bool = True

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
    collapse_arrays: bool = True,
) -> None:
    """Set column width, abbreviation and array-collapsing settings."""
    global _max_name_width, _max_partition_width, _abbreviate_states
    global _collapse_arrays
    _max_name_width = max_name
    _max_partition_width = max_partition
    _abbreviate_states = abbreviate
    _collapse_arrays = collapse_arrays


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


# Search terms: `state:pending`, `part:gpu`, `gpu:>0`, or a bare word. Aliases keep
# the short forms people actually type working.
_QUERY_KEYS: dict[str, str] = {
    "state": "state", "st": "state", "s": "state",
    "part": "partition", "partition": "partition", "p": "partition",
    "name": "name", "n": "name",
    "id": "id", "job": "id",
    "gpu": "gpu", "gpus": "gpu", "gres": "gpu",
}

_COMPARISONS: tuple[str, ...] = (">=", "<=", "!=", ">", "<", "=")

# Row key for the "nothing matched" line; never a real job id.
_PLACEHOLDER_KEY = "__no_match__"


class Term(NamedTuple):
    """One parsed search term. `field` is None for a bare substring."""

    field: str | None
    op: str
    value: str


def parse_query(text: str) -> list[Term]:
    """Split a search string into terms, to be ANDed together.

    ``train state:run gpu:>0`` becomes a substring term plus two field terms.
    A `key:` prefix that is not a known field stays a plain substring, so
    nothing a user types can break the filter — ``foo:bar`` just searches for
    "foo:bar".
    """
    try:
        tokens = shlex.split(text)
    except ValueError:  # unbalanced quote while still typing
        tokens = text.split()

    terms: list[Term] = []
    for token in tokens:
        if not token:
            continue
        key, sep, value = token.partition(":")
        field = _QUERY_KEYS.get(key.lower()) if sep else None
        if field is None:
            terms.append(Term(None, "~", token.lower()))
            continue
        op = "~"
        for candidate in _COMPARISONS:
            if value.startswith(candidate):
                op, value = candidate, value[len(candidate):]
                break
        terms.append(Term(field, op, value.strip().lower()))
    return terms


def _compare(actual: float, op: str, expected: float) -> bool:
    if op == ">":
        return actual > expected
    if op == ">=":
        return actual >= expected
    if op == "<":
        return actual < expected
    if op == "<=":
        return actual <= expected
    if op == "!=":
        return actual != expected
    return actual == expected


def elapsed_seconds(text: str) -> int:
    """Parse Slurm's elapsed format: ``[DD-]HH:MM:SS``, ``MM:SS``, ``N/A``."""
    text = (text or "").strip()
    if not text or text in ("N/A", "Unknown", "INVALID"):
        return 0
    days = 0
    if "-" in text:
        head, _, text = text.partition("-")
        days = int(head) if head.isdigit() else 0
    parts = text.split(":")
    try:
        values = [int(p) for p in parts]
    except ValueError:
        return 0
    while len(values) < 3:
        values.insert(0, 0)  # MM:SS -> 0:MM:SS
    hours, minutes, seconds = values[-3:]
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def group_jobs(jobs: list) -> list[tuple[str, list]]:
    """Group array tasks by their base job id, keeping the incoming order.

    Returns ``[(base_id, members)]``. Non-array jobs come back as their own
    single-member group, so callers can treat everything uniformly.
    """
    order: list[str] = []
    groups: dict[str, list] = {}
    for job in jobs:
        base = base_job_id(job.job_id) or job.job_id
        if base not in groups:
            groups[base] = []
            order.append(base)
        groups[base].append(job)
    return [(base, groups[base]) for base in order]


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
        old_selected = table._selected_row_key()
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
    TITLE: str = ""
    SHOW_CURSOR_INITIAL: bool = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._all_jobs: list = []
        self._filter_text: str = ""
        self._bookmarked: set[str] = set()
        self._multiselected: set[str] = set()
        self._force_next: bool = False
        # Array handling: base ids the user has opened, and the members behind
        # each collapsed row. Both survive polls, so a refresh never folds a
        # group the user expanded.
        self._expanded: set[str] = set()
        self._groups: dict[str, list] = {}
        self._terms: list[Term] = []

    def on_mount(self) -> None:
        for col in self.COLUMNS:
            self.add_column(col, key=col)
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.show_cursor = self.SHOW_CURSOR_INITIAL
        self.border_title = self.TITLE

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
        self._filter_text = text.strip()
        self._terms = parse_query(self._filter_text)
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
        return [key for key in _row_keys(self) if key != _PLACEHOLDER_KEY]

    def get_job(self, job_id: str):
        """Return the job dataclass for `job_id` from the last poll, or None."""
        return next((j for j in self._all_jobs if j.job_id == job_id), None)

    def _selected_row_key(self) -> str | None:
        if self.row_count == 0:
            return None
        try:
            row_key, _ = self.coordinate_to_cell_key(self.cursor_coordinate)
        except Exception:
            return None
        key = str(row_key.value)
        return None if key == _PLACEHOLDER_KEY else key

    def get_selected_job_id(self) -> str | None:
        """The job id the detail panels should show.

        For a collapsed array that is its first task — the group's own key is a
        base id, which is not a job Slurm can describe on its own.
        """
        key = self._selected_row_key()
        if key is None:
            return None
        members = self._groups.get(key)
        return members[0].job_id if members else key

    def selected_group(self) -> tuple[str, list] | None:
        """(base id, members) when a collapsed array row is selected."""
        key = self._selected_row_key()
        members = self._groups.get(key) if key else None
        return (key, members) if members else None

    def expand_ids(self, ids) -> list[str]:
        """Replace any collapsed-array row keys with the ids of their members."""
        out: list[str] = []
        for value in ids:
            members = self._groups.get(value)
            out.extend(m.job_id for m in members) if members else out.append(value)
        return out

    def toggle_expand(self, base: str | None = None) -> bool:
        """Expand/collapse an array row. Returns True if anything changed."""
        key = base if base is not None else self._selected_row_key()
        if key is None or key not in self._groups:
            return False
        self._expanded.symmetric_difference_update({key})
        self._rebuild()
        return True

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row: DataTable claims the key for this message."""
        key = str(event.row_key.value) if event.row_key else None
        if key in self._groups:
            self.toggle_expand(key)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = str(event.row_key.value) if event.row_key else None
        members = self._groups.get(key) if key else None
        job_id = members[0].job_id if members else (key or self.get_selected_job_id())
        if job_id:
            self.post_message(JobSelected(job_id, self.SOURCE))

    def _markers(self, job_id: str) -> str:
        sel = "◉ " if job_id in self._multiselected else ""
        bm = "★ " if job_id in self._bookmarked else ""
        return sel + bm

    def _rebuild(self) -> None:
        """Rebuild from _all_jobs: filter, group arrays, pin bookmarks to top."""
        filtered = self._all_jobs
        if self._terms:
            filtered = [j for j in filtered if self._filter_match(j, self._terms)]

        if _collapse_arrays:
            groups = group_jobs(filtered)
        else:
            groups = [(job.job_id, [job]) for job in filtered]

        # A single row is an ordinary job — including a pending array that
        # squeue already reports as one `123_[12-40]` row — and renders as before.
        self._groups = {b: m for b, m in groups if len(m) > 1}
        self._expanded &= set(self._groups)  # forget arrays that finished

        def pinned(base: str, members: list) -> bool:
            return base in self._bookmarked or any(
                m.job_id in self._bookmarked for m in members
            )

        ordered = ([g for g in groups if pinned(*g)]
                   + [g for g in groups if not pinned(*g)])

        new_data: dict[str, tuple] = {}
        for base, members in ordered:
            if base not in self._groups:
                job = members[0]
                new_data[job.job_id] = self._row_for(job)
                continue
            expanded = base in self._expanded
            new_data[base] = self._group_row_for(base, members, expanded)
            if expanded:
                for index, job in enumerate(members):
                    last = index == len(members) - 1
                    new_data[job.job_id] = self._row_for(
                        job, indent="└ " if last else "├ "
                    )

        if not new_data and self._terms:
            # An empty table with an active filter looks like a bug; say so.
            new_data[_PLACEHOLDER_KEY] = self._placeholder_row()

        self._update_title(len(filtered))

        force = self._force_next
        self._force_next = False
        _apply_diff(self, new_data, force=force)

    def _update_title(self, matched: int) -> None:
        """Border title carries match count when a filter is active."""
        if not self._terms:
            self.border_title = self.TITLE
            return
        self.border_title = f"{self.TITLE} — {matched}/{len(self._all_jobs)} match"

    def _placeholder_row(self) -> tuple:
        cells = [Text("no jobs match", style="dim")]
        cells += [""] * (len(self.COLUMNS) - 1)
        return tuple(cells)

    def _filter_match(self, job, terms: list[Term]) -> bool:
        """Every term must match (AND). Bare terms search the whole row."""
        return all(self._match_term(job, term) for term in terms)

    def _match_term(self, job, term: Term) -> bool:
        if term.field is None:
            return any(term.value in field.lower() for field in self._search_fields(job))
        if term.field == "state":
            # Prefix match, so "fail" finds FAILED and "pend" finds PENDING.
            return getattr(job, "state", "").lower().startswith(term.value)
        if term.field == "partition":
            return term.value in getattr(job, "partition", "").lower()
        if term.field == "name":
            return term.value in getattr(job, "name", "").lower()
        if term.field == "id":
            return term.value in job.job_id.lower()
        if term.field == "gpu":
            # Only squeue rows carry GRES; sacct rows have none, so a gpu:
            # filter simply matches nothing in the terminated table.
            gres = getattr(job, "gres", None)
            if gres is None:
                return False
            try:
                wanted = float(term.value) if term.value else 0.0
            except ValueError:
                return False
            return _compare(float(gres_count(gres)), term.op, wanted)
        return False

    def _group_label(self, base: str, members: list, expanded: bool) -> Text:
        """`▸ 123_[0-11] ×12` — the collapsed row's Job ID cell."""
        tasks = sum(array_task_count(m.job_id) for m in members)
        bounds = array_index_span(m.job_id for m in members)
        span = f"[{bounds[0]}-{bounds[1]}]" if bounds else "[]"
        arrow = "▾" if expanded else "▸"
        return Text.assemble(
            (f"{arrow} ", "dim"), f"{base}_{span}", (f" ×{tasks}", "dim"),
        )

    @staticmethod
    def _tally(counts: dict[str, int], styles: dict[str, str]) -> Text:
        """Render `{state: n}` as a compact coloured summary."""
        text = Text()
        for state, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            if text.plain:
                text.append(" ")
            text.append(f"{count}", style=styles.get(state, ""))
            text.append(_STATE_ABBREV.get(state, state)[:4].lower(), style="dim")
        return text

    # --- subclass hooks ---
    def _search_fields(self, job) -> tuple[str, ...]:
        """Fields a bare (non `key:`) search term looks through."""
        raise NotImplementedError

    def _row_for(self, job, indent: str = "") -> tuple:
        raise NotImplementedError

    def _group_row_for(self, base: str, members: list, expanded: bool) -> tuple:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Active Jobs Table
# ---------------------------------------------------------------------------


class ActiveJobTable(_BaseJobTable):
    """Upper-left panel: currently running / pending jobs."""

    COLUMNS = ("Job ID", "Name", "Elapsed", "Partition")
    SOURCE = "active"
    TITLE = "Active Jobs"

    def _search_fields(self, job: RunningJob) -> tuple[str, ...]:
        return (job.job_id, job.name, job.partition)

    def _row_for(self, job: RunningJob, indent: str = "") -> tuple:
        name = _truncate(
            f"{indent}{self._markers(job.job_id)}{job.name}", _max_name_width
        )
        id_text = Text(job.job_id, style=_ACTIVE_STATE_STYLES.get(job.state, ""))
        part_text = Text(
            _truncate(job.partition, _max_partition_width),
            style=_partition_style(job.partition),
        )
        return (id_text, name, job.elapsed, part_text)

    def _group_row_for(self, base: str, members: list, expanded: bool) -> tuple:
        """Collapsed array: id range, shared name, state tally, partition.

        The tally replaces Elapsed, which means little for a dozen tasks that
        started at different times.
        """
        counts: dict[str, int] = {}
        for job in members:
            counts[job.state] = counts.get(job.state, 0) + array_task_count(job.job_id)
        first = members[0]
        name = _truncate(f"{self._markers(base)}{first.name}", _max_name_width)
        part_text = Text(
            _truncate(first.partition, _max_partition_width),
            style=_partition_style(first.partition),
        )
        return (
            self._group_label(base, members, expanded),
            name,
            self._tally(counts, _ACTIVE_STATE_STYLES),
            part_text,
        )


# ---------------------------------------------------------------------------
# Completed Jobs Table
# ---------------------------------------------------------------------------


class CompletedJobTable(_BaseJobTable):
    """Lower-left panel: past completed / failed / cancelled jobs."""

    COLUMNS = ("Job ID", "Name", "State", "Partition", "Elapsed")
    SOURCE = "completed"
    TITLE = "Terminated Jobs"
    SHOW_CURSOR_INITIAL = False

    def _search_fields(self, job: CompletedJob) -> tuple[str, ...]:
        return (job.job_id, job.name, job.partition, job.state)

    def _row_for(self, job: CompletedJob, indent: str = "") -> tuple:
        name = _truncate(
            f"{indent}{self._markers(job.job_id)}{job.name}", _max_name_width
        )
        part_text = Text(
            _truncate(job.partition, _max_partition_width),
            style=_partition_style(job.partition),
        )
        return (job.job_id, name, _styled_state(job.state), part_text, job.elapsed)

    def _group_row_for(self, base: str, members: list, expanded: bool) -> tuple:
        """Collapsed array: id range, shared name, state tally, longest run."""
        counts: dict[str, int] = {}
        for job in members:
            state = job.state.split(" ")[0]
            counts[state] = counts.get(state, 0) + array_task_count(job.job_id)
        first = members[0]
        name = _truncate(f"{self._markers(base)}{first.name}", _max_name_width)
        part_text = Text(
            _truncate(first.partition, _max_partition_width),
            style=_partition_style(first.partition),
        )
        longest = max(members, key=lambda j: elapsed_seconds(j.elapsed)).elapsed
        return (
            self._group_label(base, members, expanded),
            name,
            self._tally(counts, _STATE_STYLES),
            part_text,
            longest,
        )
