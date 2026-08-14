"""Tables for the partition monitor screen.

`PartitionTable` lists every partition with its node/CPU state and load;
`PartitionJobTable` lists the jobs on one partition across *all* users.
"""

from __future__ import annotations

from textual.message import Message
from textual.widgets import DataTable
from rich.text import Text

from lazyslurm.models import (
    NodeDevice,
    NodeInfo,
    PartitionInfo,
    PartitionJob,
    format_bytes,
    gres_devices,
)
from lazyslurm.widgets.job_table import _partition_style, _truncate
from lazyslurm.widgets.keyed_table import KeyedTable

# Load bar rendering
_BAR_WIDTH = 10
_BAR_FULL = "█"
_BAR_EMPTY = "░"

# States in which a node's counters say nothing useful: its CPUs are not
# allocatable and neither are its GPUs, however idle they look.
_UNAVAILABLE = ("down", "drained", "draining", "fail", "maint")

# One mark per device in the GPUs column, when gpu_column = "glyphs".
_DEVICE_BUSY = "▣"
_DEVICE_FREE = "▢"

# Past this many devices the marks are wider than the count they replace and
# start pushing Reason off the screen, so the count wins regardless.
_MAX_GLYPHS = 12

# Set from the app's config; see set_node_display().
_node_expand = "gpu"
_gpu_column = "count"


def set_node_display(node_expand: str = "gpu", gpu_column: str = "count") -> None:
    """Apply the config options that change how node rows read."""
    global _node_expand, _gpu_column
    _node_expand = node_expand
    _gpu_column = gpu_column


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


class PartitionTable(KeyedTable):
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
        self.refill((part.name, self._row_for(part)) for part in partitions)

    def get_partition(self, name: str) -> PartitionInfo | None:
        return next((p for p in self._partitions if p.name == name), None)

    def get_selected_partition(self) -> str | None:
        return self.selected_key()

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


_NODE_STATE_STYLES: dict[str, str] = {
    "idle": "green",
    "mixed": "yellow",
    "allocated": "dark_orange",
    "completing": "cyan",
    "reserved": "blue",
    "drained": "red dim",
    "draining": "red dim",
    "down": "red bold",
    "fail": "red bold",
    "failing": "red",
    "maint": "magenta dim",
    "unknown": "dim",
}


# A device row's key. "/" cannot occur in a Slurm node name, so the parent is
# always recoverable from the key -- which is what keeps the cursor, the jobs
# panel and NodeSelected working when the cursor sits on a GPU.
_DEVICE_SEP = "/gpu"


def device_key(node: str, index: int) -> str:
    return f"{node}{_DEVICE_SEP}{index}"


def node_of(key: str) -> str:
    """The node a row belongs to, whether it is a node row or a device row."""
    return key.split(_DEVICE_SEP)[0]


def device_of(key: str) -> int | None:
    """The device index of a device row, or None for a node row."""
    _, sep, index = key.partition(_DEVICE_SEP)
    return int(index) if sep and index.isdigit() else None


def device_marks(devices: list[NodeDevice], usable: bool = True) -> Text:
    """`▣▣▢▣▣▣▣▣` — one mark per device, green while any is free.

    A drained node is dim whatever its marks say: eight idle GPUs on a node
    nothing can be scheduled onto are not eight free GPUs, and painting them
    green invites exactly the wrong conclusion.
    """
    free = sum(1 for d in devices if not d.busy)
    marks = "".join(_DEVICE_BUSY if d.busy else _DEVICE_FREE for d in devices)
    if not usable:
        return Text(marks, style="dim")
    return Text(marks, style="green" if free else "red")


class NodeTable(KeyedTable):
    """Top panel of the node screen: one row per node of a partition.

    A node row folds open into one row per GPU (`Enter`), which is the only
    way to see *which* device is free -- "7/8" cannot say. That much is free:
    sinfo already reports the allocated indices. The owner and the live
    utilisation cost a round trip each and are filled in on demand.
    """

    COLUMNS = (
        "Node", "State", "CPUs A/I/O/T", "Load", "Memory", "GPUs", "Reason",
    )

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # NB: not `_nodes` — Textual's DOMNode uses that for its children.
        self._node_infos: list[NodeInfo] = []
        self._expanded: set[str] = set()
        # node -> its devices, once something has been resolved for them. The
        # free/busy state is re-read from sinfo on every poll; the columns that
        # cost a round trip (owner, utilisation) live here until they do.
        self._devices: dict[str, list[NodeDevice]] = {}

    def on_mount(self) -> None:
        for col in self.COLUMNS:
            self.add_column(col, key=col)
        self.cursor_type = "row"
        self.zebra_stripes = True

    # -- rows ----------------------------------------------------------------

    def update_nodes(self, nodes: list[NodeInfo]) -> None:
        """Rebuild, keeping the cursor on the same node."""
        self._node_infos = nodes
        names = {node.name for node in nodes}
        self._expanded &= names        # forget nodes that left the partition
        self._devices = {n: d for n, d in self._devices.items() if n in names}
        self.refill(self._rows())

    def _rows(self):
        for node in self._node_infos:
            yield node.name, self._row_for(node)
            if node.name not in self._expanded:
                continue
            devices = self.devices_of(node)
            for index, device in enumerate(devices):
                last = index == len(devices) - 1
                yield (
                    device_key(node.name, device.index),
                    self._device_row_for(device, "└ " if last else "├ "),
                )

    def devices_of(self, node: NodeInfo) -> list[NodeDevice]:
        """This node's GPUs, carrying anything already resolved for them.

        sinfo is the authority on which are busy -- it is re-read every poll --
        so the remembered ones only contribute the columns it cannot answer.
        """
        fresh = gres_devices(node.gres, node.gres_used)
        known = {d.index: d for d in self._devices.get(node.name, [])}
        for device in fresh:
            previous = known.get(device.index)
            if previous is None:
                continue
            device.job_id = previous.job_id
            device.user = previous.user
            device.name = previous.name
            device.cpus = previous.cpus
            device.util = previous.util
            device.mem_used = previous.mem_used
            device.mem_total = previous.mem_total
        return fresh

    def set_devices(self, node: str, devices: list[NodeDevice]) -> None:
        """Remember what `g` / `Shift+G` resolved, and redraw."""
        self._devices[node] = devices
        self.refill(self._rows())

    # -- expansion -----------------------------------------------------------

    def is_expandable(self, node: str) -> bool:
        info = self.get_node(node)
        return bool(info and _node_expand != "off" and gres_devices(info.gres, info.gres_used))

    def expand(self, node: str) -> None:
        """Open a node's devices, whether or not it was already open.

        The on-demand keys use this rather than toggle: having asked who holds
        the GPUs, being shown the collapsed row again would be perverse.
        """
        if self.is_expandable(node):
            self._expanded.add(node)
            self.refill(self._rows())

    def toggle_expand(self, node: str | None = None) -> bool:
        """Expand/collapse a node row. True if anything changed."""
        name = node if node is not None else self.get_selected_node()
        if not name or not self.is_expandable(name):
            return False
        self._expanded.symmetric_difference_update({name})
        self.refill(self._rows())
        return True

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a row: fold the node it belongs to."""
        key = str(event.row_key.value) if event.row_key else None
        if key:
            self.toggle_expand(node_of(key))

    # -- selection -----------------------------------------------------------

    def get_node(self, name: str) -> NodeInfo | None:
        return next((n for n in self._node_infos if n.name == name), None)

    def get_selected_node(self) -> str | None:
        """The node under the cursor -- the parent, when it is on a device."""
        key = self.selected_key()
        return node_of(key) if key else None

    def get_selected_device(self) -> int | None:
        """The device index under the cursor, or None on a node row."""
        return device_of(self.selected_key() or "")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        key = str(event.row_key.value) if event.row_key else self.selected_key()
        name = node_of(key) if key else None
        if name:
            self.post_message(NodeSelected(name))

    def _row_for(self, node: NodeInfo) -> tuple:
        style = _NODE_STATE_STYLES.get(node.base_state, "")
        state = Text(node.state, style=style)

        devices = gres_devices(node.gres, node.gres_used)
        usable = node.base_state not in _UNAVAILABLE
        if not node.gpus_total:
            gpus = Text("—", style="dim")
        elif _gpu_column == "glyphs" and 0 < len(devices) <= _MAX_GLYPHS:
            gpus = device_marks(devices, usable)
        else:
            gpus = Text(
                f"{node.gpus_used}/{node.gpus_total}",
                style=("green" if node.gpus_free else "red") if usable else "dim",
            )

        marker = "▾ " if node.name in self._expanded else (
            "▸ " if devices and _node_expand != "off" else "  "
        )

        # A drained node's counters say nothing useful; its reason does.
        if node.base_state in _UNAVAILABLE:
            load = Text("—", style="dim")
        else:
            load = load_bar(node.load, width=6)

        # Unknown is not "full": a node that has not reported its free memory
        # gets the same "—" the load column above already uses.
        used_mb = node.mem_used_mb
        if node.memory_mb and used_mb is not None:
            mem = Text.assemble(
                (f"{used_mb / 1024:5.0f}", "" if (node.mem_used or 0) < 0.9 else "red"),
                ("/", "dim"),
                f"{node.memory_mb / 1024:.0f}G",
            )
        elif node.memory_mb:
            mem = Text.assemble(
                ("    —", "dim"), ("/", "dim"), f"{node.memory_mb / 1024:.0f}G",
            )
        else:
            mem = Text("—", style="dim")

        return (
            Text.assemble(
                (marker, "dim"),
                (node.name, "bold" if node.unresponsive else ""),
            ),
            state,
            node.cpus_aiot,
            load,
            mem,
            gpus,
            Text(_truncate(node.reason, 28), style="red dim") if node.reason else "",
        )

    def _device_row_for(self, device: NodeDevice, indent: str) -> tuple:
        """A GPU under its node, borrowing the columns above by meaning.

        Identity stays identity, State stays state, Load is already a bar on
        the same 0-1 scale, Memory keeps its used/total shape. A free device
        fills in nothing else -- there is nothing else to say about it.
        """
        label = Text.assemble(("  " + indent, "dim"), f"GPU {device.index}")
        if not device.busy:
            return (
                label, Text("free", style="green"), "", "", "",
                self._power_cell(device), "",
            )

        util = load_bar(device.util, width=6) if device.util is not None else Text("")
        if device.mem_total:
            mem = Text.assemble(
                format_bytes(device.mem_used or 0), ("/", "dim"),
                format_bytes(device.mem_total),
            )
        else:
            mem = Text("")
        owner = " ".join(p for p in (device.job_id, device.user, device.name) if p)
        return (
            label,
            Text("busy", style="dark_orange"),
            device.cpus,
            util,
            mem,
            self._power_cell(device),
            _truncate(owner, 28),
        )

    @staticmethod
    def _power_cell(device: NodeDevice) -> Text:
        """The GPUs column on a device row: what it is drawing.

        The model goes here only until there is something better to say -- every
        device on a node is the same model, so repeating it down the block is
        the least informative thing the column could carry.
        """
        if device.power is None:
            return Text(device.model or "—", style="dim")
        if device.power_limit:
            ratio = device.power / device.power_limit
            style = "red" if ratio >= 0.9 else "yellow" if ratio >= 0.6 else "green"
            return Text.assemble(
                (f"{device.power:.0f}", style), ("/", "dim"), f"{device.power_limit:.0f}W",
            )
        return Text(f"{device.power:.0f}W")


class NodeSelected(Message):
    """Posted when the cursor moves to a different node."""

    def __init__(self, node: str) -> None:
        super().__init__()
        self.node = node


class PartitionJobTable(KeyedTable):
    """Right panel of the partition screen: all users' jobs on a partition."""

    COLUMNS = ("Job ID", "User", "Name", "State", "Time", "Limit", "N", "CPUs", "GRES", "Node/Reason")

    def __init__(self, *args, user: str = "", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.user = user
        self._jobs: list[PartitionJob] = []

    def jobs(self) -> list[PartitionJob]:
        """The jobs of the last refresh, for callers that need their ids."""
        return self._jobs

    def on_mount(self) -> None:
        for col in self.COLUMNS:
            self.add_column(col, key=col)
        self.cursor_type = "row"
        self.zebra_stripes = True

    def update_jobs(self, jobs: list[PartitionJob]) -> None:
        """Rebuild, keeping the cursor on the same job.

        This list is polled on the refresh timer, so without that it jumps
        back to the top every few seconds and a busy partition cannot be read
        at all.
        """
        self._jobs = jobs
        self.refill((job.job_id, self._row_for(job)) for job in jobs)

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
