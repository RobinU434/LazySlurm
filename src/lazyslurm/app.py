"""Main LazySlurm Textual application."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Input, RichLog, Static

from lazyslurm import help as help_topics
from lazyslurm import slurm
from lazyslurm.models import (
    Config,
    NodeSample,
    PriorityInfo,
    RESOURCE_MONITOR_MODES,
    parse_duration,
)
from lazyslurm.widgets.detail_view import (
    DetailView,
    parse_mem_bytes,
    render_cpu_monitor,
    render_gpu_monitor,
)
from lazyslurm.widgets.job_table import ActiveJobTable, CompletedJobTable, JobSelected, set_partition_colors, set_display_config
from lazyslurm.widgets.metadata_view import MetadataView
from lazyslurm.widgets.usage_view import UsageTable, format_hours
from lazyslurm.widgets.version_footer import VersionFooter
from lazyslurm.widgets.partition_view import (
    NodeSelected,
    set_node_display,
    NodeTable,
    PartitionJobTable,
    PartitionSelected,
    PartitionTable,
)


# ---------------------------------------------------------------------------
# Modal screens
# ---------------------------------------------------------------------------


class HelpScreen(ModalScreen[None]):
    """Key bindings for the panel the user is actually in.

    The content comes from `lazyslurm.help`, which a test cross-checks against
    the real BINDINGS so this cannot drift out of date again.
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("question_mark", "dismiss", "Close"),
    ]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen > Vertical {
        width: 78;
        height: auto;
        max-height: 90%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    HelpScreen VerticalScroll {
        height: auto;
        max-height: 100%;
    }
    """

    def __init__(self, context: str = help_topics.JOBS) -> None:
        super().__init__()
        self.context = context

    def compose(self) -> ComposeResult:
        with Vertical():
            yield VerticalScroll(Static(help_topics.render(self.context)))


class ConfirmCancelScreen(ModalScreen[bool]):
    """Confirm job cancellation."""

    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n", "deny", "No"),
        Binding("escape", "deny", "Cancel"),
    ]

    DEFAULT_CSS = """
    ConfirmCancelScreen {
        align: center middle;
    }
    ConfirmCancelScreen > Vertical {
        width: 50;
        height: auto;
        border: round $error;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, job_ids: str | list[str]) -> None:
        super().__init__()
        self.job_ids = [job_ids] if isinstance(job_ids, str) else list(job_ids)

    def compose(self) -> ComposeResult:
        with Vertical():
            if len(self.job_ids) == 1:
                msg = f"[bold red]Cancel job {self.job_ids[0]}?[/]\n\n"
            else:
                preview = ", ".join(self.job_ids[:5])
                if len(self.job_ids) > 5:
                    preview += f", ... ({len(self.job_ids)} total)"
                msg = f"[bold red]Cancel {len(self.job_ids)} jobs?[/]\n\n[dim]{preview}[/]\n\n"
            yield Static(
                msg + "Press [bold]y[/] to confirm, [bold]n[/] or [bold]Escape[/] to abort."
            )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


class ConfirmResubmitScreen(ModalScreen[bool]):
    """Confirm job resubmission."""

    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n", "deny", "No"),
        Binding("escape", "deny", "Cancel"),
    ]

    DEFAULT_CSS = """
    ConfirmResubmitScreen {
        align: center middle;
    }
    ConfirmResubmitScreen > Vertical {
        width: 60;
        height: auto;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, job_id: str, script: str) -> None:
        super().__init__()
        self.job_id = job_id
        self.script = script

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"[bold]Resubmit job {self.job_id}?[/]\n\n"
                f"Script: [cyan]{self.script}[/]\n\n"
                "Press [bold]y[/] to confirm, [bold]n[/] or [bold]Escape[/] to abort."
            )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


class EditJobScreen(ModalScreen[dict]):
    """Edit properties (runtime, partition, resources) of pending job(s).

    Dismisses with a dict of the fields the user actually changed, or None if
    aborted. For a single job the inputs are prefilled with its current values;
    for a multi-job edit they start blank and every non-empty field is applied
    to all selected jobs.
    """

    # Up/Down move between lines like in an editor, so they must be handled
    # before the focused Input sees them (priority=True).
    BINDINGS = [
        Binding("ctrl+s", "submit", "Write"),
        Binding("escape", "cancel", "Quit"),
        Binding("up", "prev_field", "Up", show=False, priority=True),
        Binding("down", "next_field", "Down", show=False, priority=True),
        Binding("tab", "next_field", "Next", show=False, priority=True),
        Binding("shift+tab", "prev_field", "Prev", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    EditJobScreen {
        align: center middle;
    }
    EditJobScreen > Vertical {
        width: 46;
        height: auto;
        border: round $accent;
        border-title-color: $text-muted;
        border-title-align: left;
        border-subtitle-color: $text-muted;
        border-subtitle-align: right;
        background: $surface;
        padding: 0 1;
    }
    EditJobScreen .line {
        height: 1;
    }
    EditJobScreen .lineno {
        width: 3;
        color: $text-muted 50%;
        text-align: right;
    }
    EditJobScreen .field-label {
        width: 16;
        color: $text-muted;
        padding-left: 1;
    }
    EditJobScreen Input {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
        background: $surface;
    }
    /* Textual's own `Input:focus` rule re-adds a tall border, which would eat
       both rows of a 1-line field; this outranks it and keeps the line flat. */
    EditJobScreen Input:focus {
        border: none;
        padding: 0;
        background: $boost;
    }
    """

    def __init__(
        self,
        job_ids: list[str],
        current: dict[str, str] | None = None,
        prefill: dict[str, str] | None = None,
        title: str = "",
    ) -> None:
        super().__init__()
        self.job_ids = list(job_ids)
        self.current = current or {}
        # What the fields *show*, when that differs from what the job has —
        # a suggested value is still a change, so it is diffed against
        # ``current`` rather than against itself.
        self.prefill = {**self.current, **(prefill or {})}
        self.title_text = title
        self._keys = [f[0] for f in slurm.EDITABLE_FIELDS]

    def compose(self) -> ComposeResult:
        with Vertical():
            for number, (key, _label, scontrol_key, _attr) in enumerate(slurm.EDITABLE_FIELDS, 1):
                with Horizontal(classes="line"):
                    yield Static(str(number), classes="lineno")
                    yield Static(scontrol_key, classes="field-label")
                    yield Input(
                        value=self.prefill.get(key, ""),
                        id=f"edit-{key}",
                    )

    def on_mount(self) -> None:
        box = self.query_one(Vertical)
        if self.title_text:
            box.border_title = f" {self.title_text} "
        elif len(self.job_ids) == 1:
            box.border_title = f" job.{self.job_ids[0]} "
        else:
            box.border_title = f" job.{len(self.job_ids)}-selected "
        box.border_subtitle = " ^S write  esc quit "
        self.query_one(f"#edit-{self._keys[0]}", Input).focus()

    # --- editor-style line navigation ---

    def _focused_index(self) -> int:
        focused = self.focused
        if focused is not None and focused.id and focused.id.startswith("edit-"):
            key = focused.id[len("edit-"):]
            if key in self._keys:
                return self._keys.index(key)
        return 0

    def _focus_line(self, index: int) -> None:
        key = self._keys[index % len(self._keys)]
        self.query_one(f"#edit-{key}", Input).focus()

    def action_next_field(self) -> None:
        self._focus_line(self._focused_index() + 1)

    def action_prev_field(self) -> None:
        self._focus_line(self._focused_index() - 1)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self.action_submit()

    def action_submit(self) -> None:
        changed: dict[str, str] = {}
        for key, _label, _scontrol_key, _attr in slurm.EDITABLE_FIELDS:
            value = self.query_one(f"#edit-{key}", Input).value.strip()
            if value and value != self.current.get(key, "").strip():
                changed[key] = value
        self.dismiss(changed)

    def action_cancel(self) -> None:
        self.dismiss({})


class SSHPromptScreen(ModalScreen[str | None]):
    """Ask the user for whatever the cluster's SSH login is prompting for.

    Used for passwords and for two-factor verification codes: the SSH session
    forwards the server's own prompt text, so the label reads exactly like it
    would in a terminal ("Verification code:", "Duo passcode:", ...).
    Dismisses with the answer, or None if the user aborts.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    SSHPromptScreen {
        align: center middle;
    }
    SSHPromptScreen > Vertical {
        width: 56;
        height: auto;
        border: round $accent;
        border-title-color: $text-muted;
        border-title-align: left;
        border-subtitle-color: $text-muted;
        border-subtitle-align: right;
        background: $surface;
        padding: 0 1;
    }
    SSHPromptScreen Input {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
        background: $surface;
    }
    SSHPromptScreen Input:focus {
        border: none;
        padding: 0;
        background: $boost;
    }
    SSHPromptScreen .prompt {
        height: auto;
        color: $text;
    }
    """

    def __init__(self, host: str, prompt: str, secret: bool = True) -> None:
        super().__init__()
        self.host = host
        self.prompt = prompt
        self.secret = secret

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.prompt, classes="prompt")
            yield Input(password=self.secret, id="ssh-answer")

    def on_mount(self) -> None:
        box = self.query_one(Vertical)
        box.border_title = f" ssh {self.host} "
        box.border_subtitle = " enter send  esc cancel "
        self.query_one("#ssh-answer", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class NodeScreen(Screen):
    """The nodes of one partition, and what is running on the highlighted one.

    Reached with Enter from the partition monitor. Top: every node with its
    state, CPU allocation and load, memory in use, GPUs taken, and the drain
    reason when Slurm has one. Bottom: the jobs on the highlighted node, from
    all users.
    """

    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("q", "close", "Back"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("g", "resolve_owners", "Who has the GPUs"),
        Binding("G", "resolve_usage", "GPU usage", show=False),
        Binding("tab", "focus_other", "Switch Panel", show=False),
        Binding("shift+tab", "focus_other", show=False),
    ]

    def __init__(self, partition: str, config: Config) -> None:
        super().__init__()
        self.partition = partition
        self.config = config
        self._selected_node: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="node-bar")
        yield NodeTable(id="node-table")
        yield PartitionJobTable(
            id="node-jobs",
            user=self.config.user or slurm.USER,
        )
        yield VersionFooter()

    async def on_mount(self) -> None:
        self.query_one("#node-table").border_title = f"Nodes of {self.partition}"
        self.query_one("#node-jobs").border_title = "Jobs on node"
        self.query_one("#node-table", NodeTable).focus()
        await self._refresh_nodes()
        if self.config.refresh > 0:
            self.set_interval(self.config.refresh, self._refresh_nodes)

    async def _refresh_nodes(self) -> None:
        table = self.query_one("#node-table", NodeTable)
        nodes = await slurm.get_partition_nodes(self.partition, self.config)
        table.update_nodes(nodes)

        idle = sum(1 for n in nodes if n.base_state == "idle")
        mixed = sum(1 for n in nodes if n.base_state == "mixed")
        busy = sum(1 for n in nodes if n.base_state == "allocated")
        out = sum(1 for n in nodes if n.base_state in
                  ("down", "drained", "draining", "fail", "failing", "maint"))
        gpus_used = sum(n.gpus_used for n in nodes)
        gpus_total = sum(n.gpus_total for n in nodes)
        summary = (
            f"[bold]{self.partition}[/]   {len(nodes)} nodes   "
            f"[green]{idle}[/] idle  [yellow]{mixed}[/] mixed  "
            f"[dark_orange]{busy}[/] full  [red]{out}[/] down/drained"
        )
        if gpus_total:
            summary += f"   GPUs [bold]{gpus_used}[/]/{gpus_total} in use"
        self.query_one("#node-bar", Static).update(summary + "   [dim](all users)[/]")

        selected = table.get_selected_node()
        if selected:
            await self._refresh_jobs(selected)

    async def _refresh_jobs(self, node: str) -> None:
        self._selected_node = node
        jobs = await slurm.get_node_jobs(node, self.config)
        job_table = self.query_one("#node-jobs", PartitionJobTable)
        job_table.update_jobs(jobs)
        job_table.border_title = f"Jobs on {node} ({len(jobs)})"

    async def on_node_selected(self, event: NodeSelected) -> None:
        if event.node != self._selected_node:
            await self._refresh_jobs(event.node)

    async def action_refresh_now(self) -> None:
        await self._refresh_nodes()

    async def action_resolve_owners(self) -> None:
        """`g`: fill in who holds each GPU of the highlighted node.

        One `scontrol show job -d` per job on the node. Not automatic, and not
        on the poll: browsing a large partition would otherwise turn every
        cursor move into a burst of round trips.
        """
        table = self.query_one("#node-table", NodeTable)
        node = table.get_selected_node()
        info = table.get_node(node) if node else None
        if not node or info is None:
            return
        devices = table.devices_of(info)
        if not devices:
            return
        job_table = self.query_one("#node-jobs", PartitionJobTable)
        jobs = [(j.job_id, j.user, j.name) for j in job_table.jobs()]
        owners = await slurm.get_device_owners(node, jobs)
        if not owners:
            # A key that appears to do nothing is worse than one that explains
            # itself: usually the node's GPUs are simply all free.
            self.notify(f"No GPU owners to resolve on {node}", timeout=4)
            return
        for device in devices:
            owner = owners.get(device.index)
            if owner:
                device.job_id, device.user, device.name, device.cpus = owner
        table.set_devices(node, devices)
        table.expand(node)

    async def action_resolve_usage(self) -> None:
        """`Shift+G`: live utilisation and memory for the highlighted node."""
        table = self.query_one("#node-table", NodeTable)
        node = table.get_selected_node()
        info = table.get_node(node) if node else None
        if not node or info is None or self.config.no_live:
            return
        devices = table.devices_of(info)
        if not devices:
            return
        reading = await slurm.get_gpu_sample(node)
        if not reading.gpus:
            self.notify(
                f"Could not read GPU usage on {node}", severity="warning", timeout=6,
            )
            return
        if len(reading.gpus) != len(devices):
            # Slurm confines an ssh session to the cgroup of whatever job it
            # lands in, so nvidia-smi can report a subset -- renumbered from 0,
            # which does NOT line up with Slurm's IDX. Attributing those figures
            # to devices by position would put another job's utilisation on the
            # wrong row, so refuse rather than guess.
            self.notify(
                f"{node} reported {len(reading.gpus)} of {len(devices)} GPUs — "
                "the session is confined to one job's devices, so the readings "
                "cannot be matched to devices",
                severity="warning",
                timeout=8,
            )
            return
        by_index = {gpu.index: gpu for gpu in reading.gpus}
        for device in devices:
            gpu = by_index.get(device.index)
            if gpu is None:
                continue
            device.util = gpu.util
            device.mem_used = gpu.mem_used
            device.mem_total = gpu.mem_total
            device.power = gpu.power
            device.power_limit = gpu.power_limit
        table.set_devices(node, devices)
        table.expand(node)

    def action_focus_other(self) -> None:
        table = self.query_one("#node-table", NodeTable)
        jobs = self.query_one("#node-jobs", PartitionJobTable)
        (jobs if table.has_focus else table).focus()

    def action_close(self) -> None:
        self.app.pop_screen()


class PartitionScreen(Screen):
    """Full-screen partition monitor.

    Top: every partition with node/CPU allocated-idle-other-total counts and a
    load bar. Bottom: the jobs on the highlighted partition, from *all* users
    (the main job tables are filtered to you).
    """

    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("p", "close", "Back"),
        Binding("q", "close", "Back"),
        Binding("enter", "show_nodes", "Nodes"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("tab", "focus_other", "Switch Panel", show=False),
        Binding("shift+tab", "focus_other", show=False),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self._selected_partition: str | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="partition-bar")
        yield PartitionTable(id="partition-table")
        yield PartitionJobTable(
            id="partition-jobs",
            user=self.config.user or slurm.USER,
        )
        yield VersionFooter()

    async def on_mount(self) -> None:
        self.query_one("#partition-table").border_title = "Partitions"
        self.query_one("#partition-jobs").border_title = "Jobs on partition"
        self.query_one("#partition-table", PartitionTable).focus()
        await self._refresh_partitions()
        if self.config.refresh > 0:
            self.set_interval(self.config.refresh, self._refresh_partitions)

    async def _refresh_partitions(self) -> None:
        table = self.query_one("#partition-table", PartitionTable)
        partitions = await slurm.get_partitions(self.config)
        table.update_partitions(partitions)

        total_nodes = sum(p.nodes_total for p in partitions)
        alloc_nodes = sum(p.nodes_alloc for p in partitions)
        running = sum(p.running for p in partitions)
        pending = sum(p.pending for p in partitions)
        self.query_one("#partition-bar", Static).update(
            f"[bold]{len(partitions)}[/] partitions   "
            f"[green]{alloc_nodes}[/]/{total_nodes} nodes allocated   "
            f"[green]{running}[/] running   [yellow]{pending}[/] pending   "
            "[dim](all users)[/]"
        )

        selected = table.get_selected_partition()
        if selected:
            await self._refresh_jobs(selected)

    async def _refresh_jobs(self, partition: str) -> None:
        self._selected_partition = partition
        jobs = await slurm.get_partition_jobs(partition, self.config)
        job_table = self.query_one("#partition-jobs", PartitionJobTable)
        job_table.update_jobs(jobs)
        job_table.border_title = f"Jobs on {partition} ({len(jobs)})"

    async def on_partition_selected(self, event: PartitionSelected) -> None:
        if event.partition != self._selected_partition:
            await self._refresh_jobs(event.partition)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter on a partition row. DataTable claims the key for its own
        select action, so this message — not the binding — is what fires."""
        if event.data_table.id == "partition-table":
            self.action_show_nodes()

    def action_show_nodes(self) -> None:
        """Drill into the highlighted partition's nodes."""
        table = self.query_one("#partition-table", PartitionTable)
        partition = table.get_selected_partition()
        if not partition:
            return
        self.app.push_screen(NodeScreen(partition, self.config))

    async def action_refresh_now(self) -> None:
        await self._refresh_partitions()

    def action_focus_other(self) -> None:
        table = self.query_one("#partition-table", PartitionTable)
        jobs = self.query_one("#partition-jobs", PartitionJobTable)
        (jobs if table.has_focus else table).focus()

    def action_close(self) -> None:
        self.app.pop_screen()


class UsageScreen(Screen):
    """Account usage and fairshare — where the allocation went, and what it costs you.

    sreport can take seconds on a busy accounting database, so the screen opens
    immediately with a placeholder and fills in when the data lands. Nothing
    here touches the poll loop: it is fetched on open, on `r`, and when the
    window changes.
    """

    BINDINGS = [
        Binding("escape", "close", "Back"),
        Binding("U", "close", "Back"),
        Binding("q", "close", "Back"),
        Binding("r", "reload", "Refresh"),
        Binding("w", "cycle_window", "Window"),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self.window = "month"
        self.user = config.user or slurm.USER

    def compose(self) -> ComposeResult:
        yield Static(id="usage-bar")
        yield Static(id="usage-fairshare")
        yield UsageTable(id="usage-table", user=self.user)
        yield VersionFooter()

    async def on_mount(self) -> None:
        self.query_one("#usage-table").border_title = "Account usage"
        self.query_one("#usage-fairshare").border_title = "Fair share"
        self.query_one("#usage-bar", Static).update("[dim]loading usage...[/]")
        self.query_one("#usage-fairshare", Static).update("[dim]loading...[/]")
        self.query_one("#usage-table", UsageTable).focus()
        self.run_worker(self._load(), exclusive=True)

    async def _load(self) -> None:
        _, _, label = slurm.usage_window(self.window)
        rows, shares = await asyncio.gather(
            slurm.get_account_usage(self.window),
            slurm.get_fairshare(self.user),
        )

        table = self.query_one("#usage-table", UsageTable)
        table.update_rows(rows)

        if not rows and not shares:
            reason = ("this cluster has no Slurm accounting enabled"
                      if not slurm.accounting_available()
                      else "no accounting data for you in this window")
            self.query_one("#usage-bar", Static).update(
                f"[dim]{label} — {reason}[/]"
            )
            self.query_one("#usage-fairshare", Static).update("")
            return

        total = table.total_hours
        mine = table.my_hours
        share = f"  [bold]{mine / total * 100:.0f}%[/] of it yours" if total else ""
        self.query_one("#usage-bar", Static).update(
            f"[bold]{label}[/]   {format_hours(mine)} CPU-hours used by you   "
            f"[dim]account total {format_hours(total)}[/]{share}"
            f"   [dim](w cycles window)[/]"
        )
        self.query_one("#usage-fairshare", Static).update(self._fairshare_text(shares))

    def _fairshare_text(self, shares: list) -> str:
        mine = [s for s in shares if s.user] or shares
        if not mine:
            return "[dim]sshare reported no association for you[/]"
        lines = []
        for share in mine[:3]:
            factor = "n/a" if share.fairshare is None else f"{share.fairshare:.3f}"
            lines.append(
                f"[bold]{share.account}[/]  factor [bold]{factor}[/]  "
                f"entitled {share.norm_shares * 100:.2f}%  "
                f"used {share.effective_usage * 100:.2f}%"
            )
            lines.append(f"  [dim]{share.reading}[/]")
        return "\n".join(lines)

    async def action_reload(self) -> None:
        self.query_one("#usage-bar", Static).update("[dim]loading usage...[/]")
        self.run_worker(self._load(), exclusive=True)

    async def action_cycle_window(self) -> None:
        self.window = slurm.next_usage_window(self.window)
        await self.action_reload()

    def action_close(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------


_MAX_HISTORY = 60  # max sparkline samples per job


def _exit_code(status: int) -> str:
    """Readable exit status from what os.system() returned."""
    try:
        return f"exit {os.waitstatus_to_exitcode(status)}"
    except ValueError:  # killed by a signal
        return f"status {status}"


class LazySlurmApp(App):
    """LazySlurm — a TUI for monitoring Slurm jobs."""

    TITLE = "LazySlurm"
    CSS_PATH = "lazyslurm.tcss"

    # Keep the key bar short: at typical ~80-column terminals, showing many
    # bindings overflows and Textual turns the bar into a scrollable slider,
    # which hides keys instead of advertising them. All bindings remain available
    # and are listed in the `?` help screen.
    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("question_mark", "help", "Help", show=True, key_display="?"),
        Binding("slash", "toggle_search", "Search", show=True, key_display="/"),
        Binding("m", "toggle_bookmark", "Bookmark", show=False),
        Binding("M", "cycle_monitor_mode", "Monitor mode", show=False),
        Binding("c", "cancel_job", "Cancel", show=False),
        Binding("shift+c", "force_cancel_job", "Force Cancel", show=False),
        Binding("ctrl+v", "toggle_multiselect", "Multi-select", show=False),
        Binding("s", "resubmit_job", "Resubmit", show=False),
        Binding("S", "resubmit_job_edit", "Resubmit (edit resources)", show=False),
        Binding("u", "edit_job", "Edit Job", show=False),
        Binding("p", "partitions", "Partitions", show=False),
        Binding("U", "usage", "Usage", show=False),
        Binding("b", "view_batch_script", "Script", show=False),
        Binding("o", "ssh_to_node", "Shell", show=False),
        Binding("O", "ssh_to_node_alt", "Shell (other method)", show=False),
        Binding("l", "page_log", "Pager", show=False),
        Binding("e", "edit_stdout", "Edit Out", show=False),
        Binding("shift+e", "edit_stderr", "Edit Err", show=False),
        Binding("comma", "edit_config", "Config", show=False, key_display=","),
        Binding("r", "refresh", "Refresh", show=True),
        # priority: Textual's own Tab binding moves focus to the next widget,
        # which would fight the panel cycle these implement.
        Binding("tab", "focus_next_right", "Next Panel", show=False, priority=True),
        Binding("shift+tab", "focus_prev_right", "Prev Panel", show=False, priority=True),
        Binding("left", "focus_prev_right", show=False),
        Binding("right", "focus_next_right", show=False),
        Binding("left_square_bracket", "prev_detail_tab", show=False),
        Binding("right_square_bracket", "next_detail_tab", show=False),
        Binding("left_parenthesis", "prev_meta_tab", show=False),
        Binding("right_parenthesis", "next_meta_tab", show=False),
    ]

    def __init__(
        self,
        config: Config | None = None,
        config_overrides: list[str] | None = None,
        config_warnings: list[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.config = config or Config()
        self._config_overrides = config_overrides or []
        self._config_warnings = config_warnings or []
        # Track which right panel has focus: "detail" or "metadata"
        self._right_focus: str = "detail"
        # Currently selected job
        self._selected_job_id: str | None = None
        self._selected_source: str = "active"
        # Node of selected job (for live monitoring)
        self._selected_node: str = ""
        # Help screen toggle
        self._help_open: bool = False
        # Search
        self._search_visible: bool = False
        # Bookmarks (session-only)
        self._bookmarked_ids: set[str] = set()
        # Job completion tracking
        self._known_running_ids: set[str] = set()
        self._first_poll_done: bool = False
        # Sparkline resource history
        self._resource_history: dict[str, dict[str, list[float]]] = {}
        # How the cpu/gpu tabs render — Shift+M cycles it for this session only,
        # the config file sets what it starts on.
        self._monitor_mode: str = (
            self.config.resource_monitor
            if self.config.resource_monitor in RESOURCE_MONITOR_MODES else "graph"
        )
        # Per-job history sampled from the node itself (not sacct): per-core and
        # per-GPU series for the meter/graph modes.
        self._monitor_history: dict[str, dict] = {}
        # Previous (TotalCPU, Elapsed) per job — the CPU series is a delta
        self._cpu_marker: dict[str, tuple[float, float] | None] = {}
        # Resubmit state
        self._resubmit_script: str = ""
        self._resubmit_work_dir: str = ""
        self._resubmit_job_id: str = ""
        # Current job log paths (for editor)
        self._stdout_path: str | None = None
        self._stderr_path: str | None = None
        # Job id `c` will cancel: a task, or an array's base id
        self._cancel_target: str | None = None
        # Multi-select state
        self._multiselect_mode: bool = False
        self._multiselect_table: str = ""  # "active" or "completed"
        self._multiselect_anchor: str | None = None  # job_id where visual mode started
        self._multiselect_ids: set[str] = set()
        # Job ids currently being edited by EditJobScreen
        self._edit_job_ids: list[str] = []
        # Timer clearing the status line
        self._status_timer = None
        # Whether the refresh timers have been started (remote mode waits for
        # the SSH session, so this can happen well after mount).
        self._polling: bool = False

    def compose(self) -> ComposeResult:
        show_gpu = not self.config.no_gpu and not self.config.no_live
        yield Static(id="cluster-bar")
        with Horizontal(id="main-container"):
            with Vertical(id="left-column"):
                yield Input(
                    placeholder="Filter: text · state:pend · part:gpu · name:train · gpu:>0",
                    id="search-input",
                )
                yield ActiveJobTable(id="active-jobs")
                yield CompletedJobTable(id="completed-jobs")
            with Vertical(id="right-column"):
                yield DetailView(id="detail-view", show_gpu=show_gpu)
                yield MetadataView(id="metadata-view")
                yield RichLog(id="command-log", wrap=True, markup=True)
        yield Static(id="status-line")
        yield VersionFooter()

    def on_mount(self) -> None:
        slurm.set_config(self.config)
        slurm.set_notice_callback(self._log)
        set_partition_colors(self.config.partition_colors)
        set_display_config(
            max_name=self.config.max_name_width,
            max_partition=self.config.max_partition_width,
            abbreviate=self.config.abbreviate_states,
            collapse_arrays=self.config.collapse_arrays,
        )
        set_node_display(
            node_expand=self.config.node_expand,
            gpu_column=self.config.gpu_column,
        )

        # Prune old cache entries. Point the script cache at the configured
        # directory first, so we prune the one we will actually be using.
        from lazyslurm import config as persistent_config
        persistent_config.prune_log_cache(max_age_days=self.config.cache_max_age_days)
        persistent_config.set_script_cache_dir(self.config.script_cache_dir)
        persistent_config.prune_script_cache(max_age_days=self.config.cache_max_age_days)

        self.query_one("#detail-view").border_title = "Job Details"
        self.query_one("#metadata-view").border_title = "Job Metadata"
        self.query_one("#command-log").border_title = "Command Log"

        # Hide search input initially
        self.query_one("#search-input").display = False

        if self.config.remote:
            self.title = f"LazySlurm [{self.config.remote}]"

        self.query_one("#active-jobs", ActiveJobTable).focus()

        # Log config overrides
        for override in self._config_overrides:
            self._log("config override", override)

        # A setting nothing reads is inert, and the file looks right — say so.
        for warning in self._config_warnings:
            self._log("config file", warning)

        # Login node warning
        import socket
        hostname = socket.gethostname()
        remote_host = self.config.remote.split("@")[-1] if self.config.remote else ""
        for name in (hostname, remote_host):
            if name and "login" in name.lower():
                self._log("[yellow]warning[/]", f"running on login node '{name}'")
                self.notify(
                    f"Running on login node '{name}' — be mindful of resource usage",
                    severity="warning",
                    timeout=8,
                )

        # Remote mode: open the one SSH session everything runs through before
        # the first poll, so any password / 2FA prompt is answered up front.
        # The timers start only once that succeeds — a tick landing mid-login
        # is a command queued behind the connection for no purpose.
        if self.config.remote:
            self.call_after_refresh(self._start_remote_session)
        else:
            self.call_after_refresh(self._poll_jobs)
            self._start_polling()

    def _start_polling(self) -> None:
        """Begin the refresh timers (refresh=0 disables auto-refresh)."""
        if self._polling:
            return
        self._polling = True
        if self.config.refresh > 0:
            self.set_interval(self.config.refresh, self._poll_jobs)
            if not self.config.no_live:
                self.set_interval(self.config.refresh, self._refresh_live_monitors)
        else:
            self._log("auto-refresh", "disabled (refresh=0)")

    # ------------------------------------------------------------------
    # Remote SSH session
    # ------------------------------------------------------------------

    def _ssh_control_opt(self) -> str:
        """`-o ControlPath=...` for the live session, so helpers reuse it.

        Any ssh/scp we shell out to must ride the connection that is already
        authenticated; opening a fresh one would prompt for 2FA again.
        """
        session = slurm.get_session()
        if session is None:
            return ""
        return f"-o {shlex.quote('ControlPath=' + session.control_path)}"

    def _proxy_command(self) -> str:
        """ProxyCommand that hops to a compute node through the live session."""
        return f"ssh {self._ssh_control_opt()} -W %h:%p {shlex.quote(self.config.remote)}"

    async def _ssh_prompt(self, prompt: str, secret: bool) -> str | None:
        """Ask the user for a password / verification code (SSH prompt callback)."""
        return await self.push_screen_wait(
            SSHPromptScreen(self.config.remote, prompt, secret)
        )

    async def _start_remote_session(self) -> None:
        """Connect the shared SSH session, then start polling."""
        self._log(f"ssh {self.config.remote}", "opening session...")
        ok, msg = await slurm.connect_remote(self._ssh_prompt, self.config)
        self._log("ssh", msg)
        if not ok:
            self.notify(msg, title="SSH connection failed", severity="error", timeout=10)
            self._log("ssh", "press [bold]r[/] to retry")
            return
        await self._poll_jobs()
        self._start_polling()

    async def on_unmount(self) -> None:
        # Drop the notice hook first: it points at this app's command log, and
        # anything logged after the widgets are gone would query a dead screen.
        slurm.set_notice_callback(None)
        await slurm.disconnect_remote()

    # ------------------------------------------------------------------
    # Data polling
    # ------------------------------------------------------------------

    async def _poll_jobs(self, force: bool = False) -> None:
        active_table = self.query_one("#active-jobs", ActiveJobTable)
        completed_table = self.query_one("#completed-jobs", CompletedJobTable)

        running, completed, part_info = await asyncio.gather(
            slurm.get_running_jobs(self.config),
            slurm.get_completed_jobs(self.config),
            slurm.get_partition_availability(self.config, force=force),
        )
        active_table.update_jobs(running)
        completed_table.update_jobs(completed)

        # Update cluster bar (counts derived from the squeue result above)
        summary = slurm.format_cluster_summary(running, part_info, self.config)
        self.query_one("#cluster-bar", Static).update(summary)

        # Job completion notifications
        current_ids = {j.job_id for j in running}
        if self._first_poll_done:
            disappeared = self._known_running_ids - current_ids
            for job_id in disappeared:
                final = next(
                    (c.state for c in completed if c.job_id == job_id),
                    "completed",
                )
                self._notify_job_done(job_id, final)
            # If the job we're viewing just ended, reload its details so the
            # panels reflect the terminal state instead of stale running data.
            if self._selected_job_id in disappeared:
                self._trigger_load(self._selected_job_id)
        self._known_running_ids = current_ids
        self._first_poll_done = True

        # Sparkline history is only ever plotted for a running job, so drop the
        # entries of jobs that have ended — otherwise a long session accumulates
        # one per job the user has ever highlighted.
        self._resource_history = {
            job_id: history
            for job_id, history in self._resource_history.items()
            if job_id in current_ids
        }
        self._cpu_marker = {
            job_id: marker
            for job_id, marker in self._cpu_marker.items()
            if job_id in current_ids
        }
        self._monitor_history = {
            job_id: history
            for job_id, history in self._monitor_history.items()
            if job_id in current_ids
        }

        # Collect sparkline samples for selected running job
        if self._selected_job_id and self._selected_job_id in current_ids:
            await self._collect_resource_sample(self._selected_job_id)

        # Auto-select first job if nothing is selected
        if self._selected_job_id is None:
            if active_table.row_count > 0:
                jid = active_table.get_selected_job_id()
                if jid:
                    self._selected_job_id = jid
                    self._selected_source = "active"
                    await self._load_job_details(jid)
            elif completed_table.row_count > 0:
                jid = completed_table.get_selected_job_id()
                if jid:
                    self._selected_job_id = jid
                    self._selected_source = "completed"
                    await self._load_job_details(jid)

    # ------------------------------------------------------------------
    # Job completion notification
    # ------------------------------------------------------------------

    def _notify_job_done(self, job_id: str, state: str) -> None:
        self._log("job completed", f"{job_id} {state}")
        self.bell()
        # Try desktop notification (non-blocking, silent failure)
        asyncio.create_task(self._try_desktop_notify(
            "LazySlurm", f"Job {job_id} {state}",
        ))

    @staticmethod
    async def _try_desktop_notify(title: str, body: str) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "notify-send", title, body,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except (OSError, asyncio.CancelledError):
            pass  # no notify-send on this box, or the app is shutting down

    # ------------------------------------------------------------------
    # Sparkline resource history
    # ------------------------------------------------------------------

    async def _collect_resource_sample(self, job_id: str) -> None:
        stats = await slurm.get_job_stats(job_id)
        if stats is None:
            return
        hist = self._resource_history.setdefault(job_id, {"memory": [], "cpu": []})
        mem = parse_mem_bytes(stats.max_rss)
        if mem is not None:
            hist["memory"].append(mem)
            if len(hist["memory"]) > _MAX_HISTORY:
                hist["memory"] = hist["memory"][-_MAX_HISTORY:]
        # TotalCPU and Elapsed are both cumulative, so the ratio of their deltas
        # since the previous sample is the core-equivalents busy over that
        # interval — the same quantity the Efficiency block reports as cpu_used.
        # Normalised by AllocCPUS it gives a 0-1 series: a flat CPU line under a
        # rising memory line is what a stalled job looks like.
        cpu_now = parse_duration(stats.total_cpu)
        elapsed_now = parse_duration(stats.elapsed)
        prev = self._cpu_marker.get(job_id)
        self._cpu_marker[job_id] = (
            (cpu_now, elapsed_now) if cpu_now is not None and elapsed_now is not None
            else None
        )
        if cpu_now is None or elapsed_now is None or prev is None:
            return
        d_cpu, d_elapsed = cpu_now - prev[0], elapsed_now - prev[1]
        if d_elapsed <= 0:
            return
        busy = d_cpu / d_elapsed
        if stats.alloc_cpus > 0:
            busy /= stats.alloc_cpus
        hist["cpu"].append(max(0.0, busy))
        if len(hist["cpu"]) > _MAX_HISTORY:
            hist["cpu"] = hist["cpu"][-_MAX_HISTORY:]

    # ------------------------------------------------------------------
    # Job selection handling (debounced)
    # ------------------------------------------------------------------

    _selection_timer: object | None = None

    async def on_job_selected(self, message: JobSelected) -> None:
        # In multi-select mode: extend the selection range from the anchor
        # to the current cursor position, but do NOT reload details.
        if self._multiselect_mode and message.source_table == self._multiselect_table:
            self._update_multiselect_range(message.job_id)
            return

        self._selected_job_id = message.job_id
        self._selected_source = message.source_table
        # Debounce: cancel any pending load and schedule a new one after 200ms.
        if self._selection_timer is not None:
            self._selection_timer.stop()
        self._selection_timer = self.set_timer(
            0.2, lambda: self._trigger_load(message.job_id),
        )

    def _update_multiselect_range(self, current_id: str) -> None:
        """Compute the selection range from anchor to current cursor."""
        if self._multiselect_table == "active":
            table = self.query_one("#active-jobs", ActiveJobTable)
        else:
            table = self.query_one("#completed-jobs", CompletedJobTable)

        order = table.get_row_order()
        if self._multiselect_anchor not in order or current_id not in order:
            return

        a = order.index(self._multiselect_anchor)
        b = order.index(current_id)
        lo, hi = (a, b) if a <= b else (b, a)
        self._multiselect_ids = set(order[lo:hi + 1])
        table.set_multiselected(self._multiselect_ids)

    def _trigger_load(self, job_id: str) -> None:
        """Start loading job details, cancelling any in-flight load."""
        # Only load if this is still the selected job (user may have moved on)
        if self._selected_job_id == job_id:
            self.run_worker(
                self._load_job_details(job_id),
                exclusive=True,  # cancels previous worker
                group="job_detail",
            )

    async def _load_job_details(self, job_id: str) -> None:
        detail_view = self.query_one("#detail-view", DetailView)
        metadata_view = self.query_one("#metadata-view", MetadataView)

        detail = await slurm.get_job_detail(job_id)
        if detail is None:
            detail_view.clear_all()
            metadata_view.load_detail(None)
            self._selected_node = ""
            self._stdout_path = None
            self._stderr_path = None
            return

        self._selected_node = detail.node_list
        self._stdout_path = detail.stdout_path
        self._stderr_path = detail.stderr_path

        # A pending job gets one extra call — sprio for the partition, which
        # answers both "what is my priority made of" and "how far up the queue
        # am I". Everything else the Pending tab shows (estimated start, reason,
        # dependency) is already in the scontrol output fetched above.
        pending = detail.state.upper().startswith("PENDING")

        # Load logs and stats — but NOT live CPU/GPU on selection.
        # Live monitors are loaded lazily by _refresh_live_monitors when
        # the user views those tabs, avoiding expensive SSH/srun calls.
        stdout_content, stderr_content, stats, priority = await asyncio.gather(
            slurm.read_log_file(detail.stdout_path),
            slurm.read_log_file(detail.stderr_path),
            slurm.get_job_stats(job_id),
            slurm.get_job_priority(job_id, detail.partition) if pending
            else asyncio.sleep(0),
        )

        # Check we're still on this job (user may have navigated away)
        if self._selected_job_id != job_id:
            return

        detail_view.load_stdout(stdout_content)
        detail_view.load_stderr(stderr_content)
        history = self._resource_history.get(job_id)
        detail_view.load_stats(stats, history=history)
        detail_view.load_cpu("[dim]Press \\[r] or wait for auto-refresh[/]")
        try:
            detail_view.load_gpu("[dim]Press \\[r] or wait for auto-refresh[/]")
        except NoMatches:
            pass  # GPU tab not present (--no-gpu / --no-live)
        metadata_view.load_detail(
            detail,
            priority if isinstance(priority, PriorityInfo) else None,
            priority_available=slurm.sprio_available(),
        )

    # ------------------------------------------------------------------
    # Live CPU/GPU auto-refresh
    # ------------------------------------------------------------------

    def _history_for(self, job_id: str) -> dict:
        """The per-job node-sampled history, created on first use."""
        return self._monitor_history.setdefault(job_id, {
            "cores": {},      # cpu id -> utilisation series
            "cpu": [],        # mean across the allocated cores
            "mem": [],        # fraction of the job's (or node's) memory
            "gpu_util": {},   # gpu index -> utilisation series
            "gpu_mem": {},    # gpu index -> memory-used fraction
        })

    @staticmethod
    def _append_sample(series: list[float], value: float) -> None:
        series.append(value)
        if len(series) > _MAX_HISTORY:
            del series[:-_MAX_HISTORY]

    def _record_node_sample(self, job_id: str, sample: NodeSample) -> None:
        hist = self._history_for(job_id)
        for core in sample.cores:
            self._append_sample(hist["cores"].setdefault(core.cpu, []), core.busy)
        if sample.cores:
            self._append_sample(hist["cpu"], sample.busy)
        if sample.mem_ratio is not None:
            self._append_sample(hist["mem"], sample.mem_ratio)

    def _record_gpu_sample(self, job_id: str, reading) -> None:
        hist = self._history_for(job_id)
        for gpu in reading.gpus:
            if gpu.util is not None:
                self._append_sample(hist["gpu_util"].setdefault(gpu.index, []), gpu.util)
            if gpu.mem_ratio is not None:
                self._append_sample(hist["gpu_mem"].setdefault(gpu.index, []), gpu.mem_ratio)

    async def _load_cpu_monitor(self) -> None:
        """Fetch and render the cpu tab in whichever mode is active.

        One round trip either way: text mode runs `ps`, the meter modes run the
        sampling script, and neither adds a call per metric.
        """
        detail_view = self.query_one("#detail-view", DetailView)
        job_id = self._selected_job_id or ""
        node = self._selected_node
        if self._monitor_mode == "text":
            content = await slurm.get_node_processes(node, self.config.user)
        else:
            sample = await slurm.get_node_sample(node, job_id)
            if job_id and not sample.error:
                self._record_node_sample(job_id, sample)
            content = render_cpu_monitor(
                sample,
                self._monitor_history.get(job_id),
                width=detail_view.monitor_width,
                graph=self._monitor_mode == "graph",
            )
        # The user may have moved to another job while the node was answering.
        if job_id and self._selected_job_id != job_id:
            return
        detail_view.load_cpu(content)

    async def _load_gpu_monitor(self) -> None:
        detail_view = self.query_one("#detail-view", DetailView)
        job_id = self._selected_job_id or ""
        node = self._selected_node
        if self._monitor_mode == "text":
            content = await slurm.get_gpu_status(node, job_id)
        else:
            reading = await slurm.get_gpu_sample(node, job_id)
            if job_id and not reading.error:
                self._record_gpu_sample(job_id, reading)
            content = render_gpu_monitor(
                reading,
                self._monitor_history.get(job_id),
                width=detail_view.monitor_width,
                graph=self._monitor_mode == "graph",
            )
        if job_id and self._selected_job_id != job_id:
            return
        detail_view.load_gpu(content)

    async def _refresh_live_monitors(self) -> None:
        if not self._selected_node or self._selected_node in ("N/A", "None", "(null)"):
            return

        detail_view = self.query_one("#detail-view", DetailView)
        tabs = detail_view.query_one("#detail-tabs")
        active_tab = tabs.active

        if active_tab == "tab-cpu":
            await self._load_cpu_monitor()
        elif active_tab == "tab-gpu" and not self.config.no_gpu:
            await self._load_gpu_monitor()

    async def action_cycle_monitor_mode(self) -> None:
        """Shift+M: text -> meter -> graph, for this session.

        Session-only on purpose: the config file stays the place that decides
        what the tabs open on, so cycling to compare modes does not rewrite it.
        """
        index = RESOURCE_MONITOR_MODES.index(self._monitor_mode)
        self._monitor_mode = RESOURCE_MONITOR_MODES[(index + 1) % len(RESOURCE_MONITOR_MODES)]
        self._log("monitor mode", self._monitor_mode)
        self._set_status(f"cpu/gpu view: {self._monitor_mode}")
        if not self.config.no_live:
            await self._refresh_live_monitors()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def on_key(self, event) -> None:
        # If search input is focused, Escape closes it
        search = self.query_one("#search-input", Input)
        if search.has_focus and event.key == "escape":
            event.prevent_default()
            search.value = ""
            search.display = False
            self._search_visible = False
            self._apply_filter("")
            self.query_one("#active-jobs", ActiveJobTable).focus()
            return

        active = self.query_one("#active-jobs", ActiveJobTable)
        completed = self.query_one("#completed-jobs", CompletedJobTable)

        if event.key == "down" and active.has_focus:
            if active.row_count > 0 and active.cursor_coordinate.row >= active.row_count - 1:
                if completed.row_count > 0:
                    event.prevent_default()
                    completed.focus()
                    completed.move_cursor(row=0)
        elif event.key == "up" and completed.has_focus:
            if completed.cursor_coordinate.row <= 0:
                if active.row_count > 0:
                    event.prevent_default()
                    active.focus()
                    active.move_cursor(row=active.row_count - 1)
        elif event.key == "down" and completed.has_focus:
            if completed.row_count > 0 and completed.cursor_coordinate.row >= completed.row_count - 1:
                if active.row_count > 0:
                    event.prevent_default()
                    active.focus()
                    active.move_cursor(row=0)
        elif event.key == "up" and active.has_focus:
            if active.cursor_coordinate.row <= 0:
                if completed.row_count > 0:
                    event.prevent_default()
                    completed.focus()
                    completed.move_cursor(row=completed.row_count - 1)

    # ------------------------------------------------------------------
    # Search / filter
    # ------------------------------------------------------------------

    def action_toggle_search(self) -> None:
        search = self.query_one("#search-input", Input)
        if self._search_visible:
            search.value = ""
            search.display = False
            self._search_visible = False
            self._apply_filter("")
            self.query_one("#active-jobs", ActiveJobTable).focus()
        else:
            search.display = True
            self._search_visible = True
            search.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search-input":
            self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter accepts the filter: close the bar, keep the query.

        Filtering exists to narrow a long list down to the jobs you want to act
        on. Every other way out of the bar clears the query, so the cursor
        could never reach the rows it found. Escape still abandons — accept and
        abandon are the useful pair.
        """
        if event.input.id != "search-input":
            return
        event.stop()
        search = self.query_one("#search-input", Input)
        search.display = False
        self._search_visible = False
        # Deliberately no _apply_filter("") — the filter stays in force, and
        # the table's border title keeps showing "— 2/4 match".
        self._focus_a_job_table()

    def _focus_a_job_table(self) -> None:
        """Focus whichever job table has rows, preferring the active one."""
        active = self.query_one("#active-jobs", ActiveJobTable)
        completed = self.query_one("#completed-jobs", CompletedJobTable)
        target = active if active.row_count else (completed if completed.row_count else active)
        target.focus()
        self._right_focus = "jobs"

    def _apply_filter(self, text: str) -> None:
        self.query_one("#active-jobs", ActiveJobTable).apply_filter(text)
        self.query_one("#completed-jobs", CompletedJobTable).apply_filter(text)

    # ------------------------------------------------------------------
    # Bookmarks
    # ------------------------------------------------------------------

    def _focused_job_table(self):
        """Whichever job table holds the cursor, or None."""
        active = self.query_one("#active-jobs", ActiveJobTable)
        completed = self.query_one("#completed-jobs", CompletedJobTable)
        if active.has_focus:
            return active
        if completed.has_focus:
            return completed
        return active if self._selected_source == "active" else completed

    def _selected_array(self) -> tuple[str, list] | None:
        """(base id, members) when the cursor sits on a collapsed array row."""
        table = self._focused_job_table()
        return table.selected_group() if table is not None else None

    def action_toggle_bookmark(self) -> None:
        # On a collapsed array, bookmark the array itself so the whole group
        # pins to the top rather than one arbitrary task.
        group = self._selected_array()
        target = group[0] if group else self._selected_job_id
        if target is None:
            return
        if target in self._bookmarked_ids:
            self._bookmarked_ids.discard(target)
        else:
            self._bookmarked_ids.add(target)
        self.query_one("#active-jobs", ActiveJobTable).set_bookmarks(self._bookmarked_ids)
        self.query_one("#completed-jobs", CompletedJobTable).set_bookmarks(self._bookmarked_ids)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _help_context(self) -> str:
        """Which panel the user is in, for the help screen."""
        screen = self.screen
        if isinstance(screen, PartitionScreen):
            return help_topics.PARTITIONS
        if isinstance(screen, NodeScreen):
            return help_topics.NODES
        if isinstance(screen, UsageScreen):
            return help_topics.USAGE

        focused = self.focused
        if focused is not None:
            if focused.id == "search-input":
                return help_topics.JOBS   # the filter keys are documented there
            for node in (focused, *focused.ancestors):
                if getattr(node, "id", None) == "detail-view":
                    return help_topics.DETAIL
                if getattr(node, "id", None) == "metadata-view":
                    return help_topics.METADATA
        return help_topics.JOBS

    def action_help(self) -> None:
        if self._help_open:
            self.app.pop_screen()
            self._help_open = False
        else:
            self._help_open = True
            self.push_screen(
                HelpScreen(self._help_context()), callback=self._on_help_dismissed
            )

    def _on_help_dismissed(self, _result: None) -> None:
        self._help_open = False

    def action_cancel_job(self) -> None:
        # Multi-select: cancel all selected jobs
        if self._multiselect_mode and self._multiselect_ids:
            ids = sorted(self._multiselect_ids)
            self.push_screen(
                ConfirmCancelScreen(ids),
                callback=self._on_multi_cancel_confirmed,
            )
            return

        # A collapsed array cancels as a whole: `scancel 123` takes every task.
        group = self._selected_array()
        if group:
            base, members = group
            self._cancel_target = base
            self.push_screen(
                ConfirmCancelScreen([f"{base} (array, {len(members)} rows)"]),
                callback=self._on_cancel_confirmed,
            )
            return

        if self._selected_job_id is None:
            self._set_status("No job selected")
            return
        self._cancel_target = self._selected_job_id
        self.push_screen(
            ConfirmCancelScreen(self._selected_job_id),
            callback=self._on_cancel_confirmed,
        )

    async def action_force_cancel_job(self) -> None:
        # Multi-select: force cancel all selected jobs
        if self._multiselect_mode and self._multiselect_ids:
            ids = sorted(self._multiselect_ids)
            self._log(f"scancel --signal=KILL {len(ids)} jobs")
            failed = 0
            for jid in ids:
                success, msg = await slurm.cancel_job(jid, force=True)
                failed += not success
                self._log("force cancel", msg)
            if failed:
                self._log("force cancel", f"{failed}/{len(ids)} failed")
            self._exit_multiselect()
            await self._poll_jobs()
            return

        group = self._selected_array()
        target = group[0] if group else self._selected_job_id
        if target is None:
            self._set_status("No job selected")
            return
        self._log(f"scancel --signal=KILL {target}")
        success, msg = await slurm.cancel_job(target, force=True)
        self._log("force cancel", msg)
        if success:
            await self._poll_jobs()

    async def _on_cancel_confirmed(self, confirmed: bool | None) -> None:
        target = self._cancel_target or self._selected_job_id
        if not confirmed or target is None:
            return
        self._log(f"scancel {target}")
        success, msg = await slurm.cancel_job(target)
        self._log("cancel", msg)
        if success:
            await self._poll_jobs()

    async def _on_multi_cancel_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        ids = sorted(self._multiselect_ids)
        self._log(f"scancel {len(ids)} jobs", ", ".join(ids[:5]) + (" ..." if len(ids) > 5 else ""))
        for jid in ids:
            success, msg = await slurm.cancel_job(jid)
            self._log("cancel", msg)
        self._exit_multiselect()
        await self._poll_jobs()

    # ------------------------------------------------------------------
    # Partition monitor
    # ------------------------------------------------------------------

    def action_partitions(self) -> None:
        """Open the partition monitor screen (Escape or p returns)."""
        self.push_screen(PartitionScreen(self.config))

    def action_usage(self) -> None:
        """Open the account usage screen (Escape or U returns)."""
        self.push_screen(UsageScreen(self.config))

    # ------------------------------------------------------------------
    # Edit job properties (scontrol update)
    # ------------------------------------------------------------------

    def action_edit_job(self) -> None:
        """Open the property editor for the selected (or multi-selected) jobs.

        Only pending jobs can be edited — Slurm fixes runtime, partition and
        the resource allocation once a job starts.
        """
        active = self.query_one("#active-jobs", ActiveJobTable)

        group = self._selected_array()
        if self._multiselect_mode and self._multiselect_ids:
            if self._multiselect_table != "active":
                self._set_status("Only pending jobs can be edited")
                return
            # A selected row may stand for a whole array; edit its tasks.
            ids = sorted(set(active.expand_ids(self._multiselect_ids)))
        elif group and self._selected_source == "active":
            ids = [job.job_id for job in group[1]]
        elif self._selected_job_id and self._selected_source == "active":
            ids = [self._selected_job_id]
        else:
            self._set_status("No pending job selected")
            return

        pending, skipped = [], []
        for jid in ids:
            job = active.get_job(jid)
            (pending if job is not None and job.state == "PENDING" else skipped).append(jid)

        if skipped:
            self._log("edit", f"skipping {len(skipped)} non-pending job(s): "
                              + ", ".join(skipped[:5]) + (" ..." if len(skipped) > 5 else ""))
        if not pending:
            self._set_status("Only pending jobs can be edited")
            return

        # Prefill from the job's current values when editing exactly one job.
        current: dict[str, str] = {}
        if len(pending) == 1:
            job = active.get_job(pending[0])
            for key, _label, _scontrol_key, attr in slurm.EDITABLE_FIELDS:
                current[key] = str(getattr(job, attr, "") or "")

        self._edit_job_ids = pending
        self.push_screen(EditJobScreen(pending, current), callback=self._on_edit_confirmed)

    async def _on_edit_confirmed(self, updates: dict | None) -> None:
        if not updates:
            return
        ids = self._edit_job_ids
        args = " ".join(slurm.build_update_args(updates))
        self._log(f"scontrol update {len(ids)} job(s)", args)
        failed = 0
        for jid in ids:
            success, msg = await slurm.update_job(jid, updates)
            failed += not success
            self._log("update", msg)
        if failed:
            self._log("update", f"{failed}/{len(ids)} failed")
        if self._multiselect_mode:
            self._exit_multiselect()
        await self._poll_jobs()
        if self._selected_job_id:
            self._trigger_load(self._selected_job_id)

    # ------------------------------------------------------------------
    # Multi-select mode (Ctrl+V)
    # ------------------------------------------------------------------

    def action_toggle_multiselect(self) -> None:
        if self._multiselect_mode:
            self._exit_multiselect()
            self._log("multi-select", "disabled")
            return

        # Determine which table currently has focus
        active = self.query_one("#active-jobs", ActiveJobTable)
        completed = self.query_one("#completed-jobs", CompletedJobTable)
        if active.has_focus:
            table, table_name = active, "active"
        elif completed.has_focus:
            table, table_name = completed, "completed"
        else:
            self._set_status("Focus a job table first")
            return

        anchor = table.get_selected_job_id()
        if not anchor:
            self._set_status("No job to anchor selection")
            return

        self._multiselect_mode = True
        self._multiselect_table = table_name
        self._multiselect_anchor = anchor
        self._multiselect_ids = {anchor}
        table.set_multiselected(self._multiselect_ids)
        self._log("multi-select", f"enabled — use Up/Down to extend, 'c' to cancel all, Ctrl+V to exit")

    def _exit_multiselect(self) -> None:
        self._multiselect_mode = False
        self._multiselect_anchor = None
        self._multiselect_ids = set()
        self.query_one("#active-jobs", ActiveJobTable).set_multiselected(set())
        self.query_one("#completed-jobs", CompletedJobTable).set_multiselected(set())
        self._multiselect_table = ""

    async def action_resubmit_job(self) -> None:
        if self._selected_job_id is None:
            self._set_status("No job selected")
            return
        if self._selected_source != "completed":
            self._set_status("Resubmit is only available for terminated jobs")
            return

        detail = await slurm.get_job_detail(self._selected_job_id)
        if detail is None:
            self._set_status("Cannot get job details")
            return

        script = detail.submit_line
        if not script or script == "N/A":
            self._set_status("Cannot determine submit script for this job")
            return

        state = detail.state.upper()
        if state.startswith("TIMEOUT") or state.startswith("OUT_OF_MEMORY"):
            self._log("resubmit", f"job ended {detail.state} — Shift+S resubmits with more")

        self._resubmit_script = script
        self._resubmit_work_dir = detail.work_dir
        self._resubmit_job_id = self._selected_job_id
        self.push_screen(
            ConfirmResubmitScreen(self._selected_job_id, script),
            callback=self._on_resubmit_confirmed,
        )

    async def _on_resubmit_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        self._log(f"sbatch {self._resubmit_script}")
        success, msg = await slurm.resubmit_job(
            self._resubmit_script, self._resubmit_work_dir, self._resubmit_job_id,
        )
        self._log("resubmit", msg)
        if success:
            await self._poll_jobs()

    # Which JobDetail accessor holds each editable field. The scontrol and
    # sacct spellings are already reconciled there.
    _RESUBMIT_PREFILL = {
        "time_limit": "time_limit",
        "partition": "partition",
        "nodes": "num_nodes",
        "cpus": "num_cpus",
        "memory": "memory",
    }

    async def action_resubmit_job_edit(self) -> None:
        """Resubmit a terminated job with different resources (Shift+S).

        The loop after a failure is "run it again, but bigger" — and the
        property editor (`u`) only works on jobs that are still queued, by
        which point it is too late.
        """
        if self._selected_job_id is None:
            self._set_status("No job selected")
            return
        if self._selected_source != "completed":
            self._set_status("Resubmit is only available for terminated jobs")
            return

        detail = await slurm.get_job_detail(self._selected_job_id)
        if detail is None:
            self._set_status("Cannot get job details")
            return

        script = detail.submit_line
        if not script or script == "N/A":
            self._set_status("Cannot determine submit script for this job")
            return

        current = {}
        for key, attr in self._RESUBMIT_PREFILL.items():
            value = str(getattr(detail, attr, "") or "")
            current[key] = "" if value == "N/A" else value

        suggested = slurm.suggest_resubmit_overrides(detail.state, current)
        for key, value in suggested.items():
            self._log("resubmit", f"{detail.state}: suggesting {key} {current[key]} -> {value}")

        self._resubmit_script = script
        self._resubmit_work_dir = detail.work_dir
        self._resubmit_job_id = self._selected_job_id
        self.push_screen(
            EditJobScreen(
                [self._selected_job_id],
                current=current,
                prefill=suggested,
                title=f"resubmit job.{self._selected_job_id} ",
            ),
            callback=self._on_resubmit_edited,
        )

    async def _on_resubmit_edited(self, overrides: dict | None) -> None:
        # Escape dismisses with {}, and so does Ctrl+S with nothing changed —
        # neither should submit a job. Plain `s` is the no-changes path.
        if not overrides:
            self._log("resubmit", "cancelled — no changes to submit")
            return
        tokens = shlex.split(self._resubmit_script)
        if tokens and tokens[0] == "sbatch":
            tokens = tokens[1:]
        preview = slurm.build_resubmit_tokens(tokens, overrides)
        chdir = f"--chdir {self._resubmit_work_dir} " if self._resubmit_work_dir else ""
        # The whole point is that the user can see what changed before it runs.
        self._log("sbatch", f"{chdir}{' '.join(preview)}")

        success, msg = await slurm.resubmit_job(
            self._resubmit_script,
            self._resubmit_work_dir,
            self._resubmit_job_id,
            overrides=overrides,
        )
        self._log("resubmit", msg)
        if success:
            await self._poll_jobs()

    async def action_ssh_to_node(self) -> None:
        await self._open_interactive_shell(self.config.interactive_shell)

    async def action_ssh_to_node_alt(self) -> None:
        """The access method the config did *not* choose (Shift+O).

        The two are different tasks — "poke at the machine" versus "debug
        inside my allocation" — and which one a user needs varies per job, not
        per install, so both are always one key away.
        """
        configured = self.config.interactive_shell
        other = "srun" if configured == "ssh" else "ssh"
        await self._open_interactive_shell(other)

    async def _open_interactive_shell(self, method: str) -> None:
        if not self._selected_node or self._selected_node in ("N/A", "None", "(null)", ""):
            self._log("shell", "no node assigned to this job")
            return

        node = slurm._first_node(self._selected_node)
        job_id = self._selected_job_id or ""
        # srun attaches to an allocation, so it needs one: a job that is no
        # longer running has no step to overlap with. Fall back rather than
        # fail, and say why.
        if method == "srun" and job_id not in self._known_running_ids:
            self._log("srun", f"job {job_id or '?'} is not running — using ssh instead")
            method = "ssh"

        cmd_str = slurm.interactive_shell_cmd(
            method,
            node,
            job_id=job_id,
            remote=self.config.remote,
            control_opt=self._ssh_control_opt(),
        )

        self._log(f"{method} {node}", cmd_str)
        with self.suspend():
            # Clear terminal and show greeting
            os.system("clear")
            job_info = f" (job {job_id})" if job_id else ""
            via = f" via {self.config.remote}" if self.config.remote else ""
            where = "inside the allocation on" if method == "srun" else "to"
            print(f"LazySlurm — connecting {where} {node}{via}{job_info}")
            print("Type 'exit' to return to LazySlurm.\n")
            rc = os.system(cmd_str)
        if rc and method == "srun":
            # Don't silently retry over ssh: a failed step launch and a shell
            # the user exited look the same from here, and ssh lands somewhere
            # meaningfully different from what they asked for.
            self._set_status(f"srun shell failed ({_exit_code(rc)}) — press O for ssh")
        else:
            self._log(method, f"session to {node} closed")

    # ------------------------------------------------------------------
    # Open log files in editor
    # ------------------------------------------------------------------

    async def action_view_batch_script(self) -> None:
        if self._selected_job_id is None:
            self._set_status("No job selected")
            return

        job_id = self._selected_job_id
        from lazyslurm import config as persistent_config

        was_cached = persistent_config.get_cached_script(job_id) is not None
        if not was_cached:
            self._log("batch script", f"fetching script for {job_id}...")

        path = await slurm.archive_batch_script(job_id)
        if path is None:
            msg = (
                "batch script unavailable — Slurm no longer holds this job "
                "(MinJobAge) and no copy was archived while it was live"
            )
            self._log("batch script", msg)
            self._set_status(f"No sbatch script available for job {job_id}")
            return

        self._log("batch script", f"{'cached' if was_cached else 'archived'} {path}")
        await self._open_readonly_in_editor(path, f"script {job_id}")

    async def action_edit_stdout(self) -> None:
        await self._open_in_editor(self._stdout_path, "stdout")

    async def action_edit_stderr(self) -> None:
        await self._open_in_editor(self._stderr_path, "stderr")

    async def action_edit_config(self) -> None:
        from lazyslurm.config import CONFIG_FILE, CONFIG_DIR
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_FILE.exists():
            import shutil as _shutil
            from importlib.resources import files
            template = files("lazyslurm").joinpath("templ", "config.toml")
            _shutil.copy2(str(template), str(CONFIG_FILE))
        editor = self.config.editor
        if shutil.which(editor) is None:
            self._log("edit config", f"editor '{editor}' not found")
            return
        self._log("edit config", str(CONFIG_FILE))
        with self.suspend():
            os.system(f"{shlex.quote(editor)} {shlex.quote(str(CONFIG_FILE))}")
        self._reload_config()

    def _reload_config(self) -> None:
        """Reload config from disk and apply changes live."""
        from lazyslurm import config as persistent_config
        from lazyslurm.__main__ import (
            parse_cache_max_age,
            parse_gpu_column,
            parse_interactive_shell,
            parse_node_expand,
            parse_resource_monitor,
            unknown_config_keys,
        )

        saved = persistent_config.load()
        old = self.config

        # The user has just been editing the file, so a typo made here is the
        # one worth catching immediately.
        for warning in unknown_config_keys(saved):
            self._log("config file", warning)
        shell, shell_warning = parse_interactive_shell(
            saved.get("interactive_shell", old.interactive_shell)
        )
        if shell_warning:
            self._log("config file", shell_warning)
        monitor, monitor_warning = parse_resource_monitor(
            saved.get("resource_monitor", old.resource_monitor)
        )
        if monitor_warning:
            self._log("config file", monitor_warning)
        node_expand, expand_warning = parse_node_expand(
            saved.get("node_expand", old.node_expand)
        )
        gpu_column, column_warning = parse_gpu_column(
            saved.get("gpu_column", old.gpu_column)
        )
        for warning in (expand_warning, column_warning):
            if warning:
                self._log("config file", warning)

        # Rebuild config from file, preserving CLI-only values (remote, user)
        self.config = Config(
            refresh=float(saved.get("refresh", old.refresh)),
            days=int(saved.get("days", old.days)),
            user=str(saved.get("user", old.user)),
            partition=str(saved.get("partition", old.partition)),
            no_gpu=bool(saved.get("no_gpu", old.no_gpu)),
            no_live=bool(saved.get("no_live", old.no_live)),
            remote=str(saved.get("remote", old.remote)),
            partition_order=saved.get("partition_order", old.partition_order),
            partition_colors=persistent_config.get_partition_colors() or old.partition_colors,
            editor=str(saved.get("editor", old.editor)),
            pager=str(saved.get("pager", old.pager)),
            max_name_width=int(saved.get("max_name_width", old.max_name_width)),
            max_partition_width=int(saved.get("max_partition_width", old.max_partition_width)),
            abbreviate_states=bool(saved.get("abbreviate_states", old.abbreviate_states)),
            collapse_arrays=bool(saved.get("collapse_arrays", old.collapse_arrays)),
            cache_max_age_days=parse_cache_max_age(
                saved.get("cache_max_age_days", old.cache_max_age_days)
            ),
            script_cache_dir=os.path.expanduser(
                str(saved.get("script_cache_dir", old.script_cache_dir))
            ),
            interactive_shell=shell,
            resource_monitor=monitor,
            node_expand=node_expand,
            gpu_column=gpu_column,
        )
        # An edit to the file is an explicit choice, so it wins over whatever
        # Shift+M left the session on.
        if monitor != old.resource_monitor:
            self._monitor_mode = monitor

        # Re-apply module-level settings
        slurm.set_config(self.config)
        persistent_config.set_script_cache_dir(self.config.script_cache_dir)
        set_partition_colors(self.config.partition_colors)
        set_display_config(
            max_name=self.config.max_name_width,
            max_partition=self.config.max_partition_width,
            abbreviate=self.config.abbreviate_states,
            collapse_arrays=self.config.collapse_arrays,
        )
        set_node_display(
            node_expand=self.config.node_expand,
            gpu_column=self.config.gpu_column,
        )

        # Log what changed
        changes = []
        for field in (
            "refresh", "days", "user", "partition", "no_gpu", "no_live",
            "editor", "max_name_width", "max_partition_width", "abbreviate_states",
            "partition_order", "cache_max_age_days", "script_cache_dir",
            "interactive_shell", "resource_monitor", "node_expand", "gpu_column",
        ):
            old_val = getattr(old, field)
            new_val = getattr(self.config, field)
            if old_val != new_val:
                changes.append(f"{field}: {old_val} → {new_val}")

        if old.partition_colors != self.config.partition_colors:
            changes.append("partition_colors updated")

        if changes:
            for c in changes:
                self._log("config reloaded", c)
            # Force full rebuild to recalculate column widths
            self.query_one("#active-jobs", ActiveJobTable).force_rebuild()
            self.query_one("#completed-jobs", CompletedJobTable).force_rebuild()
        else:
            self._log("config reloaded", "no changes")

    # Pagers that can jump to the end of the file, so a live log opens on its
    # newest lines. `less -R` also keeps the log's own ANSI colors.
    _PAGER_FLAGS = {
        "less": ["-R", "+G"],
        "most": ["+"],
        "more": ["+G"],
        "bat": ["--paging=always", "--style=plain"],
    }

    async def action_page_log(self) -> None:
        """Open the log of the active detail tab in a pager (stdout or stderr).

        The inline panel only ever shows the tail; this is how you read a whole
        multi-gigabyte log — the pager seeks instead of loading, and `F` inside
        `less` follows the file as the job writes to it.
        """
        tabs = self.query_one("#detail-view", DetailView).query_one("#detail-tabs")
        if tabs.active == "tab-stderr":
            path, label = self._stderr_path, "stderr"
        else:
            path, label = self._stdout_path, "stdout"
        await self._page_file(path, label)

    async def _page_file(self, path: str | None, label: str) -> None:
        if not path:
            self._log(f"page {label}", "no log file path available")
            return

        pager = self.config.pager
        flags = self._PAGER_FLAGS.get(os.path.basename(pager), [])

        if self.config.remote:
            # Run the pager *on the cluster* over the existing connection: no
            # copying a huge file down, and no second authentication.
            if shutil.which("ssh") is None:
                self._log(f"page {label}", "ssh not found")
                return
            cmd = (
                f"ssh -t {self._ssh_control_opt()} {shlex.quote(self.config.remote)} "
                + shlex.quote(
                    " ".join(shlex.quote(a) for a in [pager, *flags, path])
                )
            )
        else:
            if shutil.which(pager) is None:
                self._log(f"page {label}", f"pager '{pager}' not found — set 'pager' in config.toml")
                return
            if not os.path.isfile(path):
                self._log(f"page {label}", f"file not found: {path}")
                return
            cmd = " ".join(shlex.quote(a) for a in [pager, *flags, path])

        self._log(f"page {label}", f"{pager} {os.path.basename(path)}")
        with self.suspend():
            os.system(cmd)
        self._log(f"page {label}", "pager closed")

    async def _open_in_editor(self, path: str | None, label: str) -> None:
        if not path:
            self._log(f"edit {label}", "no log file path available")
            return

        editor = self.config.editor
        # Check editor exists
        if shutil.which(editor) is None:
            self._log(f"edit {label}", f"editor '{editor}' not found — set 'editor' in config.toml")
            return

        if self.config.remote:
            # Remote: copy file to a local temp file, open editor, clean up
            import tempfile
            self._log(f"edit {label}", f"fetching {path} from {self.config.remote}...")
            tmp = tempfile.NamedTemporaryFile(
                suffix=f"_{os.path.basename(path)}",
                prefix="lazyslurm_",
                delete=False,
            )
            tmp.close()
            # scp over the session's control socket — no second authentication.
            # A remote scp path passes through *two* shells, the local one and
            # the one scp starts on the far side, so it needs quoting twice:
            # one round leaves "/work/my runs/x.out" to be re-split remotely.
            rc = os.system(
                f"scp -q {self._ssh_control_opt()} "
                f"{shlex.quote(self.config.remote)}:{shlex.quote(shlex.quote(path))} "
                f"{shlex.quote(tmp.name)}"
            )
            if rc != 0:
                self._log(f"edit {label}", f"failed to fetch remote file: {path}")
                os.unlink(tmp.name)
                return
            local_path = tmp.name
        else:
            if not os.path.isfile(path):
                self._log(f"edit {label}", f"file not found: {path}")
                return
            local_path = path

        self._log(f"edit {label}", f"{editor} {os.path.basename(path)}")
        with self.suspend():
            os.system(f"{shlex.quote(editor)} {shlex.quote(local_path)}")

        # Clean up temp file for remote mode
        if self.config.remote and local_path != path:
            try:
                os.unlink(local_path)
            except OSError:
                pass

        self._log(f"edit {label}", "editor closed")

    # Read-only flag per editor. vim gets -R rather than -M on purpose: -R still
    # allows ":w /somewhere/else", which is the "save it elsewhere" workflow.
    _READONLY_FLAGS = {
        "vim": ["-R"], "nvim": ["-R"], "vi": ["-R"], "view": ["-R"], "gvim": ["-R"],
        "nano": ["-v"],
        "less": [], "more": [], "bat": [], "most": [],
    }

    async def _open_readonly_in_editor(self, path: str | Path, label: str) -> None:
        """Open a local file read-only. Unlike _open_in_editor, no scp step:
        archived scripts are always local (their text arrived over ssh stdout).
        """
        editor = self.config.editor
        if shutil.which(editor) is None:
            self._log(label, f"editor '{editor}' not found — set 'editor' in config.toml")
            return

        name = os.path.basename(editor)
        if name in self._READONLY_FLAGS:
            flags = self._READONLY_FLAGS[name]
        else:
            flags = []
            self._log(label, f"'{editor}' has no known read-only flag — opening writable")

        self._log(label, f"{editor} {os.path.basename(str(path))}")
        with self.suspend():
            os.system(" ".join(shlex.quote(a) for a in [editor, *flags, str(path)]))
        self._log(label, "editor closed")

    async def action_refresh(self) -> None:
        self._log("refresh")
        # In remote mode a failed or dropped session is retried here — the
        # user's only way back after cancelling or mistyping a 2FA code.
        if self.config.remote and slurm.get_session() is None:
            await self._start_remote_session()
            return
        # `force` here means the cluster bar, whose cache is a plain TTL and
        # can genuinely be stale. The completed-job list is not forced: its
        # incremental query already re-reads everything that could have
        # changed, so a full re-read would cost a second on a wide window to
        # return the same rows -- and with `refresh = 0` this is the *only*
        # path, which is exactly the one worth keeping fast.
        await self._poll_jobs(force=True)
        if self._selected_job_id:
            await self._load_job_details(self._selected_job_id)
            # Only the tab being looked at, the way the auto-refresh already
            # does it. Sampling both cost a second round trip -- and, in remote
            # mode, a serialized one -- to update a panel that is not on screen.
            if not self.config.no_live:
                await self._refresh_live_monitors()
        self._log("refresh", "complete")

    def _log(self, action: str, result: str = "") -> None:
        """Write a timestamped entry to the command log panel."""
        log = self.query_one("#command-log", RichLog)
        ts = datetime.now().strftime("%H:%M:%S")
        log.write(f"[dim]{ts}[/] {action}")
        if result:
            log.write(f"  [dim]>>> {result}[/]")

    # How long a status message stays on the bar above the key bar.
    _STATUS_SECONDS = 5.0

    def _set_status(self, text: str) -> None:
        """Show a user-facing message on the status line and log it.

        Unlike `_log`, which traces commands, this is for things the user is
        waiting to be told — a refusal, a result. The command log scrolls; the
        status line holds the message where the eye already is.
        """
        if not text:
            return
        self._log(text)
        try:
            status = self.query_one("#status-line", Static)
        except NoMatches:
            return  # called before mount (or from a screen without the bar)
        status.update(text)
        if self._status_timer is not None:
            self._status_timer.stop()
        self._status_timer = self.set_timer(
            self._STATUS_SECONDS, lambda: status.update(""),
        )


    # Tab cycles through the three panels a user acts in. The job tables are
    # part of the cycle: without them, focus that reached the right-hand panels
    # could only be brought back with the mouse.
    _FOCUS_CYCLE = ("jobs", "detail", "metadata")

    def _focus_panel(self, panel: str) -> None:
        if panel == "jobs":
            self._focus_a_job_table()
            return
        view = self.query_one("#detail-view" if panel == "detail" else "#metadata-view")
        # TabbedContent itself is not focusable — focusing it was a no-op, and
        # the panel only ever got focus because Textual's own Tab binding moved
        # it. The first focusable descendant is the tab strip, which is what
        # [ / ] and ( / ) then act on.
        target = next((node for node in view.query("*") if node.focusable), None)
        if target is not None:
            target.focus()
        self._right_focus = panel

    def _cycle_focus(self, step: int) -> None:
        current = self._right_focus if self._right_focus in self._FOCUS_CYCLE else "jobs"
        # Real focus wins when it is on a table: the user may have clicked, or
        # arrived there some other way. (_focused_job_table falls back to a
        # table when neither has focus, so it cannot answer this question.)
        active = self.query_one("#active-jobs", ActiveJobTable)
        completed = self.query_one("#completed-jobs", CompletedJobTable)
        if active.has_focus or completed.has_focus:
            current = "jobs"
        index = self._FOCUS_CYCLE.index(current)
        self._focus_panel(self._FOCUS_CYCLE[(index + step) % len(self._FOCUS_CYCLE)])

    def action_focus_next_right(self) -> None:
        self._cycle_focus(1)

    def action_focus_prev_right(self) -> None:
        self._cycle_focus(-1)

    def action_next_detail_tab(self) -> None:
        self.query_one("#detail-view", DetailView).switch_tab(1)

    def action_prev_detail_tab(self) -> None:
        self.query_one("#detail-view", DetailView).switch_tab(-1)

    def action_next_meta_tab(self) -> None:
        self.query_one("#metadata-view", MetadataView).switch_tab(1)

    def action_prev_meta_tab(self) -> None:
        self.query_one("#metadata-view", MetadataView).switch_tab(-1)
