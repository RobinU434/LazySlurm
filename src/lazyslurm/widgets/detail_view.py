"""Upper-right panel: job output/error logs, live CPU/GPU, and stats."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.css.query import NoMatches
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static, TabbedContent, TabPane, RichLog

from lazyslurm.models import (
    Efficiency,
    JobStats,
    format_bytes,
    format_duration,
    parse_mem_bytes,
    sizing_hint,
)

# Efficiency colouring: green when the request was about right, red when the
# job used almost none of what it reserved.
_EFF_GOOD, _EFF_FAIR = 0.60, 0.25

__all__ = ["DetailView", "parse_mem_bytes", "sparkline", "efficiency_bar"]


def efficiency_style(ratio: float | None) -> str:
    if ratio is None:
        return "dim"
    if ratio >= 1.0:
        return "red bold"      # used everything it asked for — may be capped
    if ratio >= _EFF_GOOD:
        return "green"
    if ratio >= _EFF_FAIR:
        return "yellow"
    return "red"


def efficiency_bar(ratio: float | None, width: int = 8) -> str:
    """`▆▆▆▁▁▁▁▁` — a compact eighth-block gauge of one ratio."""
    if ratio is None:
        return " " * width
    filled = min(width, max(0, round(min(ratio, 1.0) * width)))
    return "▆" * filled + "▁" * (width - filled)

_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], scale_max: float | None = None) -> str:
    """Render a sparkline from a list of numeric values.

    ``scale_max`` fixes the top of the scale — pass 1.0 for a series that is
    already a fraction, so a job using half its cores does not look identical
    to one using all of them.
    """
    if not values:
        return ""
    mx = scale_max if scale_max else max(values)
    if mx <= 0:
        return "▁" * len(values)
    return "".join(_SPARK_CHARS[min(int(v / mx * 7), 7)] for v in values)


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
        except NoMatches:
            pass  # GPU tab not present (--no-gpu / --no-live)

    @staticmethod
    def _efficiency_section(stats: JobStats) -> str:
        """The Efficiency block: used / requested, a gauge, and a sizing hint."""
        eff = stats.efficiency
        lines = ["[bold underline]Efficiency[/]"]

        if not eff.has_any:
            lines.append("  [dim]unavailable — Slurm no longer has accounting "
                         "data for this job[/]")
            return "\n".join(lines)

        def row(label: str, ratio: float | None, used: str, asked: str, note: str = "") -> str:
            style = efficiency_style(ratio)
            gauge = efficiency_bar(ratio)
            if ratio is None:
                return f"  {label:<9} [dim]{used:>9} / {asked:<9}      —[/]"
            # Never round a fraction of a percent up to "1%": a job that used
            # half a percent of its request should read as having used none.
            pct = "<1%" if 0 < ratio < 0.01 else f"{ratio * 100:.0f}%"
            return (
                f"  {label:<9} {used:>9} / {asked:<9} "
                f"[{style}]{pct:>4}[/] [{style}]{gauge}[/]{note}"
            )

        if eff.cpu is not None:
            lines.append(row(
                "CPU", eff.cpu,
                "<0.1" if 0 < eff.cpu_used < 0.1 else f"{eff.cpu_used:.1f}",
                f"{eff.cpu_alloc} cores",
                "  [dim]← over-requested[/]" if eff.cpu < _EFF_FAIR else "",
            ))
        if eff.memory is not None:
            note = ""
            if eff.oom_risk:
                note = "  [red]← at the limit, risks OOM[/]"
            elif eff.memory < _EFF_FAIR:
                note = "  [dim]← over-requested[/]"
            per_node = "/node" if eff.nnodes > 1 else ""
            lines.append(row(
                "Memory", eff.memory,
                format_bytes(eff.mem_used), format_bytes(eff.mem_request) + per_node,
                note,
            ))
        if eff.gpus:
            lines.append(f"  {'GPU':<9} {eff.gpus:>9} / {eff.gpus} allocated"
                         f"      [dim]— utilisation is not recorded by Slurm[/]")
        if eff.walltime is not None:
            lines.append(row(
                "Walltime", eff.walltime,
                format_duration(eff.elapsed), format_duration(eff.time_limit),
            ))

        hint = sizing_hint(eff)
        if hint:
            lines.append(f"  [dim]{hint}[/]")
        return "\n".join(lines)

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

        # Efficiency — what the job used against what it reserved. First,
        # because it is the only part that answers "was this sized right?".
        efficiency = self._efficiency_section(stats)
        if efficiency:
            sections.append(efficiency)

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
                # Fraction of allocated cores busy since the previous sample —
                # plotted against a fixed 0-100% scale, not its own maximum.
                cpu = history["cpu"]
                hist_lines.append(
                    f"  CPU:    {sparkline(cpu, scale_max=1.0)}"
                    f"  ({len(cpu)} samples, now {cpu[-1] * 100:.0f}% of alloc)"
                )
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
        except NoMatches:
            pass  # GPU tab not present (--no-gpu / --no-live)
        self.query_one("#stats-content", Static).update("")
