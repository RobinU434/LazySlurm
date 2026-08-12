"""Upper-right panel: job output/error logs, live CPU/GPU, and stats."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static, TabbedContent, TabPane, RichLog

from lazyslurm.models import JobStats

_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float]) -> str:
    """Render a sparkline from a list of numeric values."""
    if not values:
        return ""
    mx = max(values)
    if mx == 0:
        return "▁" * len(values)
    return "".join(_SPARK_CHARS[min(int(v / mx * 7), 7)] for v in values)


def parse_mem_bytes(s: str) -> float | None:
    """Parse a memory string like '1234K', '512M', '2.5G' to bytes."""
    if not s or s == "N/A":
        return None
    s = s.strip()
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    if s[-1].upper() in multipliers:
        try:
            return float(s[:-1]) * multipliers[s[-1].upper()]
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


class DetailView(Vertical):
    """Tabbed view showing stdout, stderr, live CPU/GPU, and accounting stats."""

    DEFAULT_CSS = """
    DetailView {
        height: 1fr;
    }
    DetailView RichLog {
        height: 1fr;
    }
    """

    def __init__(self, *args, show_gpu: bool = True, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._show_gpu = show_gpu

    def compose(self) -> ComposeResult:
        with TabbedContent(id="detail-tabs"):
            with TabPane("stdout", id="tab-stdout"):
                yield RichLog(id="log-stdout", wrap=True, highlight=True)
            with TabPane("stderr", id="tab-stderr"):
                yield RichLog(id="log-stderr", wrap=True, highlight=True)
            with TabPane("cpu", id="tab-cpu"):
                yield VerticalScroll(Static(id="cpu-content", expand=True))
            if self._show_gpu:
                with TabPane("gpu", id="tab-gpu"):
                    yield VerticalScroll(Static(id="gpu-content", expand=True))
            with TabPane("stats", id="tab-stats"):
                yield VerticalScroll(Static(id="stats-content", expand=True))

    @property
    def _tab_ids(self) -> list[str]:
        ids = ["tab-stdout", "tab-stderr", "tab-cpu"]
        if self._show_gpu:
            ids.append("tab-gpu")
        ids.append("tab-stats")
        return ids

    def load_stdout(self, content: str) -> None:
        log = self.query_one("#log-stdout", RichLog)
        log.clear()
        log.write(content)

    def load_stderr(self, content: str) -> None:
        log = self.query_one("#log-stderr", RichLog)
        log.clear()
        log.write(content)

    def load_cpu(self, content: str) -> None:
        self.query_one("#cpu-content", Static).update(content)

    def load_gpu(self, content: str) -> None:
        try:
            self.query_one("#gpu-content", Static).update(content)
        except Exception:
            pass  # GPU tab not present

    def load_stats(
        self,
        stats: JobStats | None,
        history: dict[str, list[float]] | None = None,
    ) -> None:
        widget = self.query_one("#stats-content", Static)
        if stats is None:
            widget.update("[dim]No stats available[/]")
            return

        sections: list[str] = []

        # CPU section
        cpu_lines = ["[bold underline]CPU[/]"]
        if stats.ave_cpu != "N/A":
            cpu_lines.append(f"  Avg CPU Time:  {stats.ave_cpu}")
        if stats.total_cpu != "N/A":
            cpu_lines.append(f"  Total CPU:     {stats.total_cpu}")
        if stats.ave_cpu_freq != "N/A":
            cpu_lines.append(f"  Avg Frequency: {stats.ave_cpu_freq}")
        if stats.elapsed != "N/A":
            cpu_lines.append(f"  Wall Time:     {stats.elapsed}")
        sections.append("\n".join(cpu_lines))

        # Memory section
        mem_lines = ["[bold underline]Memory[/]"]
        if stats.req_mem != "N/A":
            mem_lines.append(f"  Requested:     {stats.req_mem}")
        if stats.max_rss != "N/A":
            mem_lines.append(f"  Max RSS:       {stats.max_rss}")
        if stats.ave_rss != "N/A":
            mem_lines.append(f"  Avg RSS:       {stats.ave_rss}")
        if stats.max_vm_size != "N/A":
            mem_lines.append(f"  Max VM Size:   {stats.max_vm_size}")
        if stats.ave_vm_size != "N/A":
            mem_lines.append(f"  Avg VM Size:   {stats.ave_vm_size}")
        if stats.max_rss_node != "N/A":
            mem_lines.append(f"  Max RSS Node:  {stats.max_rss_node}")
        if stats.max_rss_task != "N/A":
            mem_lines.append(f"  Max RSS Task:  {stats.max_rss_task}")
        sections.append("\n".join(mem_lines))

        # GPU section
        if stats.gpu_alloc != "N/A":
            gpu_lines = ["[bold underline]GPU[/]"]
            gpu_lines.append(f"  Allocated:     {stats.gpu_alloc}")
            if stats.gpu_tres != "N/A":
                gpu_lines.append(f"  TRES:          {stats.gpu_tres}")
            sections.append("\n".join(gpu_lines))

        # Disk I/O section
        io_lines = ["[bold underline]Disk I/O[/]"]
        if stats.ave_disk_read != "N/A":
            io_lines.append(f"  Avg Read:      {stats.ave_disk_read}")
        if stats.max_disk_read != "N/A":
            io_lines.append(f"  Max Read:      {stats.max_disk_read}")
        if stats.ave_disk_write != "N/A":
            io_lines.append(f"  Avg Write:     {stats.ave_disk_write}")
        if stats.max_disk_write != "N/A":
            io_lines.append(f"  Max Write:     {stats.max_disk_write}")
        if len(io_lines) > 1:
            sections.append("\n".join(io_lines))

        # Sparkline history
        if history:
            hist_lines = ["[bold underline]Resource History[/]"]
            if "memory" in history and history["memory"]:
                hist_lines.append(f"  Memory: {sparkline(history['memory'])}  ({len(history['memory'])} samples)")
            if "cpu" in history and history["cpu"]:
                hist_lines.append(f"  CPU:    {sparkline(history['cpu'])}  ({len(history['cpu'])} samples)")
            if len(hist_lines) > 1:
                sections.append("\n".join(hist_lines))

        widget.update("\n\n".join(sections) + f"\n\n[dim]Source: {stats.source}[/]")

    def switch_tab(self, direction: int) -> None:
        """Switch tab by direction (-1 = left, +1 = right)."""
        tabs = self.query_one("#detail-tabs", TabbedContent)
        tab_ids = self._tab_ids
        current = tabs.active
        if current in tab_ids:
            idx = tab_ids.index(current)
            new_idx = (idx + direction) % len(tab_ids)
            tabs.active = tab_ids[new_idx]

    def clear_all(self) -> None:
        self.query_one("#log-stdout", RichLog).clear()
        self.query_one("#log-stderr", RichLog).clear()
        self.query_one("#cpu-content", Static).update("")
        try:
            self.query_one("#gpu-content", Static).update("")
        except Exception:
            pass
        self.query_one("#stats-content", Static).update("")
