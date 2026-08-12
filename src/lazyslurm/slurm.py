"""Async wrappers around Slurm CLI commands."""

from __future__ import annotations

import asyncio
import os
import shlex
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from lazyslurm.models import CompletedJob, Config, JobDetail, JobStats, RunningJob

USER = os.environ.get("USER", os.environ.get("LOGNAME", ""))

# Module-level config, set once from app.py via set_config().
_config: Config = Config()

# Multiplex SSH connections: the first command opens a master connection and
# subsequent commands reuse it (ControlPersist keeps it warm), turning many
# TCP+auth handshakes per poll into one. Biggest latency win for --remote mode.
_SSH_CONTROL_DIR = os.path.join(os.path.expanduser("~"), ".ssh", "cm-lazyslurm")
try:
    os.makedirs(_SSH_CONTROL_DIR, mode=0o700, exist_ok=True)
except OSError:
    _SSH_CONTROL_DIR = ""

_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=3",
    "-o", "BatchMode=yes",
]
if _SSH_CONTROL_DIR:
    _SSH_OPTS += [
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=60s",
        "-o", f"ControlPath={os.path.join(_SSH_CONTROL_DIR, '%C')}",
    ]
_SSH_TIMEOUT = 8  # seconds


def set_config(config: Config) -> None:
    """Set the module-level config (called once at app startup)."""
    global _config
    _config = config


# ---------------------------------------------------------------------------
# Transport layer
# ---------------------------------------------------------------------------


async def _run_cmd(*args: str) -> tuple[str, str, int]:
    """Run a command locally or via SSH if remote mode is active."""
    if _config.remote:
        # Tunnel through SSH: join args into a single shell command
        remote_cmd = " ".join(shlex.quote(a) for a in args)
        return await _run_ssh(_config.remote, remote_cmd)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return (
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
        proc.returncode or 0,
    )


async def _run_ssh(host: str, remote_cmd: str) -> tuple[str, str, int]:
    """Run a command on a remote host via SSH."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", *_SSH_OPTS, host, remote_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_SSH_TIMEOUT,
        )
        return (
            stdout.decode(errors="replace"),
            stderr.decode(errors="replace"),
            proc.returncode or 0,
        )
    except asyncio.TimeoutError:
        return "", "SSH timeout", 1
    except OSError as e:
        return "", str(e), 1


async def _ssh_cmd(node: str, remote_cmd: str) -> tuple[str, int]:
    """Run a command on a compute node.

    In local mode: SSH directly to the node.
    In remote mode: SSH via ProxyJump through the login node.
    """
    try:
        cmd = ["ssh", *_SSH_OPTS]
        if _config.remote:
            cmd.extend(["-J", _config.remote])
        cmd.extend([node, remote_cmd])
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=_SSH_TIMEOUT,
        )
        return stdout.decode(errors="replace"), proc.returncode or 0
    except (asyncio.TimeoutError, OSError):
        return "", 1


# ---------------------------------------------------------------------------
# squeue – running / pending jobs
# ---------------------------------------------------------------------------

_SQUEUE_FORMAT = "%i|%j|%M|%P|%T|%l|%D|%C|%m|%b|%Z"


async def get_running_jobs(config: Config | None = None) -> list[RunningJob]:
    """Fetch current jobs for the user via squeue, sorted by job ID descending."""
    cfg = config or _config
    user = cfg.user or USER
    cmd: list[str] = [
        "squeue", "-u", user,
        f"--format={_SQUEUE_FORMAT}",
        "--noheader",
        "--sort=-i",
    ]
    if cfg.partition:
        cmd.extend(["-p", cfg.partition])

    stdout, _, rc = await _run_cmd(*cmd)
    if rc != 0 or not stdout.strip():
        return []

    jobs: list[RunningJob] = []
    for line in stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 11:
            continue
        jobs.append(RunningJob(
            job_id=parts[0].strip(),
            name=parts[1].strip(),
            elapsed=parts[2].strip(),
            partition=parts[3].strip(),
            state=parts[4].strip(),
            time_limit=parts[5].strip(),
            nodes=parts[6].strip(),
            cpus=parts[7].strip(),
            memory=parts[8].strip(),
            gres=parts[9].strip() or "None",
            work_dir=parts[10].strip(),
        ))
    jobs.sort(key=lambda j: int(j.job_id) if j.job_id.isnumeric() else 0, reverse=True)
    return jobs


# ---------------------------------------------------------------------------
# sacct – completed / past jobs
# ---------------------------------------------------------------------------

_SACCT_FORMAT = "JobID,JobName,State,ExitCode,Start,End,Elapsed,Partition"


async def get_completed_jobs(config: Config | None = None) -> list[CompletedJob]:
    """Fetch past jobs via sacct, sorted by job ID descending (latest first)."""
    cfg = config or _config
    days = cfg.days
    user = cfg.user or USER
    start_time = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00")
    cmd: list[str] = [
        "sacct",
        "-u", user,
        f"--format={_SACCT_FORMAT}",
        f"--starttime={start_time}",
        "--noheader",
        "--parsable2",
    ]

    stdout, _, rc = await _run_cmd(*cmd)
    if rc != 0 or not stdout.strip():
        return []

    jobs: list[CompletedJob] = []
    for line in stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 8:
            continue
        job_id = parts[0].strip()
        if "." in job_id:
            continue
        state = parts[2].strip()
        if state in ("RUNNING", "PENDING", "REQUEUED"):
            continue
        if cfg.partition and parts[7].strip() != cfg.partition:
            continue
        jobs.append(CompletedJob(
            job_id=job_id,
            name=parts[1].strip(),
            state=state,
            exit_code=parts[3].strip(),
            start=parts[4].strip(),
            end=parts[5].strip(),
            elapsed=parts[6].strip(),
            partition=parts[7].strip(),
        ))
    jobs.sort(key=lambda j: int(j.job_id) if j.job_id.isnumeric() else 0, reverse=True)
    return jobs


# ---------------------------------------------------------------------------
# scontrol – job detail
# ---------------------------------------------------------------------------


def _parse_scontrol(output: str) -> dict[str, str]:
    """Parse scontrol show job output into a key-value dict.

    Most fields are whitespace-separated ``key=value`` tokens, but a few
    (notably ``SubmitLine``) hold a value that itself contains spaces and runs
    to the end of its line. Those are captured whole so the full sbatch command
    survives — otherwise ``SubmitLine=sbatch --array=1-4 job.sh`` would be
    truncated to just ``sbatch`` and break resubmission.
    """
    result: dict[str, str] = {}
    for line in output.splitlines():
        marker = "SubmitLine="
        idx = line.find(marker)
        if idx != -1:
            result["SubmitLine"] = line[idx + len(marker):].strip()
            line = line[:idx]  # parse any tokens preceding it normally
        for token in line.split():
            if "=" in token:
                key, _, value = token.partition("=")
                result[key] = value
    return result


async def get_job_detail(job_id: str) -> JobDetail | None:
    """Get detailed info for a job. Tries scontrol first, falls back to sacct."""
    from lazyslurm import config as persistent_config

    stdout, _, rc = await _run_cmd("scontrol", "show", "job", job_id)
    if rc == 0 and stdout.strip() and "Invalid job id" not in stdout:
        raw = _parse_scontrol(stdout)
        stdout_path = raw.get("StdOut")
        stderr_path = raw.get("StdErr")
        work_dir = raw.get("WorkDir", "")
        # Cache all paths so they survive after the job leaves scontrol.
        # Command and SubmitLine are stored separately — see cache_job_paths.
        persistent_config.cache_job_paths(
            job_id,
            stdout_path,
            stderr_path,
            raw.get("Command"),
            work_dir,
            submit_line=raw.get("SubmitLine"),
        )
        # scontrol answered, so the job is still in slurmctld and its batch
        # script is still retrievable — archive it now, because after MinJobAge
        # it is gone for good. Cheap after the first time (one stat), and a
        # failure here must never stop detail loading.
        try:
            await archive_batch_script(job_id)
        except Exception:
            pass
        return JobDetail(
            job_id=job_id,
            raw=raw,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            work_dir=work_dir,
            source="scontrol",
        )
    return await _get_job_detail_sacct(job_id)


async def _get_job_detail_sacct(job_id: str) -> JobDetail | None:
    """Get job detail from sacct as fallback."""
    fmt = (
        "JobID,JobName,State,ExitCode,Partition,NodeList,NCPUS,NNodes,"
        "ReqMem,Timelimit,Elapsed,Submit,Start,End,WorkDir,Account,QOS,"
        "ReqTRES,AllocTRES,SubmitLine"
    )
    stdout, _, rc = await _run_cmd(
        "sacct",
        "-j", job_id,
        f"--format={fmt}",
        "--noheader",
        "--parsable2",
    )
    if rc != 0 or not stdout.strip():
        return None

    for line in stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 20:
            continue
        jid = parts[0].strip()
        if "." in jid:
            continue

        raw = {
            "JobID": parts[0].strip(),
            "JobName": parts[1].strip(),
            "State": parts[2].strip(),
            "ExitCode": parts[3].strip(),
            "Partition": parts[4].strip(),
            "Nodelist": parts[5].strip(),
            "NCPUS": parts[6].strip(),
            "NNodes": parts[7].strip(),
            "ReqMem": parts[8].strip(),
            "Timelimit": parts[9].strip(),
            "Elapsed": parts[10].strip(),
            "Submit": parts[11].strip(),
            "Start": parts[12].strip(),
            "End": parts[13].strip(),
            "WorkDir": parts[14].strip(),
            "Account": parts[15].strip(),
            "QoS": parts[16].strip(),
            "ReqTRES": parts[17].strip(),
            "AllocTRES": parts[18].strip(),
            "SubmitLine": parts[19].strip(),
        }
        work_dir = raw["WorkDir"]
        job_name = raw["JobName"]

        # Check the persistent cache (paths saved while job was running)
        from lazyslurm import config as persistent_config
        cached_out, cached_err = persistent_config.get_cached_log_paths(job_id)
        if cached_out or cached_err:
            stdout_path = cached_out
            stderr_path = cached_err
        else:
            # Fall back to guessing from filename patterns
            stdout_path = await _guess_log_path(work_dir, job_id, "out", job_name)
            stderr_path = await _guess_log_path(work_dir, job_id, "err", job_name)

        # Restore cached command/workdir into raw dict if sacct didn't provide them
        cached_cmd, cached_wd = persistent_config.get_cached_command(job_id)
        if cached_cmd and not raw.get("Command") and not raw.get("SubmitLine"):
            raw["Command"] = cached_cmd
        if cached_wd and not work_dir:
            work_dir = cached_wd

        # Many clusters merge stdout and stderr into one .out file.
        # If no separate .err file found, fall back to the stdout path.
        if not stderr_path and stdout_path:
            stderr_path = stdout_path
        return JobDetail(
            job_id=job_id,
            raw=raw,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            work_dir=work_dir,
            source="sacct",
        )
    return None


async def _guess_log_path(
    work_dir: str, job_id: str, suffix: str, job_name: str = "",
) -> str | None:
    """Try common Slurm log file naming patterns (local or remote).

    Checks multiple patterns used by different cluster configurations.
    """
    if not work_dir:
        return None

    ext_out = "out" if suffix == "out" else "err"
    candidates = [
        # Default Slurm pattern
        os.path.join(work_dir, f"slurm-{job_id}.{ext_out}"),
        # Common: just .out / .err
        os.path.join(work_dir, f"slurm-{job_id}.{suffix}"),
        # Some clusters use job name in the filename
    ]
    if job_name:
        candidates.extend([
            os.path.join(work_dir, f"{job_name}-{job_id}.{ext_out}"),
            os.path.join(work_dir, f"{job_name}_{job_id}.{ext_out}"),
            os.path.join(work_dir, f"{job_name}.{ext_out}"),
            # sbatch --output/--error with %j pattern
            os.path.join(work_dir, f"{job_name}-%j.{ext_out}".replace("%j", job_id)),
        ])
    # Also check logs/ subdirectory
    candidates.extend([
        os.path.join(work_dir, "logs", f"slurm-{job_id}.{ext_out}"),
        os.path.join(work_dir, "log", f"slurm-{job_id}.{ext_out}"),
    ])

    for path in candidates:
        if await _file_exists(path):
            return path
    return None


async def _file_exists(path: str) -> bool:
    """Check if a file exists (locally or on the remote host)."""
    if _config.remote:
        _, _, rc = await _run_ssh(_config.remote, f"test -f {shlex.quote(path)}")
        return rc == 0
    return await asyncio.to_thread(os.path.isfile, path)


# ---------------------------------------------------------------------------
# sstat – resource usage for running jobs
# ---------------------------------------------------------------------------

_SSTAT_FORMAT = (
    "AveCPU,AveCPUFreq,AveRSS,MaxRSS,AveVMSize,MaxVMSize,"
    "AveDiskRead,AveDiskWrite,MaxDiskRead,MaxDiskWrite,"
    "MaxRSSNode,MaxRSSTask"
)

_SACCT_STATS_FORMAT = (
    "TotalCPU,Elapsed,ReqMem,AllocTRES,ReqTRES"
)


async def get_job_stats(job_id: str) -> JobStats | None:
    """Get resource usage stats combining sstat (running) and sacct data."""
    sstat_result, sacct_result = await asyncio.gather(
        _get_sstat(job_id),
        _get_sacct_stats(job_id),
    )

    if sstat_result is None and sacct_result is None:
        return None

    stats = sstat_result or JobStats(job_id=job_id)

    if sacct_result:
        stats.total_cpu = sacct_result.get("TotalCPU", "N/A")
        stats.elapsed = sacct_result.get("Elapsed", "N/A")
        stats.req_mem = sacct_result.get("ReqMem", "N/A")
        for tres_key in ("AllocTRES", "ReqTRES"):
            tres = sacct_result.get(tres_key, "")
            if "gres/gpu" in tres.lower():
                for part in tres.split(","):
                    if "gres/gpu" in part.lower():
                        stats.gpu_alloc = part.strip()
                        break
                break
        stats.gpu_tres = sacct_result.get("AllocTRES", sacct_result.get("ReqTRES", "N/A"))
        if stats.source == "sstat":
            stats.source = "combined"
        else:
            stats.source = "sacct"

    return stats


async def _get_sstat(job_id: str) -> JobStats | None:
    """Get live resource usage for a running job via sstat."""
    stdout, _, rc = await _run_cmd(
        "sstat",
        "-j", f"{job_id}.batch",
        f"--format={_SSTAT_FORMAT}",
        "--noheader",
        "--parsable2",
    )
    if rc != 0 or not stdout.strip():
        return None

    line = stdout.strip().splitlines()[0]
    parts = line.split("|")
    if len(parts) < 12:
        return None
    return JobStats(
        job_id=job_id,
        ave_cpu=parts[0].strip() or "N/A",
        ave_cpu_freq=parts[1].strip() or "N/A",
        ave_rss=parts[2].strip() or "N/A",
        max_rss=parts[3].strip() or "N/A",
        ave_vm_size=parts[4].strip() or "N/A",
        max_vm_size=parts[5].strip() or "N/A",
        ave_disk_read=parts[6].strip() or "N/A",
        ave_disk_write=parts[7].strip() or "N/A",
        max_disk_read=parts[8].strip() or "N/A",
        max_disk_write=parts[9].strip() or "N/A",
        max_rss_node=parts[10].strip() or "N/A",
        max_rss_task=parts[11].strip() or "N/A",
        source="sstat",
    )


async def _get_sacct_stats(job_id: str) -> dict[str, str] | None:
    """Get accounting stats from sacct."""
    stdout, _, rc = await _run_cmd(
        "sacct",
        "-j", job_id,
        f"--format={_SACCT_STATS_FORMAT}",
        "--noheader",
        "--parsable2",
    )
    if rc != 0 or not stdout.strip():
        return None

    for line in stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue
        return {
            "TotalCPU": parts[0].strip(),
            "Elapsed": parts[1].strip(),
            "ReqMem": parts[2].strip(),
            "AllocTRES": parts[3].strip(),
            "ReqTRES": parts[4].strip(),
        }
    return None


# ---------------------------------------------------------------------------
# Log file reading
# ---------------------------------------------------------------------------

TAIL_LINES = 500


async def read_log_file(path: str | None, tail_lines: int = TAIL_LINES) -> str:
    """Read the tail of a log file (locally or via SSH in remote mode)."""
    if not path:
        return "(no log file path available)"

    if _config.remote:
        # Read file via SSH
        cmd = f"tail -n {tail_lines} {shlex.quote(path)} 2>/dev/null || echo '(file not found: {path})'"
        stdout, _, rc = await _run_ssh(_config.remote, cmd)
        return stdout if stdout.strip() else f"(file not found: {path})"

    if not os.path.isfile(path):
        return f"(file not found: {path})"

    def _read() -> str:
        with open(path, errors="replace") as f:
            lines = deque(f, maxlen=tail_lines)
        return "".join(lines)

    return await asyncio.to_thread(_read)


# ---------------------------------------------------------------------------
# scancel – cancel a job
# ---------------------------------------------------------------------------


async def cancel_job(job_id: str, force: bool = False) -> tuple[bool, str]:
    """Cancel a job. If force=True, sends SIGKILL immediately. Returns (success, msg)."""
    if force:
        _, stderr, rc = await _run_cmd("scancel", "--signal=KILL", job_id)
    else:
        _, stderr, rc = await _run_cmd("scancel", job_id)
    if rc == 0:
        kind = "force-cancelled" if force else "cancelled"
        return True, f"Job {job_id} {kind}."
    return False, f"Failed to cancel job {job_id}: {stderr.strip()}"


def _script_token_index(tokens: list[str]) -> int | None:
    """Index of the script path in an sbatch argument list, or None.

    The script is the last bare (non-flag) token, skipping any token that is the
    value of a preceding separate-form option such as "--array 1-4" or "-J name".
    """
    skip_next = False
    found = None
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            # A separate-form option ("--array 1-4") consumes the next token;
            # the "=" form ("--array=1-4") carries its own value.
            if "=" not in tok:
                skip_next = True
            continue
        found = i
    return found


async def resubmit_job(
    command: str, work_dir: str, job_id: str | None = None,
) -> tuple[bool, str]:
    """Resubmit a job using its original sbatch command. Returns (success, msg).

    `command` is either a script path (from scontrol Command=) or a full
    sbatch command line (from sacct SubmitLine=, e.g. "sbatch --array=1-4 job.sh").

    If `job_id` is given and the original script file is gone, falls back to the
    archived copy of the script (see archive_batch_script) so a job whose script
    was moved or deleted can still be resubmitted.
    """
    from lazyslurm import config as persistent_config

    tokens = shlex.split(command) if command else []
    if not tokens:
        return False, "Resubmit failed: empty submit command"

    # Drop a leading "sbatch" from a full SubmitLine so we don't pass it as a script.
    if tokens[0] == "sbatch":
        tokens = tokens[1:]

    note = ""
    idx = _script_token_index(tokens)
    if job_id and idx is not None and not await _file_exists(tokens[idx]):
        archived = persistent_config.get_cached_script(job_id)
        if archived is None:
            return False, (
                f"Resubmit failed: script '{tokens[idx]}' no longer exists and "
                "no archived copy is available"
            )
        if _config.remote:
            # The archive is local but sbatch runs on the login node.
            return False, (
                f"Resubmit failed: script '{tokens[idx]}' no longer exists; the "
                "archived-script fallback is not supported in remote mode"
            )
        note = f" (original script missing — submitted archived copy {archived})"
        tokens[idx] = str(archived)

    args = (["--chdir", work_dir] if work_dir else []) + tokens

    stdout, stderr, rc = await _run_cmd("sbatch", *args)
    if rc == 0:
        return True, stdout.strip() + note
    return False, f"Resubmit failed: {stderr.strip()}"


# ---------------------------------------------------------------------------
# scontrol write batch_script – the job's sbatch script
# ---------------------------------------------------------------------------


async def get_batch_script(job_id: str) -> str | None:
    """Fetch a job's sbatch script text from Slurm, or None if unavailable.

    Only works while the job is still in slurmctld — same window as
    `scontrol show job`, i.e. until MinJobAge seconds after the job ends.

    Two things to know about this command:
      * The trailing "-" makes it write to stdout. Without it, scontrol drops a
        slurm-<id>.sh file into the current directory, which would litter the
        user's cwd and land on the wrong host in remote mode.
      * The exit code is 0 *even when retrieval fails* ("job script retrieval
        failed: Invalid job id specified" goes to stderr, stdout stays empty).
        So rc is deliberately ignored; non-empty stdout is the only success test.
    """
    stdout, _stderr, _rc = await _run_cmd(
        "scontrol", "write", "batch_script", job_id, "-",
    )
    return stdout if stdout.strip() else None


async def archive_batch_script(job_id: str, force: bool = False) -> Path | None:
    """Return a local path to the job's sbatch script, fetching it if needed.

    Uses the cached archive when present (arrays share one script via the base
    job id), otherwise fetches from Slurm and archives the text. Returns None
    when the script is neither cached nor still retrievable.
    """
    from lazyslurm import config as persistent_config

    if not force:
        cached = persistent_config.get_cached_script(job_id)
        if cached is not None:
            return cached

    text = await get_batch_script(job_id)
    if text is None:
        return None
    return persistent_config.cache_script(job_id, text)


# ---------------------------------------------------------------------------
# Cluster summary (sinfo + squeue counts)
# ---------------------------------------------------------------------------


async def get_partition_availability(config: Config | None = None) -> list[str]:
    """Return per-partition node availability strings from `sinfo`.

    Each entry looks like "gpu:10/5/0/15" (allocated/idle/other/total).
    Honors cfg.partition_order for display ordering.
    """
    cfg = config or _config
    stdout_si, _, _ = await _run_cmd(
        "sinfo", "--noheader", "--summarize", "--format=%P|%a|%F",
    )
    if not stdout_si.strip():
        return []

    # Parse sinfo: partition|availability|allocated/idle/other/total
    part_dict: dict[str, str] = {}
    for line in stdout_si.strip().splitlines():
        fields = line.split("|")
        if len(fields) >= 3:
            pname = fields[0].strip().rstrip("*")
            avail = fields[1].strip()
            nodes = fields[2].strip()  # e.g. "10/5/0/15"
            if avail == "up":
                part_dict[pname] = f"{pname}:{nodes}"

    if cfg.partition_order:
        ordered = [part_dict[p] for p in cfg.partition_order if p in part_dict]
        # Append remaining partitions not in the order list
        for p in part_dict:
            if p not in cfg.partition_order:
                ordered.append(part_dict[p])
        return ordered
    return list(part_dict.values())


def format_cluster_summary(
    running_jobs: list[RunningJob],
    part_info: list[str],
    config: Config | None = None,
) -> str:
    """Build the one-line cluster bar from already-fetched data (no I/O).

    Running/pending counts are derived from the squeue result we already have,
    avoiding a second squeue call per poll.
    """
    cfg = config or _config
    user = cfg.user or USER
    running = sum(1 for j in running_jobs if j.state == "RUNNING")
    pending = sum(1 for j in running_jobs if j.state == "PENDING")

    parts: list[str] = [
        f"[bold]{user}[/]",
        f"[green]{running}[/] running",
        f"[yellow]{pending}[/] pending",
    ]
    if part_info:
        parts.append("  " + "  ".join(part_info))
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Live node monitoring via SSH
# ---------------------------------------------------------------------------


async def get_node_processes(node: str, user: str = "") -> str:
    """Get a top-like process listing from a compute node via SSH."""
    if not node or node in ("N/A", "None", "(null)"):
        return "[dim]No node assigned[/]"

    first_node = _first_node(node)
    target_user = user or (_config.user if _config.user else USER)

    stdout, rc = await _ssh_cmd(
        first_node,
        f"ps -u {shlex.quote(target_user)} -o pid,%cpu,%mem,rss:10,vsz:10,etime,comm --sort=-%cpu --no-headers 2>/dev/null | head -30",
    )
    if rc != 0 or not stdout.strip():
        return f"[dim]Could not reach {first_node} (SSH failed)[/]"

    header = f"{'PID':>7}  {'%CPU':>5}  {'%MEM':>5}  {'RSS':>10}  {'VSZ':>10}  {'ELAPSED':>12}  COMMAND\n"
    separator = "-" * 72 + "\n"
    return f"[bold]Node: {first_node}[/]\n\n{header}{separator}{stdout}"


async def get_gpu_status(node: str, job_id: str = "") -> str:
    """Get nvidia-smi output for only the GPUs allocated to a job.

    Uses `srun --overlap --jobid` to run nvidia-smi inside the job's cgroup,
    which automatically restricts visibility to only allocated GPUs.
    Falls back to SSH-based nvidia-smi if srun is not available.
    """
    if not node or node in ("N/A", "None", "(null)"):
        return "[dim]No node assigned[/]"

    first_node = _first_node(node)

    # Strategy 1 (preferred): Run nvidia-smi inside the job's cgroup via srun.
    # Slurm's cgroup automatically restricts CUDA_VISIBLE_DEVICES,
    # so nvidia-smi only sees the allocated GPUs.
    if job_id:
        stdout, stderr, rc = await _run_cmd(
            "srun", "--overlap", f"--jobid={job_id}",
            "bash", "-c",
            "echo CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES; nvidia-smi 2>/dev/null || echo 'nvidia-smi not available'",
        )
        if rc == 0 and stdout.strip():
            # Extract CUDA_VISIBLE_DEVICES from first line for the header
            lines = stdout.strip().splitlines()
            cuda_line = lines[0] if lines[0].startswith("CUDA_VISIBLE_DEVICES=") else ""
            gpu_indices = cuda_line.split("=", 1)[1] if "=" in cuda_line else ""
            nvsmi_output = "\n".join(lines[1:]) if cuda_line else stdout.strip()

            header = f"[bold]Node: {first_node}[/]"
            if gpu_indices:
                header += f"  [dim](CUDA_VISIBLE_DEVICES={gpu_indices})[/]"
            return f"{header}\n\n{nvsmi_output}"

    # Strategy 2 (fallback): SSH to the node and run nvidia-smi directly.
    # This shows all GPUs on the node — not ideal but better than nothing.
    nvsmi_cmd = "nvidia-smi 2>/dev/null || echo 'nvidia-smi not available on this node'"
    stdout, rc = await _ssh_cmd(first_node, nvsmi_cmd)
    if rc != 0 or not stdout.strip():
        return f"[dim]Could not reach {first_node}[/]"

    header = f"[bold]Node: {first_node}[/]"
    if job_id:
        header += f"  [dim yellow](showing all GPUs — srun --overlap failed, falling back to SSH)[/]"
    return f"{header}\n\n{stdout}"


def _first_node(node_spec: str) -> str:
    """Extract the first node name from a Slurm node specification.

    Handles formats like 'node001', 'node[001-003]', 'node001,node002'.
    """
    if "," in node_spec and "[" not in node_spec:
        return node_spec.split(",")[0]
    if "[" in node_spec:
        prefix = node_spec.split("[")[0]
        inside = node_spec.split("[")[1].rstrip("]")
        first_range = inside.split(",")[0]
        first_num = first_range.split("-")[0]
        return f"{prefix}{first_num}"
    return node_spec
