"""Tables for the partition monitor screen.

`PartitionTable` lists every partition with its node/CPU state and load;
`PartitionJobTable` lists the jobs on one partition across *all* users.
"""

from __future__ import annotations

from textual.message import Message
from textual.widgets import DataTable
from rich.text import Text

from lazyslurm.models import PartitionInfo, PartitionJob
from lazyslurm.widgets.job_table import _partition_style, _truncate

# Load bar rendering
_BAR_WIDTH = 10
_BAR_FULL = "█"
_BAR_EMPTY = "░"


def load_bar(fraction: float, width: int = _BAR_WIDTH) -> Text:
    """Render a 0.0-1.0 fraction as a colored bar with a percentage."""
    fraction = min(max(fraction, 0.0), 1.0)
    filled = round(fraction * width)
    if fraction >= 0.9:
        style = "red"
    elif fraction >= 0.6:
        style = "yellow"
    else:
        style = "green"
    bar = _BAR_FULL * filled + _BAR_EMPTY * (width - filled)
    return Text.assemble((bar, style), f" {fraction * 100:3.0f}%")


class PartitionSelected(Message):
    """Posted when the cursor moves to a different partition."""

    def __init__(self, partition: str) -> None:
        super().__init__()
        self.partition = partition


class PartitionTable(DataTable):
    """Left panel of the partition screen: one row per partition."""

    COLUMNS = (
        "Partition", "Load", "Nodes A/I/O/T", "CPUs A/I/O/T",
        "Run", "Pend", "Limit", "GRES",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._partitions: list[PartitionInfo] = []

    def on_mount(self) -> None:
        for col in self.COLUMNS:
            self.add_column(col, key=col)
        self.cursor_type = "row"
        self.zebra_stripes = True

    def update_partitions(self, partitions: list[PartitionInfo]) -> None:
        """Rebuild the table, keeping the cursor on the same partition."""
        self._partitions = partitions
        selected = self.get_selected_partition()
        self.clear()
        for part in partitions:
            self.add_row(*self._row_for(part), key=part.name)
        if selected:
            for index, part in enumerate(partitions):
                if part.name == selected:
                    self.move_cursor(row=index)
                    break

    def get_partition(self, name: str) -> PartitionInfo | None:
        return next((p for p in self._partitions if p.name == name), None)

    def get_selected_partition(self) -> str | None:
        if self.row_count == 0:
            return None
        try:
            row_key, _ = self.coordinate_to_cell_key(self.cursor_coordinate)
            return str(row_key.value)
        except Exception:
            return None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        name = str(event.row_key.value) if event.row_key else self.get_selected_partition()
        if name:
            self.post_message(PartitionSelected(name))

    def _row_for(self, part: PartitionInfo) -> tuple:
        down = part.avail != "up"
        name = Text(part.name, style="dim strike" if down else _partition_style(part.name))
        return (
            name,
            Text("[down]", style="dim") if down else load_bar(part.load),
            part.nodes_aiot,
            part.cpus_aiot,
            Text(str(part.running), style="green" if part.running else "dim"),
            Text(str(part.pending), style="yellow" if part.pending else "dim"),
            part.time_limit,
            _truncate(part.gres, 24),
        )


class PartitionJobTable(DataTable):
    """Right panel of the partition screen: all users' jobs on a partition."""

    COLUMNS = ("Job ID", "User", "Name", "State", "Time", "Limit", "N", "CPUs", "GRES", "Node/Reason")

    def __init__(self, *args, user: str = "", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.user = user
        self._jobs: list[PartitionJob] = []

    def on_mount(self) -> None:
        for col in self.COLUMNS:
            self.add_column(col, key=col)
        self.cursor_type = "row"
        self.zebra_stripes = True

    def update_jobs(self, jobs: list[PartitionJob]) -> None:
        self._jobs = jobs
        self.clear()
        for job in jobs:
            self.add_row(*self._row_for(job), key=job.job_id)

    def _row_for(self, job: PartitionJob) -> tuple:
        mine = self.user and job.user == self.user
        id_style = "bold cyan" if mine else ""
        state_style = "green" if job.state == "RUNNING" else "yellow"
        return (
            Text(("▸ " if mine else "") + job.job_id, style=id_style),
            Text(job.user, style="bold" if mine else "dim"),
            _truncate(job.name, 18),
            Text(job.state, style=state_style),
            job.elapsed,
            job.time_limit,
            job.nodes,
            job.cpus,
            _truncate(job.gres, 16),
            _truncate(job.nodelist, 22),
        )
