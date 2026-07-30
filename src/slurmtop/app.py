"""Main SlurmTop Textual application."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from datetime import datetime

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Input, RichLog, Static

from slurmtop import slurm
from slurmtop.daemon import CacheThread, is_daemon_running
from slurmtop.models import Config
from slurmtop.widgets.detail_view import DetailView, parse_mem_bytes
from slurmtop.widgets.job_table import ActiveJobTable, CompletedJobTable, JobSelected, set_partition_colors, set_display_config
from slurmtop.widgets.metadata_view import MetadataView


# ---------------------------------------------------------------------------
# Modal screens
# ---------------------------------------------------------------------------


class HelpScreen(ModalScreen[None]):
    """Overlay showing key bindings."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("question_mark", "dismiss", "Close"),
    ]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen > Vertical {
        width: 72;
        height: auto;
        max-height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                "[bold underline]SlurmTop Help[/]\n\n"
                "[bold]Navigation[/]\n"
                "  [bold cyan]Up / Down[/]       Navigate job list (wraps between panels)\n"
                "  [bold cyan]Tab / Shift+Tab[/] Switch right panel focus\n"
                "  [bold cyan]Left / Right[/]    Switch right panel focus\n"
                "  [bold cyan]\\[ / \\][/]          Switch Job Details tabs\n"
                "  [bold cyan]( / )[/]           Switch Job Metadata tabs\n"
                "  [bold cyan]Escape[/]          Close search bar\n\n"
                "[bold]Actions[/]\n"
                "  [bold cyan]/[/]               Search / filter jobs by ID, name, or partition\n"
                "  [bold cyan]m[/]               Bookmark / unbookmark job (★ pinned to top)\n"
                "  [bold cyan]c[/]               Cancel selected job(s) (with confirmation)\n"
                "  [bold cyan]Shift+C[/]         Force cancel job(s) (SIGKILL, no confirmation)\n"
                "  [bold cyan]Ctrl+V[/]          Toggle multi-select mode (vim-visual)\n"
                "  [bold cyan]s[/]               Resubmit terminated job (with confirmation)\n"
                "  [bold cyan]e[/]               Open stdout in editor (vim/nano, set in config)\n"
                "  [bold cyan]Shift+E[/]         Open stderr in editor\n"
                "  [bold cyan]o[/]               SSH to job's compute node (suspends TUI)\n"
                "  [bold cyan],[/]               Edit config file (~/.config/slurmtop/config.toml)\n"
                "  [bold cyan]r[/]               Force refresh all data\n"
                "  [bold cyan]?[/]               Toggle this help screen\n"
                "  [bold cyan]q[/]               Quit\n\n"
                "[bold]Detail Tabs[/]\n"
                "  [bold cyan]stdout[/]  Job standard output log\n"
                "  [bold cyan]stderr[/]  Job standard error log\n"
                "  [bold cyan]cpu[/]     Live process list from node (auto-refreshes)\n"
                "  [bold cyan]gpu[/]     Live nvidia-smi from node (auto-refreshes)\n"
                "  [bold cyan]stats[/]   CPU, memory, GPU, disk I/O + sparkline history\n\n"
                "[bold]Cluster Bar (top line)[/]\n"
                "  Shows your running/pending job counts and partition status.\n"
                "  Partition format: [bold]name[/]:[bold]A[/]/[bold]I[/]/[bold]O[/]/[bold]T[/]\n"
                "    [bold]A[/]=allocated  [bold]I[/]=idle  [bold]O[/]=other  [bold]T[/]=total nodes\n\n"
                "[bold]Notifications[/]\n"
                "  Terminal bell + desktop notification when a running job completes.\n\n"
                "Press [bold]?[/] or [bold]Escape[/] to close."
            )


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


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------


_MAX_HISTORY = 60  # max sparkline samples per job


class SlurmTopApp(App):
    """SlurmTop — a TUI for monitoring Slurm jobs."""

    TITLE = "SlurmTop"
    CSS_PATH = "slurmtop.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("question_mark", "help", "Help", show=True, key_display="?"),
        Binding("slash", "toggle_search", "Search", show=True, key_display="/"),
        Binding("m", "toggle_bookmark", "Bookmark", show=True),
        Binding("c", "cancel_job", "Cancel", show=True),
        Binding("shift+c", "force_cancel_job", "Force Cancel", show=False),
        Binding("ctrl+v", "toggle_multiselect", "Multi-select", show=True),
        Binding("s", "resubmit_job", "Resubmit", show=True),
        Binding("o", "ssh_to_node", "SSH", show=True),
        Binding("e", "edit_stdout", "Edit Out", show=True),
        Binding("shift+e", "edit_stderr", "Edit Err", show=False),
        Binding("comma", "edit_config", "Config", show=True, key_display=","),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("tab", "focus_next_right", "Next Panel", show=False),
        Binding("shift+tab", "focus_prev_right", "Prev Panel", show=False),
        Binding("left", "focus_prev_right", show=False),
        Binding("right", "focus_next_right", show=False),
        Binding("left_square_bracket", "prev_detail_tab", show=False),
        Binding("right_square_bracket", "next_detail_tab", show=False),
        Binding("left_parenthesis", "prev_meta_tab", show=False),
        Binding("right_parenthesis", "next_meta_tab", show=False),
    ]

    def __init__(self, config: Config | None = None, config_overrides: list[str] | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.config = config or Config()
        self._config_overrides = config_overrides or []
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
        # Resubmit state
        self._resubmit_script: str = ""
        self._resubmit_work_dir: str = ""
        # Current job log paths (for editor)
        self._stdout_path: str | None = None
        self._stderr_path: str | None = None
        # Multi-select state
        self._multiselect_mode: bool = False
        self._multiselect_table: str = ""  # "active" or "completed"
        self._multiselect_anchor: str | None = None  # job_id where visual mode started
        self._multiselect_ids: set[str] = set()
        # Log path cache thread
        self._cache_thread: CacheThread | None = None

    def compose(self) -> ComposeResult:
        show_gpu = not self.config.no_gpu and not self.config.no_live
        yield Static(id="cluster-bar")
        with Horizontal(id="main-container"):
            with Vertical(id="left-column"):
                yield Input(
                    placeholder="Filter by ID, name, partition...",
                    id="search-input",
                )
                yield ActiveJobTable(id="active-jobs")
                yield CompletedJobTable(id="completed-jobs")
            with Vertical(id="right-column"):
                yield DetailView(id="detail-view", show_gpu=show_gpu)
                yield MetadataView(id="metadata-view")
                yield RichLog(id="command-log", wrap=True, markup=True)
        yield Footer()

    def on_mount(self) -> None:
        slurm.set_config(self.config)
        set_partition_colors(self.config.partition_colors)
        set_display_config(
            max_name=self.config.max_name_width,
            max_partition=self.config.max_partition_width,
            abbreviate=self.config.abbreviate_states,
        )

        # Prune old log path cache entries
        from slurmtop import config as persistent_config
        persistent_config.prune_log_cache(max_age_days=self.config.cache_max_age_days)

        self.query_one("#active-jobs").border_title = "Active Jobs"
        self.query_one("#completed-jobs").border_title = "Terminated Jobs"
        self.query_one("#detail-view").border_title = "Job Details"
        self.query_one("#metadata-view").border_title = "Job Metadata"
        self.query_one("#command-log").border_title = "Command Log"

        # Hide search input initially
        self.query_one("#search-input").display = False

        if self.config.remote:
            self.title = f"SlurmTop [{self.config.remote}]"

        self.query_one("#active-jobs", ActiveJobTable).focus()

        # Log config overrides
        for override in self._config_overrides:
            self._log("config override", override)

        # Start log path caching (daemon or thread)
        if is_daemon_running():
            self._log("log cache", "daemon is running, using external daemon")
        else:
            self._cache_thread = CacheThread(
                user=self.config.user,
                remote=self.config.remote,
            )
            self._cache_thread.start()
            self._log("log cache", "started background thread")

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

        # Polling (refresh=0 disables auto-refresh)
        self.call_after_refresh(self._poll_jobs)
        if self.config.refresh > 0:
            self.set_interval(self.config.refresh, self._poll_jobs)
            if not self.config.no_live:
                self.set_interval(self.config.refresh, self._refresh_live_monitors)
        else:
            self._log("auto-refresh", "disabled (refresh=0)")

    def on_unmount(self) -> None:
        if self._cache_thread and self._cache_thread.running:
            self._cache_thread.stop()

    # ------------------------------------------------------------------
    # Data polling
    # ------------------------------------------------------------------

    async def _poll_jobs(self) -> None:
        active_table = self.query_one("#active-jobs", ActiveJobTable)
        completed_table = self.query_one("#completed-jobs", CompletedJobTable)

        running, completed, part_info = await asyncio.gather(
            slurm.get_running_jobs(self.config),
            slurm.get_completed_jobs(self.config),
            slurm.get_partition_availability(self.config),
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
            "SlurmTop", f"Job {job_id} {state}",
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
        except Exception:
            pass

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
        # Parse CPU: try total_cpu as seconds-like value, or just count samples
        # For sparklines, use max_rss as primary metric; CPU is harder to normalize
        # Use ave_rss as a proxy for CPU activity (non-zero means active)
        cpu_val = parse_mem_bytes(stats.ave_rss)
        if cpu_val is not None:
            hist["cpu"].append(cpu_val)
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

        # Load logs and stats — but NOT live CPU/GPU on selection.
        # Live monitors are loaded lazily by _refresh_live_monitors when
        # the user views those tabs, avoiding expensive SSH/srun calls.
        stdout_content, stderr_content, stats = await asyncio.gather(
            slurm.read_log_file(detail.stdout_path),
            slurm.read_log_file(detail.stderr_path),
            slurm.get_job_stats(job_id),
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
        except Exception:
            pass
        metadata_view.load_detail(detail)

    # ------------------------------------------------------------------
    # Live CPU/GPU auto-refresh
    # ------------------------------------------------------------------

    async def _refresh_live_monitors(self) -> None:
        if not self._selected_node or self._selected_node in ("N/A", "None", "(null)"):
            return

        detail_view = self.query_one("#detail-view", DetailView)
        tabs = detail_view.query_one("#detail-tabs")
        active_tab = tabs.active

        if active_tab == "tab-cpu":
            content = await slurm.get_node_processes(self._selected_node, self.config.user)
            detail_view.load_cpu(content)
        elif active_tab == "tab-gpu" and not self.config.no_gpu:
            content = await slurm.get_gpu_status(self._selected_node, self._selected_job_id or "")
            detail_view.load_gpu(content)

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

    def _apply_filter(self, text: str) -> None:
        self.query_one("#active-jobs", ActiveJobTable).apply_filter(text)
        self.query_one("#completed-jobs", CompletedJobTable).apply_filter(text)

    # ------------------------------------------------------------------
    # Bookmarks
    # ------------------------------------------------------------------

    def action_toggle_bookmark(self) -> None:
        if self._selected_job_id is None:
            return
        if self._selected_job_id in self._bookmarked_ids:
            self._bookmarked_ids.discard(self._selected_job_id)
        else:
            self._bookmarked_ids.add(self._selected_job_id)
        self.query_one("#active-jobs", ActiveJobTable).set_bookmarks(self._bookmarked_ids)
        self.query_one("#completed-jobs", CompletedJobTable).set_bookmarks(self._bookmarked_ids)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_help(self) -> None:
        if self._help_open:
            self.app.pop_screen()
            self._help_open = False
        else:
            self._help_open = True
            self.push_screen(HelpScreen(), callback=self._on_help_dismissed)

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

        if self._selected_job_id is None:
            self._set_status("No job selected")
            return
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

        if self._selected_job_id is None:
            self._set_status("No job selected")
            return
        self._log(f"scancel --signal=KILL {self._selected_job_id}")
        success, msg = await slurm.cancel_job(self._selected_job_id, force=True)
        self._log("force cancel", msg)
        if success:
            await self._poll_jobs()

    async def _on_cancel_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed or self._selected_job_id is None:
            return
        self._log(f"scancel {self._selected_job_id}")
        success, msg = await slurm.cancel_job(self._selected_job_id)
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

        self._resubmit_script = script
        self._resubmit_work_dir = detail.work_dir
        self.push_screen(
            ConfirmResubmitScreen(self._selected_job_id, script),
            callback=self._on_resubmit_confirmed,
        )

    async def _on_resubmit_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            return
        self._log(f"sbatch {self._resubmit_script}")
        success, msg = await slurm.resubmit_job(
            self._resubmit_script, self._resubmit_work_dir,
        )
        self._log("resubmit", msg)
        if success:
            await self._poll_jobs()

    async def action_ssh_to_node(self) -> None:
        if not self._selected_node or self._selected_node in ("N/A", "None", "(null)", ""):
            self._log("ssh", "no node assigned to this job")
            return

        node = slurm._first_node(self._selected_node)
        cmd_parts = ["ssh"]
        if self.config.remote:
            cmd_parts.extend(["-J", self.config.remote])
        cmd_parts.append(node)
        cmd_str = " ".join(cmd_parts)

        self._log(f"ssh {node}")
        with self.suspend():
            # Clear terminal and show greeting
            os.system("clear")
            job_info = f" (job {self._selected_job_id})" if self._selected_job_id else ""
            via = f" via {self.config.remote}" if self.config.remote else ""
            print(f"SlurmTop — connecting to {node}{via}{job_info}")
            print(f"Type 'exit' to return to SlurmTop.\n")
            os.system(cmd_str)
        self._log("ssh", f"session to {node} closed")

    # ------------------------------------------------------------------
    # Open log files in editor
    # ------------------------------------------------------------------

    async def action_edit_stdout(self) -> None:
        await self._open_in_editor(self._stdout_path, "stdout")

    async def action_edit_stderr(self) -> None:
        await self._open_in_editor(self._stderr_path, "stderr")

    async def action_edit_config(self) -> None:
        from slurmtop.config import CONFIG_FILE, CONFIG_DIR
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_FILE.exists():
            import shutil as _shutil
            from importlib.resources import files
            template = files("slurmtop").joinpath("templ", "config.toml")
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
        from slurmtop import config as persistent_config

        saved = persistent_config.load()
        old = self.config

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
            max_name_width=int(saved.get("max_name_width", old.max_name_width)),
            max_partition_width=int(saved.get("max_partition_width", old.max_partition_width)),
            abbreviate_states=bool(saved.get("abbreviate_states", old.abbreviate_states)),
            cache_max_age_days=saved.get("cache_max_age_days", old.cache_max_age_days),
        )

        # Re-apply module-level settings
        slurm.set_config(self.config)
        set_partition_colors(self.config.partition_colors)
        set_display_config(
            max_name=self.config.max_name_width,
            max_partition=self.config.max_partition_width,
            abbreviate=self.config.abbreviate_states,
        )

        # Log what changed
        changes = []
        for field in (
            "refresh", "days", "user", "partition", "no_gpu", "no_live",
            "editor", "max_name_width", "max_partition_width", "abbreviate_states",
            "partition_order",
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
                prefix="slurmtop_",
                delete=False,
            )
            tmp.close()
            # scp the file
            rc = os.system(f"scp -q {self.config.remote}:{shlex.quote(path)} {shlex.quote(tmp.name)}")
            if rc != 0:
                self._log(f"edit {label}", f"failed to fetch remote file")
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

    async def action_refresh(self) -> None:
        self._log("refresh")
        await self._poll_jobs()
        if self._selected_job_id:
            await self._load_job_details(self._selected_job_id)
            # Also refresh live monitors on explicit refresh
            if not self.config.no_live and self._selected_node:
                detail_view = self.query_one("#detail-view", DetailView)
                cpu_content, gpu_content = await asyncio.gather(
                    slurm.get_node_processes(self._selected_node, self.config.user),
                    slurm.get_gpu_status(self._selected_node, self._selected_job_id or "")
                    if not self.config.no_gpu else asyncio.sleep(0),
                )
                detail_view.load_cpu(cpu_content)
                if not self.config.no_gpu and isinstance(gpu_content, str):
                    detail_view.load_gpu(gpu_content)
        self._log("refresh", "complete")

    def _log(self, action: str, result: str = "") -> None:
        """Write a timestamped entry to the command log panel."""
        log = self.query_one("#command-log", RichLog)
        ts = datetime.now().strftime("%H:%M:%S")
        log.write(f"[dim]{ts}[/] {action}")
        if result:
            log.write(f"  [dim]>>> {result}[/]")

    def _set_status(self, text: str) -> None:
        """Log a message to the command log panel."""
        if text:
            self._log(text)
        # Also update the footer subtitle for one-line visibility


    def action_focus_next_right(self) -> None:
        detail = self.query_one("#detail-view", DetailView)
        metadata = self.query_one("#metadata-view", MetadataView)
        if self._right_focus == "detail":
            self._right_focus = "metadata"
            metadata.query_one("TabbedContent").focus()
        else:
            self._right_focus = "detail"
            detail.query_one("TabbedContent").focus()

    def action_focus_prev_right(self) -> None:
        self.action_focus_next_right()

    def action_next_detail_tab(self) -> None:
        self.query_one("#detail-view", DetailView).switch_tab(1)

    def action_prev_detail_tab(self) -> None:
        self.query_one("#detail-view", DetailView).switch_tab(-1)

    def action_next_meta_tab(self) -> None:
        self.query_one("#metadata-view", MetadataView).switch_tab(1)

    def action_prev_meta_tab(self) -> None:
        self.query_one("#metadata-view", MetadataView).switch_tab(-1)
