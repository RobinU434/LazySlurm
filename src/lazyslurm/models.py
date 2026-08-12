"""Data models for Slurm job information."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Config:
    """Runtime configuration from CLI arguments."""

    refresh: float = 5.0
    days: int = 7
    user: str = ""
    partition: str = ""
    no_gpu: bool = False
    no_live: bool = False
    remote: str = ""  # SSH target for remote mode, e.g. "user@login.hpc.edu"
    partition_order: list[str] | None = None  # e.g. ["gpu", "cpu", "fat"]
    partition_colors: dict[str, str] | None = None  # e.g. {"gpu": "green", "cpu": "cyan"}
    editor: str = "vim"  # text editor for viewing log files ("vim", "nano", etc.)
    pager: str = "less"  # pager for browsing whole logs ("less", "more", "bat", ...)
    max_name_width: int = 16  # max characters for job name column
    max_partition_width: int = 16  # max characters for partition column
    abbreviate_states: bool = False  # show abbreviated state names in terminated jobs
    collapse_arrays: bool = True  # fold a job array's tasks into one expandable row
    cache_max_age_days: int | None = 30  # prune cache entries older than this (None = never)
    script_cache_dir: str = ""  # where to archive sbatch scripts ("" = <config_dir>/scripts)


def _array_ranges(job_id: str) -> list[tuple[int, int, int]]:
    """The task-index ranges a job id covers, as ``[(lo, hi, step)]``.

    ``123_[12-40]`` -> ``[(12, 40, 1)]``, ``123_[1,3,5]`` -> three single
    ranges, ``123_4`` -> ``[(4, 4, 1)]``, a non-array id -> ``[]``.

    The spec is parsed explicitly rather than by scraping digits, because not
    every number in it is a task index:

    - ``%`` starts a concurrency throttle — ``[1-4%10]`` runs tasks 1-4, ten at
      a time — and it always terminates the spec, so everything after it goes.
    - ``:`` starts a stride — ``[0-9:2]`` is tasks 0, 2, 4, 6, 8 — so the step
      bounds the range but is not itself an index.
    """
    _, sep, suffix = job_id.partition("_")
    if not sep or not suffix:
        return []
    if not suffix.startswith("["):
        return [(int(suffix), int(suffix), 1)] if suffix.isdigit() else []

    spec = suffix[1:].split("]", 1)[0].split("%", 1)[0]
    ranges: list[tuple[int, int, int]] = []
    for part in spec.split(","):
        bounds, _, stride = part.strip().partition(":")
        step = int(stride) if stride.strip().isdigit() and int(stride) > 0 else 1
        lo, _, hi = bounds.strip().partition("-")
        lo, hi = lo.strip(), hi.strip()
        if lo.isdigit() and hi.isdigit():
            ranges.append((int(lo), int(hi), step))
        elif lo.isdigit() and not hi:
            ranges.append((int(lo), int(lo), 1))
    return ranges


def array_task_count(job_id: str) -> int:
    """How many array tasks one squeue/sacct row stands for.

    A pending array arrives as a single row covering a range — ``123_[12-40]``
    is 29 tasks. Running tasks arrive one row each, so anything that is not a
    range counts as 1.
    """
    ranges = _array_ranges(job_id)
    if not ranges:
        return 1
    return sum((hi - lo) // step + 1 for lo, hi, step in ranges) or 1


def array_index_span(job_ids) -> tuple[int, int] | None:
    """Lowest and highest task index across several array job ids."""
    ranges = [r for job_id in job_ids for r in _array_ranges(job_id)]
    if not ranges:
        return None
    return min(lo for lo, _, _ in ranges), max(hi for _, hi, _ in ranges)


@dataclass
class RunningJob:
    """A currently running or pending job from squeue."""

    job_id: str
    name: str
    elapsed: str
    partition: str
    state: str
    time_limit: str = ""
    nodes: str = ""
    cpus: str = ""
    memory: str = ""
    gres: str = ""
    work_dir: str = ""


@dataclass
class CompletedJob:
    """A completed/failed/cancelled job from sacct."""

    job_id: str
    name: str
    state: str
    exit_code: str = ""
    start: str = ""
    end: str = ""
    elapsed: str = ""
    partition: str = ""


@dataclass
class PartitionInfo:
    """Aggregated state of one partition from `sinfo --summarize`.

    Node and CPU counts follow Slurm's A/I/O/T convention: allocated, idle,
    other (down/drained/mixed-unavailable), total.
    """

    name: str
    avail: str = "up"
    nodes_alloc: int = 0
    nodes_idle: int = 0
    nodes_other: int = 0
    nodes_total: int = 0
    cpus_alloc: int = 0
    cpus_idle: int = 0
    cpus_other: int = 0
    cpus_total: int = 0
    time_limit: str = ""
    gres: str = ""
    running: int = 0  # job counts, filled in from squeue
    pending: int = 0

    @property
    def nodes_aiot(self) -> str:
        return f"{self.nodes_alloc}/{self.nodes_idle}/{self.nodes_other}/{self.nodes_total}"

    @property
    def cpus_aiot(self) -> str:
        return f"{self.cpus_alloc}/{self.cpus_idle}/{self.cpus_other}/{self.cpus_total}"

    @property
    def load(self) -> float:
        """Fraction of usable CPUs currently allocated (0.0-1.0).

        "Other" CPUs (down/drained nodes) are excluded from the denominator —
        a partition with half its nodes drained is fully loaded when the
        remaining half is busy, not 50% loaded.
        """
        usable = self.cpus_alloc + self.cpus_idle
        return self.cpus_alloc / usable if usable else 0.0


def gres_count(spec: str) -> int:
    """Total device count in a GRES string.

    ``gpu:a100:8(S:0-1)`` -> 8, ``gpu:a100:7(IDX:0-6)`` -> 7, ``gpu:2`` -> 2,
    and ``(null)`` / ``""`` -> 0. Comma-separated entries are summed.
    """
    spec = (spec or "").strip()
    if not spec or spec in ("(null)", "N/A", "None"):
        return 0
    total = 0
    for entry in spec.split(","):
        head = entry.split("(", 1)[0]          # drop "(S:0-1)" / "(IDX:0-7)"
        parts = [p for p in head.split(":") if p]
        if len(parts) < 2:
            continue
        try:
            total += int(parts[-1])
        except ValueError:
            continue
    return total


@dataclass
class NodeInfo:
    """One compute node of a partition, from `sinfo -N`."""

    name: str
    state: str = ""  # "idle", "mixed", "allocated", "drained*", ...
    cpus_alloc: int = 0
    cpus_idle: int = 0
    cpus_other: int = 0
    cpus_total: int = 0
    memory_mb: int = 0  # configured
    free_mem_mb: int = 0
    cpu_load: float = 0.0  # absolute load average, as Slurm reports it
    gres: str = ""  # configured
    gres_used: str = ""
    reason: str = ""  # why it is down / drained

    @property
    def base_state(self) -> str:
        """State without Slurm's trailing flags (`*` unresponsive, `$` reserved)."""
        return self.state.rstrip("*$~#!%@^-")

    @property
    def unresponsive(self) -> bool:
        return self.state.endswith("*")

    @property
    def cpus_aiot(self) -> str:
        return f"{self.cpus_alloc}/{self.cpus_idle}/{self.cpus_other}/{self.cpus_total}"

    @property
    def load(self) -> float:
        """Load average over total CPUs (0.0-1.0+, can exceed 1 when oversubscribed)."""
        return self.cpu_load / self.cpus_total if self.cpus_total else 0.0

    @property
    def mem_used_mb(self) -> int:
        return max(self.memory_mb - self.free_mem_mb, 0)

    @property
    def mem_used(self) -> float:
        return self.mem_used_mb / self.memory_mb if self.memory_mb else 0.0

    @property
    def gpus_total(self) -> int:
        return gres_count(self.gres)

    @property
    def gpus_used(self) -> int:
        return gres_count(self.gres_used)

    @property
    def gpus_free(self) -> int:
        return max(self.gpus_total - self.gpus_used, 0)


@dataclass
class PartitionJob:
    """A job on a partition, from any user (squeue without -u)."""

    job_id: str
    user: str
    name: str
    state: str
    elapsed: str = ""
    time_limit: str = ""
    nodes: str = ""
    cpus: str = ""
    gres: str = ""
    nodelist: str = ""  # node list, or the pending reason in parentheses


@dataclass
class PriorityInfo:
    """One pending job's priority, broken into the factors sprio reports.

    `rank`/`queued` place the job among the other pending jobs of its
    partition: rank 1 is next in line. Both are 0 when the queue could not be
    read (sprio needs accounting enabled).
    """

    job_id: str
    total: int = 0
    age: int = 0
    fairshare: int = 0
    job_size: int = 0
    partition: int = 0
    qos: int = 0
    rank: int = 0
    queued: int = 0

    @property
    def factors(self) -> list[tuple[str, int]]:
        """The non-zero contributions, largest first."""
        named = [
            ("Age", self.age),
            ("Fair-share", self.fairshare),
            ("Job size", self.job_size),
            ("Partition", self.partition),
            ("QOS", self.qos),
        ]
        return sorted([f for f in named if f[1]], key=lambda f: -f[1])

    @property
    def ahead(self) -> int:
        """How many pending jobs of the partition outrank this one."""
        return max(self.rank - 1, 0)


@dataclass
class JobDetail:
    """Detailed job info parsed from scontrol show job or sacct."""

    job_id: str
    raw: dict[str, str] = field(default_factory=dict)
    stdout_path: str | None = None
    stderr_path: str | None = None
    work_dir: str = ""
    source: str = "scontrol"  # "scontrol" or "sacct"

    @property
    def submit_line(self) -> str:
        # Prefer SubmitLine (full sbatch command line) over Command (just script path)
        return self.raw.get("SubmitLine") or self.raw.get("Command") or "N/A"

    @property
    def partition(self) -> str:
        return self.raw.get("Partition", "N/A")

    @property
    def node_list(self) -> str:
        return self.raw.get("NodeList", self.raw.get("Nodelist", "N/A"))

    @property
    def num_cpus(self) -> str:
        return self.raw.get("NumCPUs", self.raw.get("NCPUS", "N/A"))

    @property
    def num_nodes(self) -> str:
        return self.raw.get("NumNodes", self.raw.get("NNodes", "N/A"))

    @property
    def memory(self) -> str:
        return self.raw.get("MinMemoryNode", self.raw.get("ReqMem", "N/A"))

    @property
    def time_limit(self) -> str:
        return self.raw.get("TimeLimit", self.raw.get("Timelimit", "N/A"))

    @property
    def run_time(self) -> str:
        return self.raw.get("RunTime", self.raw.get("Elapsed", "N/A"))

    @property
    def submit_time(self) -> str:
        return self.raw.get("SubmitTime", self.raw.get("Submit", "N/A"))

    @property
    def start_time(self) -> str:
        return self.raw.get("StartTime", self.raw.get("Start", "N/A"))

    @property
    def end_time(self) -> str:
        return self.raw.get("EndTime", self.raw.get("End", "N/A"))

    @property
    def state(self) -> str:
        return self.raw.get("JobState", self.raw.get("State", "N/A"))

    @property
    def tres(self) -> str:
        return self.raw.get("TRES", self.raw.get("ReqTRES", self.raw.get("AllocTRES", "N/A")))

    @property
    def gres(self) -> str:
        tres = self.tres
        if tres and "gres/gpu" in tres.lower():
            for part in tres.split(","):
                if "gres/gpu" in part.lower():
                    return part.strip()
        return self.raw.get("Gres", "None")

    @property
    def account(self) -> str:
        return self.raw.get("Account", "N/A")

    @property
    def qos(self) -> str:
        return self.raw.get("QOS", self.raw.get("QoS", "N/A"))


def parse_mem_bytes(text: str) -> float | None:
    """Parse a Slurm memory string — ``1234K``, ``512M``, ``2.5G`` — to bytes.

    Slurm's per-node/per-cpu markers (``64Gn``, ``4Gc``) are handled by
    parse_req_mem(); this only takes the plain size.
    """
    if not text or text in ("N/A", "Unknown", ""):
        return None
    text = text.strip()
    multipliers = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}
    if text and text[-1].upper() in multipliers:
        try:
            return float(text[:-1]) * multipliers[text[-1].upper()]
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_duration(text: str) -> float | None:
    """Seconds from any duration shape sacct emits.

    ``1-04:09:36`` (days), ``06:31:12``, ``00:43.900`` (MM:SS with millis),
    ``43.9`` — plus ``UNLIMITED``/``Partition_Limit``, which are not durations
    and come back as None.
    """
    value = (text or "").strip()
    if not value or value in ("N/A", "Unknown", "UNLIMITED", "Partition_Limit", "INVALID"):
        return None

    days = 0.0
    if "-" in value:
        head, _, value = value.partition("-")
        try:
            days = float(head)
        except ValueError:
            return None

    parts = value.split(":")
    try:
        numbers = [float(p) for p in parts]
    except ValueError:
        return None
    if len(numbers) > 3:
        return None
    while len(numbers) < 3:
        numbers.insert(0, 0.0)   # MM:SS -> 0:MM:SS, SS -> 0:0:SS
    hours, minutes, seconds = numbers
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_req_mem(raw: str, alloc_cpus: int = 0, nnodes: int = 0) -> float | None:
    """Total bytes a job asked for, whatever units sacct used.

    Slurm before 21.08 qualifies the number: ``64Gn`` is per node, ``4Gc`` is
    per CPU. Newer versions report the job total with no marker at all, so an
    unmarked value is taken as-is rather than multiplied.
    """
    value = (raw or "").strip()
    if not value or value in ("N/A", "0", "Unknown"):
        return None

    per = ""
    if value[-1] in ("n", "c"):
        per, value = value[-1], value[:-1]

    size = parse_mem_bytes(value)
    if size is None:
        return None
    if per == "n":
        return size * max(nnodes, 1)
    if per == "c":
        return size * max(alloc_cpus, 1)
    return size


@dataclass
class Efficiency:
    """How much of what a job asked for it actually used.

    Every ratio is a fraction of 1.0, or None when Slurm did not record the
    numbers needed for it (a job too old for sacct, a step that never ran, a
    partition with no default time limit).
    """

    cpu: float | None = None
    memory: float | None = None
    walltime: float | None = None

    cpu_used: float = 0.0        # core-equivalents actually busy
    cpu_alloc: int = 0
    mem_used: float = 0.0        # bytes, peak of any one task
    mem_request: float = 0.0     # bytes, per node
    elapsed: float = 0.0         # seconds
    time_limit: float = 0.0      # seconds
    gpus: int = 0
    nnodes: int = 1

    @property
    def has_any(self) -> bool:
        return any(v is not None for v in (self.cpu, self.memory, self.walltime))

    @property
    def oom_risk(self) -> bool:
        """Peak memory at or above the request — the next run may be killed."""
        return self.memory is not None and self.memory >= 1.0


def compute_efficiency(stats) -> Efficiency:
    """Derive CPU / memory / walltime efficiency from a JobStats.

    CPU is ``TotalCPU / (cores x elapsed)`` — the same definition seff uses.
    Memory compares the peak RSS of one task against the request *per node*, so
    a multi-node job is not credited with memory it never touched on one node.
    """
    eff = Efficiency()
    eff.cpu_alloc = stats.alloc_cpus
    eff.nnodes = max(stats.nnodes, 1)
    eff.gpus = stats.gpu_count

    elapsed = parse_duration(stats.elapsed)
    total_cpu = parse_duration(stats.total_cpu)
    limit = parse_duration(stats.time_limit)
    used_mem = parse_mem_bytes(stats.max_rss)
    request = parse_req_mem(stats.req_mem, stats.alloc_cpus, stats.nnodes)

    if elapsed:
        eff.elapsed = elapsed
        if total_cpu is not None and stats.alloc_cpus > 0:
            eff.cpu_used = total_cpu / elapsed
            eff.cpu = total_cpu / (elapsed * stats.alloc_cpus)
        if limit:
            eff.time_limit = limit
            eff.walltime = elapsed / limit

    if used_mem is not None and request:
        per_node = request / eff.nnodes
        eff.mem_used = used_mem
        eff.mem_request = per_node
        if per_node > 0:
            eff.memory = used_mem / per_node

    return eff


def format_bytes(value: float) -> str:
    """Bytes as the shortest sensible Slurm-style size: 2.6G, 512M, 177M."""
    for unit, size in (("T", 1024 ** 4), ("G", 1024 ** 3), ("M", 1024 ** 2), ("K", 1024)):
        if value >= size:
            scaled = value / size
            return f"{scaled:.1f}{unit}" if scaled < 10 else f"{scaled:.0f}{unit}"
    return f"{value:.0f}B"


def format_duration(seconds: float) -> str:
    """Seconds as ``6:37:27`` / ``17:02`` / ``43s``."""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    if minutes:
        return f"{minutes}:{secs:02d}"
    return f"{secs}s"


def sizing_hint(eff: Efficiency) -> str:
    """What to ask for next time, when the job was clearly over-provisioned.

    Suggests roughly a third more than the job actually used, so a rerun has
    headroom. Empty when the numbers are missing or the request was reasonable.
    """
    suggestions = []
    if eff.memory is not None and eff.memory < 0.5 and eff.mem_used > 0:
        target = eff.mem_used * 1.3
        gib = target / 1024 ** 3
        if gib >= 1:
            suggestions.append(f"--mem={max(1, int(gib + 0.999))}G")
        else:
            suggestions.append(f"--mem={max(256, int(target / 1024 ** 2 + 255) // 256 * 256)}M")
    if eff.cpu is not None and eff.cpu < 0.5 and eff.cpu_alloc > 1:
        cores = max(1, int(eff.cpu_used * 1.3 + 0.999))
        if cores < eff.cpu_alloc:
            suggestions.append(f"--cpus-per-task={cores}")
    if eff.walltime is not None and eff.walltime < 0.5 and eff.time_limit > 0:
        target = eff.elapsed * 1.5
        hours, rest = divmod(int(target), 3600)
        suggestions.append(f"--time={hours:02d}:{rest // 60:02d}:00")
    if not suggestions:
        return ""
    return "next time try " + " ".join(suggestions)


@dataclass
class JobStats:
    """Resource usage stats from sstat and sacct."""

    job_id: str
    # CPU
    ave_cpu: str = "N/A"
    ave_cpu_freq: str = "N/A"
    # Memory
    ave_rss: str = "N/A"
    max_rss: str = "N/A"
    ave_vm_size: str = "N/A"
    max_vm_size: str = "N/A"
    req_mem: str = "N/A"
    # Disk I/O
    ave_disk_read: str = "N/A"
    ave_disk_write: str = "N/A"
    max_disk_read: str = "N/A"
    max_disk_write: str = "N/A"
    # GPU (from sacct TRES)
    gpu_alloc: str = "N/A"
    gpu_tres: str = "N/A"
    # From sacct
    total_cpu: str = "N/A"
    elapsed: str = "N/A"
    max_rss_node: str = "N/A"
    max_rss_task: str = "N/A"
    # Denominators for the efficiency report
    alloc_cpus: int = 0
    nnodes: int = 0
    ntasks: int = 0
    time_limit: str = "N/A"
    source: str = "sstat"  # "sstat", "sacct", or "combined"

    @property
    def gpu_count(self) -> int:
        """GPUs allocated, from the TRES string sacct reports."""
        for spec in (self.gpu_alloc, self.gpu_tres):
            for part in (spec or "").split(","):
                if "gres/gpu=" in part.lower():
                    try:
                        return int(part.split("=")[-1])
                    except ValueError:
                        continue
        return 0

    @property
    def efficiency(self) -> "Efficiency":
        return compute_efficiency(self)
