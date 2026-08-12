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


def array_task_count(job_id: str) -> int:
    """How many array tasks one squeue/sacct row stands for.

    A pending array arrives as a single row covering a range — ``123_[12-40]``
    is 29 tasks, ``123_[1,3,5]`` is 3, ``123_[1-4%2]`` is 4 (the ``%`` only
    throttles how many run at once). Running tasks arrive one row each, so
    anything without a range is 1.
    """
    _, _, suffix = job_id.partition("_")
    if not suffix.startswith("["):
        return 1
    spec = suffix[1:].split("]", 1)[0].split("%", 1)[0]
    total = 0
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            if lo.strip().isdigit() and hi.strip().isdigit():
                total += int(hi) - int(lo) + 1
                continue
        total += 1
    return total or 1


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
    source: str = "sstat"  # "sstat", "sacct", or "combined"
