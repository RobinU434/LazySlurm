"""Lower-right panel: job metadata and sbatch options in tabbed view."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static, TabbedContent, TabPane

from lazyslurm.models import JobDetail, PriorityInfo
from lazyslurm.slurm import explain_reason, format_start_estimate

# The Pending tab only exists while the selected job is pending, so the tab
# strip stays the same for every other job.
_BASE_TABS = ["tab-resources", "tab-submission", "tab-raw"]
_PENDING_TAB = "tab-pending"

_BAR_WIDTH = 20


def priority_bar(value: int, total: int, width: int = _BAR_WIDTH) -> str:
    """`████████░░░░` — one factor's share of the total priority."""
    if total <= 0 or value <= 0:
        return ""
    filled = max(1, round(width * value / total))
    return "█" * min(filled, width) + "░" * max(0, width - filled)


class MetadataView(Vertical):
    """Tabbed view showing job resources, submission info, and raw details."""

    DEFAULT_CSS = """
    MetadataView {
        height: 1fr;
    }
    MetadataView VerticalScroll {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        with TabbedContent(id="meta-tabs"):
            with TabPane("Resources", id="tab-resources"):
                yield VerticalScroll(Static(id="meta-resources", expand=True))
            with TabPane("Submission", id="tab-submission"):
                yield VerticalScroll(Static(id="meta-submission", expand=True))
            with TabPane("Pending", id=_PENDING_TAB):
                yield VerticalScroll(Static(id="meta-pending", expand=True))
            with TabPane("Raw", id="tab-raw"):
                yield VerticalScroll(Static(id="meta-raw", expand=True))

    def on_mount(self) -> None:
        self._show_pending_tab(False)

    # ------------------------------------------------------------------

    def _show_pending_tab(self, visible: bool) -> None:
        tabs = self.query_one("#meta-tabs", TabbedContent)
        try:
            if visible:
                tabs.show_tab(_PENDING_TAB)
            else:
                if tabs.active == _PENDING_TAB:
                    tabs.active = "tab-resources"
                tabs.hide_tab(_PENDING_TAB)
        except Exception:
            pass

    def _visible_tabs(self) -> list[str]:
        tabs = self.query_one("#meta-tabs", TabbedContent)
        order = ["tab-resources", "tab-submission", _PENDING_TAB, "tab-raw"]
        visible = []
        for tab_id in order:
            # Only the tab in the strip says anything about visibility: an
            # inactive TabPane always has display=False, hidden or not.
            try:
                if tabs.get_tab(tab_id).display:
                    visible.append(tab_id)
            except Exception:
                continue
        return visible or _BASE_TABS

    def load_detail(
        self,
        detail: JobDetail | None,
        priority: PriorityInfo | None = None,
        priority_available: bool = True,
    ) -> None:
        if detail is None:
            for sid in ("#meta-resources", "#meta-submission", "#meta-raw", "#meta-pending"):
                self.query_one(sid, Static).update("No job selected")
            self._show_pending_tab(False)
            return

        # Resources tab
        self.query_one("#meta-resources", Static).update(
            f"[bold]State:[/]      {detail.state}\n"
            f"[bold]Partition:[/]   {detail.partition}\n"
            f"[bold]Nodes:[/]      {detail.num_nodes}\n"
            f"[bold]CPUs:[/]       {detail.num_cpus}\n"
            f"[bold]Memory:[/]     {detail.memory}\n"
            f"[bold]GPU/GRES:[/]   {detail.gres}\n"
            f"[bold]TRES:[/]       {detail.tres}\n"
            f"[bold]Node List:[/]  {detail.node_list}\n"
            f"[bold]Time Limit:[/] {detail.time_limit}\n"
            f"[bold]Run Time:[/]   {detail.run_time}\n"
            f"[bold]Account:[/]    {detail.account}\n"
            f"[bold]QoS:[/]        {detail.qos}"
        )

        # Submission tab
        self.query_one("#meta-submission", Static).update(
            f"[bold]Submit Time:[/] {detail.submit_time}\n"
            f"[bold]Start Time:[/]  {detail.start_time}\n"
            f"[bold]End Time:[/]    {detail.end_time}\n"
            f"[bold]Work Dir:[/]    {detail.work_dir}\n"
            f"[bold]StdOut:[/]      {detail.stdout_path or 'N/A'}\n"
            f"[bold]StdErr:[/]      {detail.stderr_path or 'N/A'}\n"
            f"[bold]Command:[/]     {detail.submit_line}"
        )

        # Pending tab — only for jobs that are actually waiting
        pending = detail.state.upper().startswith("PENDING")
        if pending:
            self.query_one("#meta-pending", Static).update(
                self._pending_report(detail, priority, priority_available)
            )
        self._show_pending_tab(pending)

        # Raw tab
        raw_text = "\n".join(f"[bold]{k}:[/] {v}" for k, v in sorted(detail.raw.items()))
        self.query_one("#meta-raw", Static).update(raw_text or "No raw data")

    def _pending_report(
        self,
        detail: JobDetail,
        priority: PriorityInfo | None,
        priority_available: bool,
    ) -> str:
        reason_code = (detail.raw.get("Reason") or "").strip() or "N/A"
        why = explain_reason(reason_code, detail.raw, priority)
        starts = format_start_estimate(detail.raw.get("StartTime", ""))

        lines = [
            f"[bold]Why:[/]      {why}",
            f"[dim]          reason code: {reason_code}[/]",
            f"[bold]Starts:[/]   {starts}",
            "",
        ]

        if priority is None:
            lines.append(
                "[dim]Priority breakdown unavailable — sprio reported nothing for this "
                "job.[/]" if priority_available else
                "[dim]Priority breakdown unavailable — this cluster does not run sprio "
                "(priority accounting is off).[/]"
            )
            return "\n".join(lines)

        lines.append(
            f"[bold]Queue:[/]    #{priority.rank} of {priority.queued} pending "
            f"in {detail.partition}"
        )
        lines.append(f"[bold]Priority:[/] {priority.total}")
        factors = priority.factors
        if not factors:
            lines.append("[dim]  all priority factors are zero[/]")
            return "\n".join(lines)

        width = max(len(name) for name, _ in factors)
        for name, value in factors:
            share = value / priority.total * 100 if priority.total else 0
            bar = priority_bar(value, priority.total)
            lines.append(
                f"  {name:<{width}}  [cyan]{bar}[/] {value:>8}  {share:4.0f}%"
            )
        return "\n".join(lines)

    def switch_tab(self, direction: int) -> None:
        """Switch tab by direction (-1 = left, +1 = right), skipping hidden tabs."""
        tabs = self.query_one("#meta-tabs", TabbedContent)
        tab_ids = self._visible_tabs()
        current = tabs.active
        if current in tab_ids:
            idx = tab_ids.index(current)
            tabs.active = tab_ids[(idx + direction) % len(tab_ids)]

    def clear_all(self) -> None:
        for sid in ("#meta-resources", "#meta-submission", "#meta-raw", "#meta-pending"):
            self.query_one(sid, Static).update("")
        self._show_pending_tab(False)
