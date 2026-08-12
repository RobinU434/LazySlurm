"""Lower-right panel: job metadata and sbatch options in tabbed view."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static, TabbedContent, TabPane

from lazyslurm.models import JobDetail


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
            with TabPane("Raw", id="tab-raw"):
                yield VerticalScroll(Static(id="meta-raw", expand=True))

    def load_detail(self, detail: JobDetail | None) -> None:
        if detail is None:
            for sid in ("#meta-resources", "#meta-submission", "#meta-raw"):
                self.query_one(sid, Static).update("No job selected")
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

        # Raw tab
        raw_text = "\n".join(f"[bold]{k}:[/] {v}" for k, v in sorted(detail.raw.items()))
        self.query_one("#meta-raw", Static).update(raw_text or "No raw data")

    def switch_tab(self, direction: int) -> None:
        """Switch tab by direction (-1 = left, +1 = right)."""
        tabs = self.query_one("#meta-tabs", TabbedContent)
        tab_ids = ["tab-resources", "tab-submission", "tab-raw"]
        current = tabs.active
        if current in tab_ids:
            idx = tab_ids.index(current)
            new_idx = (idx + direction) % len(tab_ids)
            tabs.active = tab_ids[new_idx]

    def clear_all(self) -> None:
        for sid in ("#meta-resources", "#meta-submission", "#meta-raw"):
            self.query_one(sid, Static).update("")
