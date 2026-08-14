"""Upper-right panel: job output/error logs, live CPU/GPU, and stats."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.css.query import NoMatches
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Static, TabbedContent, TabPane, RichLog

from lazyslurm.models import (
    Efficiency,
    GpuReading,
    JobStats,
    NodeSample,
    format_bytes,
    format_duration,
    parse_mem_bytes,
    sizing_hint,
)

# Efficiency colouring: green when the request was about right, red when the
# job used almost none of what it reserved.
_EFF_GOOD, _EFF_FAIR = 0.60, 0.25

__all__ = [
    "DetailView",
    "parse_mem_bytes",
    "sparkline",
    "efficiency_bar",
    "format_span",
    "meter_bar",
    "render_cpu_monitor",
    "render_gpu_monitor",
]


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


# ---------------------------------------------------------------------------
# htop/nvtop-style meters
# ---------------------------------------------------------------------------

# Load colouring: a core at 90% is the point, not a problem, so the top band is
# for saturation rather than danger — it is memory where red means trouble.
_LOAD_BUSY, _LOAD_HOT = 0.60, 0.85


def load_style(ratio: float) -> str:
    if ratio >= _LOAD_HOT:
        return "red"
    if ratio >= _LOAD_BUSY:
        return "yellow"
    return "green"


def meter_bar(ratio: float | None, width: int = 20, style: str | None = None) -> str:
    """`▏███████░░░░░` — one htop-style gauge, coloured by how full it is."""
    width = max(1, width)
    if ratio is None:
        return "[dim]▏" + "░" * width + "[/]"
    ratio = min(1.0, max(0.0, ratio))
    filled = min(width, int(round(ratio * width)))
    # A non-zero reading always shows at least one block: "1%" next to an empty
    # bar reads as a rendering bug rather than as a nearly idle core.
    if ratio > 0 and filled == 0:
        filled = 1
    colour = style or load_style(ratio)
    return f"[dim]▏[/][{colour}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/]"


def _pct(ratio: float | None) -> str:
    return "  ? " if ratio is None else f"{min(ratio, 1.0) * 100:3.0f}%"


def _band(values: list[float] | None, width: int) -> str:
    """A history band, right-aligned so the newest sample sits at the edge."""
    if not values:
        return "[dim]" + "·" * width + "[/]"
    recent = values[-width:]
    pad = "·" * (width - len(recent))
    return f"[dim]{pad}[/]{sparkline(recent, scale_max=1.0)}"


def _grid(cells: list[str], columns: int) -> list[str]:
    """Lay cells out in ``columns``, filling top-to-bottom as htop does.

    Column-major, so core ids read downwards: cores 0-3 on the left, 4-7 on the
    right, rather than every other core alternating sides.
    """
    rows = (len(cells) + columns - 1) // columns
    return ["  ".join(cells[c * rows + r]
                      for c in range(columns)
                      if c * rows + r < len(cells))
            for r in range(rows)]


def format_span(span: float | None) -> str:
    """How long the percentages average over, when that is worth saying.

    A sample that timed itself covers half a second and reads as "now", so it
    goes unmentioned. One differenced against the previous refresh covers the
    gap since -- which can be minutes, and must not be passed off as current.
    """
    if span is None or span <= 2:
        return ""
    if span < 90:
        return f" · last {span:.0f}s"
    minutes, seconds = divmod(int(span), 60)
    return f" · last {minutes}m {seconds:02d}s"


def render_cpu_monitor(
    sample: NodeSample,
    history: dict | None = None,
    width: int = 80,
    graph: bool = False,
) -> str:
    """The cpu tab in meter/graph mode: one bar per allocated core.

    ``history`` is the app's per-job ring buffer — ``{"cores": {cpu: [...]},
    "cpu": [...], "mem": [...]}`` — and is only read when ``graph`` is set.
    """
    if sample.error:
        return sample.error
    history = history or {}
    width = max(40, width)
    lines: list[str] = []

    scope = ("allocated to this job" if sample.scope == "job"
             else "on the node — srun --overlap unavailable, showing the whole machine")
    count = len(sample.cores)
    lines.append(f"[bold]Node: {sample.node}[/]  "
                 f"[dim]{count} core{'' if count == 1 else 's'}, {scope}"
                 f"{format_span(sample.span)}[/]")
    lines.append("")

    if not sample.cores:
        lines.append("[dim]No per-core data — /proc/stat was unreadable[/]")
        return "\n".join(lines)

    label = max(len(str(c.cpu)) for c in sample.cores)
    if graph:
        # One core per line, each with its own trailing history band: the whole
        # point of graph mode is seeing a core ramp, plateau or stall.
        core_hist = history.get("cores", {})
        bar_w = max(10, min(30, (width - label - 12) // 2))
        band_w = max(8, width - label - bar_w - 14)
        for core in sample.cores:
            lines.append(
                f"  {core.cpu:>{label}} {meter_bar(core.busy, bar_w)} {_pct(core.busy)}  "
                f"{_band(core_hist.get(core.cpu), band_w)}"
            )
    else:
        cell_w = label + 30
        columns = max(1, min(4, (width - 2) // (cell_w + 2)))
        bar_w = max(8, (width - 2 - 2 * (columns - 1)) // columns - label - 8)
        cells = [
            f"{core.cpu:>{label}} {meter_bar(core.busy, bar_w)} {_pct(core.busy)}"
            for core in sample.cores
        ]
        lines.extend("  " + row for row in _grid(cells, columns))

    lines.append("")
    mem_w = max(10, min(40, width - 40))
    mem_ratio = sample.mem_ratio
    mem_scope = "job" if sample.mem_scope == "job" else "node"
    if sample.mem_total:
        used = format_bytes(sample.mem_used or 0)
        total = format_bytes(sample.mem_total)
        style = "red" if mem_ratio and mem_ratio >= 0.9 else None
        lines.append(
            f"  {'Mem':>{label}} {meter_bar(mem_ratio, mem_w, style)} {_pct(mem_ratio)}"
            f"  {used}/{total} [dim]({mem_scope})[/]"
        )
    if sample.load:
        one, five, fifteen = sample.load
        lines.append(f"  {'Load':>{label}} [dim]{one:.2f}  {five:.2f}  {fifteen:.2f}"
                     f"  (1 / 5 / 15 min, whole node)[/]")

    if graph:
        band_w = max(20, width - label - 20)
        cpu_hist, mem_hist = history.get("cpu"), history.get("mem")
        if cpu_hist or mem_hist:
            lines.append("")
            lines.append("[bold underline]History[/]  [dim]newest on the right[/]")
            if cpu_hist:
                lines.append(f"  {'CPU':>{label}} {_band(cpu_hist, band_w)}  "
                             f"[dim]{len(cpu_hist)} samples[/]")
            if mem_hist:
                lines.append(f"  {'Mem':>{label}} {_band(mem_hist, band_w)}  "
                             f"[dim]{len(mem_hist)} samples[/]")
    return "\n".join(lines)


def render_gpu_monitor(
    reading: GpuReading,
    history: dict | None = None,
    width: int = 80,
    graph: bool = False,
) -> str:
    """The gpu tab in meter/graph mode: per-device utilisation and memory."""
    if reading.error:
        return reading.error
    history = history or {}
    width = max(40, width)
    lines: list[str] = []

    scope = ("allocated to this job" if reading.scope == "job"
             else "all node GPUs — srun --overlap unavailable")
    count = len(reading.gpus)
    lines.append(f"[bold]Node: {reading.node}[/]  "
                 f"[dim]{count} GPU{'' if count == 1 else 's'}, {scope}[/]")

    bar_w = max(12, min(34, width - 44))
    band_w = max(20, width - 24)
    util_hist, mem_hist = history.get("gpu_util", {}), history.get("gpu_mem", {})

    for gpu in reading.gpus:
        lines.append("")
        extras = []
        if gpu.temperature is not None:
            extras.append(f"{gpu.temperature:.0f}°C")
        if gpu.power is not None:
            extras.append(
                f"{gpu.power:.0f}W" + (f"/{gpu.power_limit:.0f}W" if gpu.power_limit else "")
            )
        suffix = f"  [dim]{'  '.join(extras)}[/]" if extras else ""
        lines.append(f"[bold]GPU {gpu.index}[/] [dim]{gpu.name}[/]{suffix}")
        lines.append(f"  {'Util':>4} {meter_bar(gpu.util, bar_w)} {_pct(gpu.util)}")
        mem_note = ""
        if gpu.mem_total:
            mem_note = f"  {format_bytes(gpu.mem_used or 0)}/{format_bytes(gpu.mem_total)}"
        style = "red" if (gpu.mem_ratio or 0) >= 0.95 else None
        lines.append(f"  {'Mem':>4} {meter_bar(gpu.mem_ratio, bar_w, style)} "
                     f"{_pct(gpu.mem_ratio)}{mem_note}")
        if graph:
            lines.append(f"  {'':>4} [dim]util[/] {_band(util_hist.get(gpu.index), band_w)}")
            lines.append(f"  {'':>4} [dim]mem [/] {_band(mem_hist.get(gpu.index), band_w)}")

    return "\n".join(lines)


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

    @property
    def monitor_width(self) -> int:
        """Columns the cpu/gpu meters may use.

        Falls back to 80 before the first layout pass, when every widget still
        reports a zero size — rendering into a width of 0 would clamp every bar
        to its minimum and look broken for one frame.
        """
        try:
            width = self.query_one("#cpu-content", Static).size.width
        except NoMatches:
            width = 0
        return (width or self.size.width or 82) - 2

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
