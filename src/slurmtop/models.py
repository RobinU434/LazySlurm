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
    max_name_width: int = 16  # max characters for job name column
    max_partition_width: int = 16  # max characters for partition column
    abbreviate_states: bool = False  # show abbreviated state names in terminated jobs
    cache_max_age_days: int | None = 30  # prune cache entries older than this (None = never)
    script_cache_dir: str = ""  # where to archive sbatch scripts ("" = <config_dir>/scripts)


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
