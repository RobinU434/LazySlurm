"""Async wrappers around Slurm CLI commands."""

from __future__ import annotations

import asyncio
import os
import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable
from pathlib import Path

from lazyslurm.ssh import PromptCallback, SSHSession, quote_argv
from lazyslurm.models import (
    CompletedJob,
    array_task_count,
    Config,
    CoreSample,
    GpuReading,
    GpuSample,
    JobDetail,
    JobStats,
    NodeInfo,
    NodeSample,
    PartitionInfo,
    PartitionJob,
    FairShare,
    PriorityInfo,
    RunningJob,
    UsageRow,
    parse_duration,
    parse_mem_bytes,
)

USER = os.environ.get("USER", os.environ.get("LOGNAME", ""))

# Module-level config, set once from app.py via set_config().
_config: Config = Config()

# Options for local-mode SSH to compute nodes (live CPU/GPU tabs). Remote mode
# does not use these — it runs everything through the one session in ssh.py.
# Multiplexing still matters here: the first call opens a master connection and
# the rest reuse it, turning many handshakes per poll into one.
_SSH_CONTROL_DIR = Path.home() / ".ssh" / "cm-lazyslurm"

_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=3",
    "-o", "BatchMode=yes",
]
_SSH_TIMEOUT = 8  # seconds


def _control_opts() -> list[str]:
    """Multiplexing options, creating the control dir at the point of use.

    Importing this module must not touch ``~/.ssh`` — only a local-mode hop to
    a compute node needs the directory. If it cannot be created, drop the
    options and let each hop open its own connection.
    """
    try:
        _SSH_CONTROL_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        return []
    return [
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=60s",
        "-o", f"ControlPath={_SSH_CONTROL_DIR / '%C'}",
    ]

# Options for the login-node -> compute-node hop in remote mode. That inner ssh
# runs on the cluster, so it must never prompt: BatchMode makes it fail fast.
_NODE_SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "ConnectTimeout=3",
    "-o", "BatchMode=yes",
]


def _as_int(value: str | None) -> int:
    """Parse a Slurm numeric field, truncating toward zero. 0 when unparsable.

    Goes through ``float`` on purpose: Slurm reports CPUsLoad as ``16.02``.
    """
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def set_config(config: Config) -> None:
    """Set the module-level config (called at app startup, and on a config reload)."""
    global _config
    _config = config
    # The caches below are answers to a question the config asks; a new config
    # may be asking a different one.
    reset_caches()


# Where this module reports things the user should know about — set to the
# app's command log at startup. Without it, failures here have nowhere to go.
_notice_cb: Callable[[str, str], None] | None = None


def set_notice_callback(callback: Callable[[str, str], None] | None) -> None:
    """Route notices (action, detail) to the app's command log."""
    global _notice_cb
    _notice_cb = callback


def _notice(action: str, detail: str = "") -> None:
    """Best-effort report to the UI. Never fails the command it describes.

    The callback writes into a widget, so it can raise if the app is being torn
    down or was replaced — and a note about a command must not be able to break
    that command. Broad on purpose: whatever the UI does here, the caller's job
    is more important than the message.
    """
    if _notice_cb is None:
        return
    try:
        _notice_cb(action, detail)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Transport layer
# ---------------------------------------------------------------------------


# The background SSH sessions, one per cluster, keyed by SSH target. Every
# remote command runs in whichever one is currently attached.
#
# A dict rather than a single session because switching clusters should not
# cost a fresh login: a detached session stays open on its ControlMaster
# socket, so re-attaching is instant and silent where reconnecting would mean
# another 2FA prompt (#62).
_sessions: dict[str, SSHSession] = {}
_attached: str = ""


def get_session() -> SSHSession | None:
    """The session commands currently run in, or None in local mode."""
    return _sessions.get(_attached)


def attached_host() -> str:
    """The SSH target currently attached, or "" in local mode."""
    return _attached


def open_hosts() -> list[str]:
    """Every host with a session still open, attached or merely detached."""
    return sorted(_sessions)


async def connect_remote(
    prompt_cb: PromptCallback | None = None,
    config: Config | None = None,
) -> tuple[bool, str]:
    """Attach to `config.remote`, opening a session for it if there is none.

    `prompt_cb(prompt, is_secret)` is awaited whenever the cluster asks for a
    password or a 2FA verification code, and returns the answer (or None to
    abort). A no-op when not in remote mode.
    """
    global _attached
    cfg = config or _config
    if not cfg.remote:
        return True, "Local mode"

    existing = _sessions.get(cfg.remote)
    if existing is not None and existing.connected:
        # Detached, not closed: this is the switch that costs nothing.
        _attached = cfg.remote
        reset_caches()
        return True, f"Reattached to {cfg.remote}"
    if existing is not None:
        await existing.close()

    session = SSHSession(cfg.remote, prompt_cb=prompt_cb)
    ok, msg = await session.connect()
    if not ok:
        _sessions.pop(cfg.remote, None)
        return ok, msg
    _sessions[cfg.remote] = session
    _attached = cfg.remote
    reset_caches()
    return ok, msg


def detach_remote() -> str:
    """Stop running commands here, but leave the connection open.

    The counterpart to disconnect: coming back is instant, and on a cluster
    with two-factor authentication that is the difference between switching
    clusters freely and being asked for a code each time.
    """
    global _attached
    host, _attached = _attached, ""
    reset_caches()
    return host


async def disconnect_remote(host: str | None = None) -> str:
    """Close a session for real. Reconnecting means authenticating again.

    Defaults to the attached one. "Detached" is not the same as "closed": on a
    shared or untrusted machine, and for a cluster you are done with, a live
    master is not what you want left behind.
    """
    global _attached
    target = host if host is not None else _attached
    session = _sessions.pop(target, None)
    if session is not None:
        await session.close()
    if target == _attached:
        _attached = ""
        reset_caches()
    return target


async def disconnect_all() -> None:
    """Close every session (called when the app exits).

    Quitting closes everything, detached sessions included: a master outliving
    the app that made it is a surprise, and there is no UI left to find it with.
    """
    global _attached
    sessions = list(_sessions.values())
    _sessions.clear()
    _attached = ""
    for session in sessions:
        try:
            await session.close()
        except (OSError, RuntimeError):
            pass


# Commands already reported as unavailable, so a missing binary is mentioned
# once rather than on every poll.
_missing_commands: set[str] = set()


async def _run_cmd(*args: str) -> tuple[str, str, int]:
    """Run a command locally, or in the shared SSH session in remote mode.

    A missing Slurm binary comes back as rc=127 with a message, the same shape
    as any other failure — every caller already treats a non-zero rc as "no
    data". Raising instead took the whole app down on the first poll, which is
    what running LazySlurm off a cluster used to do.
    """
    if _config.remote:
        return await _run_remote(quote_argv(args))
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        if args and args[0] not in _missing_commands:
            _missing_commands.add(args[0])
            _notice("command unavailable", f"{args[0]}: {reason}")
        return "", f"{args[0] if args else 'command'}: {reason}", 127
    stdout, stderr = await proc.communicate()
    return (
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
        proc.returncode or 0,
    )


async def _run_remote(remote_cmd: str, timeout: float | None = None) -> tuple[str, str, int]:
    """Run a shell command on the login node through the shared session."""
    session = get_session()
    if session is None:
        return "", "No SSH session — remote mode is not connected", 1
    return await session.run(remote_cmd, timeout=timeout)


async def _ssh_cmd(node: str, remote_cmd: str) -> tuple[str, int]:
    """Run a command on a compute node.

    Local mode: SSH straight to the node. Remote mode: the hop to the node is
    made *from the login node*, inside the existing session, rather than with a
    local ProxyJump — a second local connection would trigger 2FA again.
    """
    if _config.remote:
        hop = "ssh " + " ".join(shlex.quote(o) for o in _NODE_SSH_OPTS)
        stdout, _, rc = await _run_remote(
            f"{hop} {shlex.quote(node)} {shlex.quote(remote_cmd)}"
        )
        return stdout, rc
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", *_SSH_OPTS, *_control_opts(), node, remote_cmd,
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
# Job ordering
# ---------------------------------------------------------------------------


def _split_row(line: str, count: int, free_text: int) -> list[str] | None:
    """Split a `|`-delimited Slurm row, tolerating `|` inside one free-text field.

    Neither squeue nor sacct escapes the delimiter, and neither lets you choose
    another one — but the only field a user controls is the job name, and every
    other field is generated by Slurm and cannot contain `|`. So a row with more
    fields than expected is not malformed: the surplus belongs to the name, and
    folding it back there keeps every later column aligned.

    Returns None when the row is genuinely too short. The one case still not
    recoverable is a `|` in the *work directory*, which is rarer still and would
    be indistinguishable from one in the name.
    """
    parts = line.split("|")
    if len(parts) < count:
        return None
    surplus = len(parts) - count
    if surplus:
        end = free_text + 1 + surplus
        parts = parts[:free_text] + ["|".join(parts[free_text:end])] + parts[end:]
    return parts


def job_sort_key(job_id: str) -> tuple[int, int]:
    """Sort key for a Slurm job id, used with ``reverse=True``.

    Array tasks carry a suffix (``2736118_11``, or ``2736118_[12-40]`` while
    the array is still pending), so a plain int() of the id fails and every
    array task used to collapse to the same key — scattering them through the
    table. Sorting on (base id, -task index) keeps each array together in the
    right place by submission order, with its tasks reading 0, 1, 2, ... down
    the block. Heterogeneous ids (``123+0``) and steps (``123.batch``) use the
    same split; anything unparseable sorts last.
    """
    head = job_id.strip()
    task = 0
    for sep in ("_", "+", "."):
        if sep in head:
            head, _, suffix = head.partition(sep)
            digits = "".join(c for c in suffix if c.isdigit() or c == "-")
            first = digits.split("-", 1)[0] if digits else ""
            task = int(first) if first.isdigit() else 0
            break
    if not head.isdigit():
        return (-1, 0)
    # Negated so that, under reverse=True, tasks within one array stay ascending.
    return (int(head), -task)


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
        parts = _split_row(line, 11, free_text=1)   # %j, the job name
        if parts is None:
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
    jobs.sort(key=lambda j: job_sort_key(j.job_id), reverse=True)
    return jobs


# ---------------------------------------------------------------------------
# sacct – completed / past jobs
# ---------------------------------------------------------------------------

_SACCT_FORMAT = "JobID,JobName,State,ExitCode,Start,End,Elapsed,Partition"

# States a job can still leave, so its row is not worth remembering yet. The
# next poll's window will pick it up again once it has settled.
_UNFINISHED_STATES = ("RUNNING", "PENDING", "REQUEUED")

# How far back a refresh looks, beyond the moment of the previous query.
#
# Two things make "since the last query" too tight. The login node's clock and
# slurmdbd's need not agree, and an accounting row can land slightly after the
# event it describes. Re-reading a couple of minutes costs nothing measurable
# (the window is priced by how many jobs fall in it, and almost none do) and
# removes both races.
_SACCT_OVERLAP = timedelta(minutes=2)

# How often to re-ask for the whole window regardless.
#
# Merging only ever adds or updates; it cannot notice a row that was revised
# retroactively into a shape the window no longer covers. A full query now and
# then keeps a session that stays open for days from drifting, and at ten
# minutes apart its cost disappears into the average.
_SACCT_RESYNC = timedelta(minutes=10)


@dataclass
class _CompletedCache:
    """Everything sacct has told us about this window, kept between polls."""

    key: tuple[str, int, str]
    jobs: dict[str, CompletedJob]
    queried_at: datetime
    full_at: datetime


_completed_cache: _CompletedCache | None = None

# How long the cluster bar's view of the partitions may be out of date. Nodes
# do not drain and come back on a five-second cadence.
_SINFO_TTL = timedelta(seconds=45)

# (partition_order, fetched at, rendered entries)
_partition_cache: tuple[tuple[str, ...], datetime, list[str]] | None = None


def reset_caches() -> None:
    """Forget everything remembered between polls.

    Called when the config changes under the module, and by tests, which share
    one imported module and would otherwise see each other's answers.
    """
    global _completed_cache, _partition_cache
    global _cluster_name
    _completed_cache = None
    _partition_cache = None
    _cluster_name = ""
    _stat_cache.clear()


def _parse_end_time(value: str) -> datetime | None:
    """The End column as a datetime, or None for "Unknown" and friends."""
    text = (value or "").strip()
    if not text or not text[0].isdigit():
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_sacct_jobs(stdout: str, partition: str = "") -> list[CompletedJob]:
    """Parse `sacct --parsable2` output into the jobs worth showing."""
    jobs: list[CompletedJob] = []
    for line in stdout.strip().splitlines():
        parts = _split_row(line, 8, free_text=1)    # JobName
        if parts is None:
            continue
        job_id = parts[0].strip()
        if "." in job_id:
            continue
        state = parts[2].strip()
        if state in _UNFINISHED_STATES:
            continue
        if partition and parts[7].strip() != partition:
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
    return jobs


async def _query_sacct(user: str, since: datetime) -> tuple[str, bool]:
    """Run sacct from `since`. Returns (stdout, ok)."""
    stdout, _, rc = await _run_cmd(
        "sacct",
        "-u", user,
        f"--format={_SACCT_FORMAT}",
        f"--starttime={since.strftime('%Y-%m-%dT%H:%M:%S')}",
        "--noheader",
        "--parsable2",
    )
    return stdout, rc == 0


async def get_completed_jobs(
    config: Config | None = None, *, full: bool = False,
) -> list[CompletedJob]:
    """Past jobs via sacct, sorted by job ID descending (latest first).

    The expensive query runs once. sacct's cost grows with the number of rows
    in the window -- a seven-day history takes ~110ms here, a thirty-day one
    ~1.5s -- and re-asking for all of it every few seconds returns a byte-
    identical answer, since a job that has ended does not change again.

    So the window is read in full once, kept, and afterwards only the part that
    can still move is re-read. That works because ``--starttime`` selects jobs
    in any state *during* the window rather than jobs that started in it: a job
    that began days ago and ended a moment ago is still in a two-minute window,
    which is exactly the job the refresh exists to notice.

    `full=True` forces the whole window to be re-read.
    """
    global _completed_cache
    cfg = config or _config
    days = cfg.days
    user = cfg.user or USER
    key = (user, days, cfg.partition or "")

    now = datetime.now()
    window_start = (now - timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    cache = _completed_cache
    if cache is not None and cache.key != key:
        # A different question entirely (another user, window or partition):
        # the remembered answers are about something else.
        cache = None

    incremental = (
        not full
        and cache is not None
        and now - cache.full_at < _SACCT_RESYNC
    )
    since = max(cache.queried_at - _SACCT_OVERLAP, window_start) if incremental else window_start

    stdout, ok = await _query_sacct(user, since)
    if not ok:
        # Keep serving what we have: a transient sacct failure should not blank
        # the table, and the next poll will merge on top of it.
        return _sorted(cache.jobs.values()) if cache is not None else []

    fresh = {job.job_id: job for job in parse_sacct_jobs(stdout, cfg.partition)}
    if incremental:
        assert cache is not None
        merged = dict(cache.jobs)
        merged.update(fresh)
        # A job leaves the window when it ended before its start, the same way
        # it would have simply stopped appearing in a full query.
        merged = {
            job_id: job
            for job_id, job in merged.items()
            if (end := _parse_end_time(job.end)) is None or end >= window_start
        }
        full_at = cache.full_at
    else:
        merged, full_at = fresh, now

    _completed_cache = _CompletedCache(
        key=key, jobs=merged, queried_at=now, full_at=full_at,
    )
    return _sorted(merged.values())


def _sorted(jobs: Iterable[CompletedJob]) -> list[CompletedJob]:
    return sorted(jobs, key=lambda j: job_sort_key(j.job_id), reverse=True)


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
        except (OSError, asyncio.TimeoutError, ValueError) as exc:
            # Never let archiving break detail loading — but say so, because
            # this is the only window in which the script can still be saved,
            # and the user would otherwise discover the gap days later.
            _notice("archive script", f"{job_id}: {exc}")
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
        parts = _split_row(line, 20, free_text=1)   # JobName
        if parts is None:
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
        ])
    # Also check logs/ subdirectory
    candidates.extend([
        os.path.join(work_dir, "logs", f"slurm-{job_id}.{ext_out}"),
        os.path.join(work_dir, "log", f"slurm-{job_id}.{ext_out}"),
    ])

    # Every candidate costs a stat — a full SSH round trip in remote mode — so
    # never probe the same path twice (the first two coincide when suffix=="out").
    for path in dict.fromkeys(candidates):
        if await _file_exists(path):
            return path
    return None


async def _file_exists(path: str) -> bool:
    """Check if a file exists (locally or on the remote host)."""
    if _config.remote:
        _, _, rc = await _run_remote(f"test -f {shlex.quote(path)}")
        return rc == 0
    return await asyncio.to_thread(os.path.isfile, path)


# ---------------------------------------------------------------------------
# Interactive shell on a compute node (the `o` key)
# ---------------------------------------------------------------------------

# The two ways to get a shell on a job's node, and why a user would pick one:
#
#   ssh   Lands on the machine, outside the job's cgroup. No CUDA_VISIBLE_DEVICES,
#         all node GPUs visible, nothing capped by the job's limits. Adds no job
#         step, so it cannot skew the efficiency report the tool also shows, and
#         it either connects or fails fast rather than blocking on Slurm state.
#   srun  Lands inside the allocation, so the environment matches what the job
#         sees. Costs a job step in sacct — an idle debugging shell drags the
#         job's reported CPU efficiency down — needs Slurm >= 20.11 for
#         --overlap, and can block negotiating the step launch. Required on
#         clusters where pam_slurm_adm refuses SSH without an allocation.
INTERACTIVE_SHELLS: tuple[str, ...] = ("ssh", "srun")


def interactive_shell_cmd(
    method: str,
    node: str,
    job_id: str = "",
    remote: str = "",
    control_opt: str = "",
    shell: str = "bash",
) -> str:
    """Build the command that opens an interactive shell on a compute node.

    Pure string building, so the choice is testable without a terminal. In
    remote mode both paths ride the already-authenticated master socket:
    ``ssh`` hops with a ProxyCommand (``-J`` would open a second connection and
    ask for the 2FA code again), and ``srun`` runs on the login node itself.
    """
    def join(*parts: str) -> str:
        return " ".join(p for p in parts if p)

    if method == "srun":
        srun = f"srun --overlap --jobid={shlex.quote(job_id)} --pty {shlex.quote(shell)}"
        if remote:
            # -t forces a pty on the login node, which srun --pty needs.
            return join("ssh", "-t", control_opt, shlex.quote(remote), shlex.quote(srun))
        return srun

    if remote:
        proxy = join("ssh", control_opt, "-W %h:%p", shlex.quote(remote))
        return join("ssh", "-o", shlex.quote("ProxyCommand=" + proxy), shlex.quote(node))
    return join("ssh", shlex.quote(node))


# ---------------------------------------------------------------------------
# Account usage and fairshare (sreport + sshare)
# ---------------------------------------------------------------------------

# Selectable windows for the usage panel. Each returns (start, end, label);
# sreport takes "now" and plain dates, so no clock arithmetic is needed beyond
# finding the first of the month or the year.
USAGE_WINDOWS: tuple[str, ...] = ("month", "30d", "year")

_WINDOW_LABELS = {
    "month": "this month",
    "30d": "last 30 days",
    "year": "this year",
}


def usage_window(window: str, today: datetime | None = None) -> tuple[str, str, str]:
    """(start, end, label) for a window key — sreport-ready date strings."""
    now = today or datetime.now()
    if window == "year":
        start = now.replace(month=1, day=1).strftime("%Y-%m-%d")
    elif window == "30d":
        start = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    else:
        window = "month"
        start = now.replace(day=1).strftime("%Y-%m-%d")
    return start, "now", _WINDOW_LABELS[window]


def next_usage_window(window: str) -> str:
    """Cycle month -> 30d -> year -> month."""
    try:
        index = USAGE_WINDOWS.index(window)
    except ValueError:
        return USAGE_WINDOWS[0]
    return USAGE_WINDOWS[(index + 1) % len(USAGE_WINDOWS)]


def parse_sreport(stdout: str) -> list[UsageRow]:
    """Parse `sreport ... -P` output.

    sreport prints a banner of dashes and a title before the parsable rows, and
    the column header itself is parsable-looking, so rows are recognised by
    content rather than position: a header starts with "Cluster", banners have
    no separator, and the hours column must be a number.
    """
    rows: list[UsageRow] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or "|" not in line or line.startswith("-"):
            continue
        fields = [f.strip() for f in line.split("|")]
        if len(fields) < 5 or fields[0].lower() in ("cluster", "cluster/account/user"):
            continue
        try:
            hours = float(fields[4].replace(",", ""))
        except ValueError:
            continue  # the header row, or a line we do not understand
        rows.append(UsageRow(
            account=fields[1], user=fields[2], name=fields[3], hours=hours,
        ))
    return rows


def parse_sshare(stdout: str) -> list[FairShare]:
    """Parse `sshare -P -o Account,User,RawShares,NormShares,RawUsage,EffectvUsage,FairShare`."""
    shares: list[FairShare] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        fields = [f.strip() for f in line.split("|")]
        if len(fields) < 7 or fields[0].lower() == "account":
            continue

        def _float(text: str) -> float:
            try:
                return float(text)
            except ValueError:
                return 0.0

        factor: float | None
        try:
            factor = float(fields[6])
        except ValueError:
            factor = None   # account rows leave FairShare empty
        shares.append(FairShare(
            account=fields[0],
            user=fields[1],
            raw_shares=fields[2],
            norm_shares=_float(fields[3]),
            raw_usage=_float(fields[4]),
            effective_usage=_float(fields[5]),
            fairshare=factor,
        ))
    return shares


# Remembered once accounting turns out to be unavailable, so the panel can say
# so instead of showing an empty table.
_accounting_missing = False


def accounting_available() -> bool:
    return not _accounting_missing


def _note_accounting_failure(stderr: str) -> None:
    global _accounting_missing
    text = stderr.lower()
    if any(m in text for m in ("not found", "no such file", "not configured",
                               "accounting_storage", "slurmdbd")):
        _accounting_missing = True


async def get_account_usage(
    window: str = "month",
    account: str = "",
    today: datetime | None = None,
) -> list[UsageRow]:
    """Per-user hours in the account over the given window, largest first."""
    start, end, _ = usage_window(window, today)
    cmd = [
        "sreport", "cluster", "AccountUtilizationByUser",
        f"start={start}", f"end={end}", "-t", "hours", "-P", "--noheader",
    ]
    if account:
        cmd.append(f"account={account}")
    stdout, stderr, rc = await _run_cmd(*cmd)
    if rc != 0:
        _note_accounting_failure(stderr)
        return []
    rows = parse_sreport(stdout)
    rows.sort(key=lambda r: (r.is_account_total, -r.hours))
    return rows


async def get_fairshare(user: str = "") -> list[FairShare]:
    """Fairshare rows for a user (their own associations)."""
    cmd = ["sshare", "-P", "-o",
           "Account,User,RawShares,NormShares,RawUsage,EffectvUsage,FairShare"]
    cmd += ["-u", user] if user else ["-U"]
    stdout, stderr, rc = await _run_cmd(*cmd)
    if rc != 0:
        _note_accounting_failure(stderr)
        return []
    return parse_sshare(stdout)


# ---------------------------------------------------------------------------
# Why is this job pending? (sprio + what scontrol already told us)
# ---------------------------------------------------------------------------

# jobid|priority|age|fairshare|jobsize|partition|qos
_SPRIO_FORMAT = "%i|%Y|%A|%F|%J|%P|%Q"


def parse_sprio(stdout: str, job_id: str) -> PriorityInfo | None:
    """Pull one job's priority factors, and its rank, out of sprio output.

    The command is asked for a whole partition rather than a single job, so the
    same output yields both the breakdown and the job's position in the queue —
    one call instead of two.
    """
    target = job_id.strip()
    rows: list[tuple[str, int]] = []
    found: PriorityInfo | None = None

    for line in stdout.strip().splitlines():
        fields = [f.strip() for f in line.split("|")]
        if len(fields) < 7 or not fields[0] or fields[0].upper() == "JOBID":
            continue

        def _int(index: int) -> int:
            try:
                return int(float(fields[index]))
            except ValueError:
                return 0

        rows.append((fields[0], _int(1)))
        if fields[0] == target:
            found = PriorityInfo(
                job_id=fields[0],
                total=_int(1),
                age=_int(2),
                fairshare=_int(3),
                job_size=_int(4),
                partition=_int(5),
                qos=_int(6),
            )

    if found is None:
        return None
    found.queued = len(rows)
    found.rank = sum(1 for _, total in rows if total > found.total) + 1
    return found


# Set once sprio turns out not to exist, so the UI can say *why* the breakdown
# is missing instead of just leaving a hole.
_sprio_missing = False


def sprio_available() -> bool:
    """False once sprio has been found to be missing on this cluster."""
    return not _sprio_missing


async def get_job_priority(job_id: str, partition: str = "") -> PriorityInfo | None:
    """Priority factors and queue position for a pending job.

    Returns None when sprio is missing, priority accounting is off, or the job
    is no longer pending — the caller shows a plain message instead.
    """
    global _sprio_missing
    if not job_id:
        return None
    cmd = ["sprio", "--noheader", f"--format={_SPRIO_FORMAT}"]
    if partition and partition not in ("N/A", "None"):
        cmd += ["-p", partition]
    try:
        stdout, stderr, rc = await _run_cmd(*cmd)
    except FileNotFoundError:  # local mode, sprio not installed
        _sprio_missing = True
        return None
    if rc != 0:
        if "not found" in stderr.lower() or "no such file" in stderr.lower():
            _sprio_missing = True
        return None
    if not stdout.strip():
        return None
    return parse_sprio(stdout, job_id)


def format_start_estimate(raw: str, now: datetime | None = None) -> str:
    """Turn scontrol's StartTime into "~14:20 (in 2h10m)".

    Slurm fills StartTime for a pending job with its backfill estimate, so no
    extra `squeue --start` call is needed. It says Unknown when it cannot
    estimate — usually because the job is blocked rather than merely queued.
    """
    value = (raw or "").strip()
    if not value or value in ("Unknown", "N/A", "None", "(null)"):
        return "not estimated yet — Slurm cannot schedule it while it is blocked"
    try:
        start = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return value

    now = now or datetime.now()
    delta = start - now
    seconds = int(delta.total_seconds())
    when = start.strftime("%H:%M") if start.date() == now.date() else start.strftime("%b %d %H:%M")
    if seconds <= 0:
        return f"~{when} (due now)"
    return f"~{when} (in {_humanize(seconds)})"


def _humanize(seconds: int) -> str:
    """43870 -> "12h11m", 300 -> "5m", 200000 -> "2d7h"."""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"
    return f"{minutes}m" if minutes else "<1m"


# Slurm's reason codes, in words. Matched exactly first, then by prefix, so the
# whole QOSMax*/AssocMax* family is covered without listing every variant.
_REASON_TEXT: dict[str, str] = {
    "Resources": "waiting for enough free nodes to become available",
    "Priority": "other jobs are ahead of it in the queue",
    "Dependency": "waiting for another job to finish",
    "DependencyNeverSatisfied": "its dependency can never be satisfied — cancel it",
    "BeginTime": "held until its requested start time",
    "JobHeldUser": "held by you — release it with `scontrol release`",
    "JobHeldAdmin": "held by an administrator",
    "Licenses": "waiting for a software license",
    "ReqNodeNotAvail": "the nodes it asked for are down, drained or reserved",
    "PartitionDown": "its partition is down",
    "PartitionInactive": "its partition is inactive",
    "PartitionNodeLimit": "it asks for more nodes than the partition allows",
    "PartitionTimeLimit": "its time limit is longer than the partition allows",
    "NodeDown": "a node it needs is down",
    "Cleaning": "a previous job is still being cleaned up on its nodes",
    "None": "it should start shortly",
}

_REASON_PREFIX: list[tuple[str, str]] = [
    ("QOSMaxGRES", "you are at your QOS limit for GPUs"),
    ("QOSMaxCpu", "you are at your QOS limit for CPUs"),
    ("QOSMaxNode", "you are at your QOS limit for nodes"),
    ("QOSMaxMem", "you are at your QOS limit for memory"),
    ("QOSMaxJobs", "you are at your QOS limit for running jobs"),
    ("QOSMaxSubmit", "you are at your QOS limit for submitted jobs"),
    ("QOSMaxWall", "its time limit is longer than the QOS allows"),
    ("QOSGrp", "your QOS group is at its resource limit"),
    ("QOSMin", "it asks for less than the QOS minimum"),
    ("QOSNotAllowed", "that QOS is not allowed on this partition"),
    ("QOSResourceLimit", "the QOS resource limit is reached"),
    ("AssocMaxJobs", "you are at your account's limit for running jobs"),
    ("AssocMaxWall", "its time limit is longer than your account allows"),
    ("AssocGrp", "your account group is at its resource limit"),
    ("AssocMax", "you are at an account resource limit"),
    ("ReqNodeNotAvail", "the nodes it asked for are down, drained or reserved"),
    ("Reservation", "waiting for its reservation to begin"),
]


def explain_reason(
    reason: str,
    raw: dict[str, str] | None = None,
    priority: PriorityInfo | None = None,
) -> str:
    """Say why a job is pending, in a sentence.

    Falls back to the raw code when Slurm reports something unrecognized —
    better a code than a wrong explanation.
    """
    code = (reason or "").strip().split("(")[0].strip()
    if not code or code in ("N/A", "none"):
        return "no reason reported"

    text = _REASON_TEXT.get(code)
    if text is None:
        for prefix, phrase in _REASON_PREFIX:
            if code.startswith(prefix):
                text = phrase
                break
    if text is None:
        return f"Slurm says: {code}"

    # Fill in the specifics Slurm hands us elsewhere.
    if code == "Priority" and priority is not None and priority.ahead:
        plural = "job" if priority.ahead == 1 else "jobs"
        text = f"{priority.ahead} {plural} ahead of it in the queue"
    elif code.startswith("Dependency") and raw:
        dependency = (raw.get("Dependency") or "").strip()
        if dependency and dependency not in ("(null)", "None"):
            text = f"waiting on {dependency}"
    return text


# ---------------------------------------------------------------------------
# sstat – resource usage for running jobs
# ---------------------------------------------------------------------------

_SSTAT_FORMAT = (
    "AveCPU,AveCPUFreq,AveRSS,MaxRSS,AveVMSize,MaxVMSize,"
    "AveDiskRead,AveDiskWrite,MaxDiskRead,MaxDiskWrite,"
    "MaxRSSNode,MaxRSSTask"
)

# JobID first so step rows can be told apart from the job row: sacct puts
# ReqMem/Timelimit only on the job row and MaxRSS only on the step rows.
_SACCT_STATS_FORMAT = (
    "JobID,TotalCPU,Elapsed,ReqMem,AllocTRES,ReqTRES,"
    "AllocCPUS,NNodes,NTasks,Timelimit,MaxRSS"
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
        stats.time_limit = sacct_result.get("Timelimit", "N/A")
        stats.alloc_cpus = _as_int(sacct_result.get("AllocCPUS"))
        stats.nnodes = _as_int(sacct_result.get("NNodes"))
        stats.ntasks = _as_int(sacct_result.get("NTasks"))
        # sstat has no MaxRSS for a finished job; sacct's steps do.
        if stats.max_rss in ("N/A", "", None) and sacct_result.get("MaxRSS"):
            stats.max_rss = sacct_result["MaxRSS"]
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

    return parse_sacct_stats(stdout)


def parse_sacct_stats(stdout: str) -> dict[str, str] | None:
    """Fold sacct's job row and its step rows into one set of numbers.

    sacct splits what the efficiency report needs across rows: the job row
    carries the request (ReqMem, Timelimit, AllocCPUS) but no MaxRSS, while
    each step row carries a MaxRSS and nothing about the request. The peak
    across steps is the job's memory high-water mark, which is what seff
    reports and what `.batch` alone would understate.
    """
    fields: dict[str, str] = {}
    peak_rss = 0.0
    peak_text = ""

    for line in stdout.strip().splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 11 or not parts[0] or parts[0].upper() == "JOBID":
            continue
        job_row = "." not in parts[0]

        if job_row and not fields:
            fields = {
                "TotalCPU": parts[1],
                "Elapsed": parts[2],
                "ReqMem": parts[3],
                "AllocTRES": parts[4],
                "ReqTRES": parts[5],
                "AllocCPUS": parts[6],
                "NNodes": parts[7],
                "NTasks": parts[8],
                "Timelimit": parts[9],
            }

        size = parse_mem_bytes(parts[10])
        if size is not None and size > peak_rss:
            peak_rss, peak_text = size, parts[10]

    if not fields:
        return None
    if peak_text:
        fields["MaxRSS"] = peak_text
    return fields


# ---------------------------------------------------------------------------
# Log file reading
# ---------------------------------------------------------------------------

TAIL_LINES = 500

# Never pull more than this off disk for one tail, however long the lines are.
# A training log with a single 200 MB progress-bar "line" must not be read whole.
_TAIL_MAX_BYTES = 4 * 1024 * 1024
_TAIL_BLOCK = 64 * 1024


def tail_file(path: str, tail_lines: int = TAIL_LINES) -> str:
    """Return the last `tail_lines` lines, reading only the end of the file.

    Job logs routinely reach hundreds of megabytes on a shared filesystem, so
    this seeks backwards in blocks instead of iterating the whole file — the
    cost is the size of the tail, not the size of the log.
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        data = b""
        while pos > 0 and data.count(b"\n") <= tail_lines and len(data) < _TAIL_MAX_BYTES:
            step = min(_TAIL_BLOCK, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data

    truncated = pos > 0 and (data.count(b"\n") <= tail_lines)
    text = data.decode(errors="replace")
    lines = text.splitlines(keepends=True)
    # The first line is partial unless we reached the start of the file.
    if pos > 0 and lines:
        lines = lines[1:]
    out = "".join(lines[-tail_lines:])
    if truncated:
        size = _TAIL_MAX_BYTES
        human = f"{size // (1024 * 1024)} MB" if size >= 1024 * 1024 else f"{size // 1024} KB"
        out = (
            f"... (truncated: no line break in the last {human} — "
            "press 'l' to open the whole file in the pager)\n"
        ) + out
    return out


async def read_log_file(path: str | None, tail_lines: int = TAIL_LINES) -> str:
    """Read the tail of a log file (locally or via SSH in remote mode)."""
    if not path:
        return "(no log file path available)"

    if _config.remote:
        # The "file not found" text is a local UI string: building it on the
        # cluster meant interpolating the path into a single-quoted echo, where
        # a path containing an apostrophe closed the quote and handed the rest
        # to the remote shell. The caller below already covers the empty case.
        stdout, _, rc = await _run_remote(f"tail -n {tail_lines} {shlex.quote(path)}")
        return stdout if stdout.strip() else f"(file not found: {path})"

    if not os.path.isfile(path):
        return f"(file not found: {path})"

    try:
        return await asyncio.to_thread(tail_file, path, tail_lines)
    except OSError as e:
        return f"(could not read {path}: {e})"


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


# ---------------------------------------------------------------------------
# scontrol update – edit properties of a pending job
# ---------------------------------------------------------------------------

# Editable properties: field key -> (label, scontrol key, RunningJob attribute).
# Only fields Slurm accepts for a *pending* job are offered; a running job's
# allocation is fixed, so app.py refuses to open the editor for one.
EDITABLE_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("time_limit", "Runtime", "TimeLimit", "time_limit"),
    ("partition", "Partition", "Partition", "partition"),
    ("nodes", "Nodes", "NumNodes", "nodes"),
    ("cpus", "CPUs", "NumCPUs", "cpus"),
    ("memory", "Memory/node", "MinMemoryNode", "memory"),
)

_MEM_SUFFIX_MB = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}


def normalize_memory(value: str) -> str:
    """Convert a squeue-style memory string to the MB integer scontrol wants.

    ``40G`` -> ``40960``, ``4000M`` -> ``4000``, ``512`` -> ``512``. Trailing
    ``n``/``c`` (squeue's per-node/per-cpu marker) is stripped. Anything that
    does not parse is passed through untouched so Slurm can report the error.
    """
    v = value.strip().rstrip("nc")
    if not v:
        return value.strip()
    suffix = v[-1].upper()
    if suffix in _MEM_SUFFIX_MB:
        number = v[:-1]
        factor = _MEM_SUFFIX_MB[suffix]
    else:
        number, factor = v, 1
    try:
        mb = float(number) * factor
    except ValueError:
        return value.strip()
    return str(int(mb))


def build_update_args(updates: dict[str, str]) -> list[str]:
    """Turn ``{field_key: value}`` into ``Key=Value`` scontrol arguments."""
    by_key = {key: scontrol_key for key, _, scontrol_key, _ in EDITABLE_FIELDS}
    args: list[str] = []
    for key, value in updates.items():
        value = value.strip()
        if not value:
            continue
        scontrol_key = by_key.get(key, key)
        if key == "memory":
            value = normalize_memory(value)
        args.append(f"{scontrol_key}={value}")
    return args


async def update_job(job_id: str, updates: dict[str, str]) -> tuple[bool, str]:
    """Apply property changes to a pending job via ``scontrol update``.

    ``updates`` maps EDITABLE_FIELDS keys to their new values; empty values are
    skipped so a blank input means "leave unchanged". Returns (success, msg).
    """
    args = build_update_args(updates)
    if not args:
        return False, f"Job {job_id}: nothing to update."
    _, stderr, rc = await _run_cmd("scontrol", "update", f"jobid={job_id}", *args)
    if rc == 0:
        return True, f"Job {job_id} updated: {' '.join(args)}"
    return False, f"Failed to update job {job_id}: {stderr.strip()}"


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


# EDITABLE_FIELDS key -> the sbatch flag and its aliases. The aliases matter
# because an override has to *replace* the same setting in the original submit
# line, and the user may have written it in any of these spellings.
_SBATCH_FLAGS: dict[str, tuple[str, tuple[str, ...]]] = {
    "time_limit": ("--time", ("--time", "-t")),
    "partition": ("--partition", ("--partition", "-p")),
    "nodes": ("--nodes", ("--nodes", "-N")),
    "cpus": ("--cpus-per-task", ("--cpus-per-task", "-c")),
    "memory": ("--mem", ("--mem",)),
}


def _drop_flag(tokens: list[str], aliases: tuple[str, ...], limit: int) -> list[str]:
    """Remove every occurrence of ``aliases`` from ``tokens[:limit]``.

    Covers the three spellings sbatch accepts: ``--time=1:00``, ``--time 1:00``
    and the short ``-t1:00``. Only options before the script are touched —
    anything after it belongs to the script, not to sbatch.
    """
    out: list[str] = []
    skip_next = False
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if i < limit:
            if tok in aliases:
                # Separate form: this token and its value both go.
                skip_next = True
                continue
            if any(tok.startswith(a + "=") for a in aliases):
                continue
            if any(len(a) == 2 and tok.startswith(a) and len(tok) > 2 for a in aliases):
                continue
        out.append(tok)
    return out


def build_resubmit_tokens(
    tokens: list[str], overrides: dict[str, str] | None,
) -> list[str]:
    """Apply resource overrides to an sbatch token list.

    Each override replaces the same option in the original line rather than
    being appended next to it — sbatch would take the later one, but leaving
    both in makes the command log lie about what was requested. A blank value
    means "keep whatever the original had", so it changes nothing.
    """
    if not overrides:
        return tokens
    for key, value in overrides.items():
        value = value.strip()
        if not value or key not in _SBATCH_FLAGS:
            continue
        flag, aliases = _SBATCH_FLAGS[key]
        if key == "memory":
            # squeue writes a trailing n/c (per-node / per-cpu); sbatch takes
            # the plain size, suffix and all ("40G", "4000M", "512").
            value = value.strip().rstrip("nc") or value.strip()
        limit = _script_token_index(tokens)
        tokens = _drop_flag(tokens, aliases, len(tokens) if limit is None else limit)
        limit = _script_token_index(tokens)
        at = len(tokens) if limit is None else limit
        tokens = tokens[:at] + [f"{flag}={value}"] + tokens[at:]
    return tokens


def suggest_resubmit_overrides(
    state: str, current: dict[str, str],
) -> dict[str, str]:
    """What to change after a failure, as EDITABLE_FIELDS-keyed values.

    A job that hit its wall clock wants more time; one the OOM killer took
    wants more memory. Doubling is the convention the user would have applied
    by hand. Only ever a prefilled suggestion — the editor shows it as the
    field's value, so it can be overridden or cleared.
    """
    upper = (state or "").upper()
    if upper.startswith("TIMEOUT"):
        seconds = parse_duration(current.get("time_limit", ""))
        if seconds:
            return {"time_limit": format_walltime(seconds * 2)}
    if upper.startswith("OUT_OF_MEMORY") or upper.startswith("OOM"):
        mb = _memory_mb(current.get("memory", ""))
        if mb:
            return {"memory": f"{int(mb * 2)}M"}
    return {}


def format_walltime(seconds: float) -> str:
    """Seconds as the ``D-HH:MM:SS`` / ``HH:MM:SS`` sbatch --time accepts.

    Not models.format_duration, which renders ``6:37:27`` for humans and drops
    the day field that Slurm needs for anything past 24 hours.
    """
    total = max(0, int(seconds))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    stamp = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}-{stamp}" if days else stamp


def _memory_mb(value: str) -> float:
    """A memory string in MB, or 0 when it does not parse."""
    text = (value or "").strip().rstrip("nc")
    if not text:
        return 0.0
    suffix = text[-1].upper()
    factor = _MEM_SUFFIX_MB.get(suffix)
    number = text[:-1] if factor else text
    try:
        return float(number) * (factor or 1)
    except ValueError:
        return 0.0


async def resubmit_job(
    command: str,
    work_dir: str,
    job_id: str | None = None,
    overrides: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Resubmit a job using its original sbatch command. Returns (success, msg).

    `command` is either a script path (from scontrol Command=) or a full
    sbatch command line (from sacct SubmitLine=, e.g. "sbatch --array=1-4 job.sh").

    `overrides` maps EDITABLE_FIELDS keys to new resource values ("run it again,
    but with more time"); each replaces the same option in the original line,
    and a blank one changes nothing.

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

    tokens = build_resubmit_tokens(tokens, overrides)
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
# Partitions (sinfo) — cluster summary bar and the partition monitor screen
# ---------------------------------------------------------------------------

# partition|avail|nodes A/I/O/T|cpus A/I/O/T|time limit|gres
_SINFO_FORMAT = "%P|%a|%F|%C|%l|%G"


def _aiot(field: str) -> tuple[int, int, int, int]:
    """Parse Slurm's "allocated/idle/other/total" counter string."""
    parts = field.split("/")
    if len(parts) != 4:
        return (0, 0, 0, 0)
    out = []
    for p in parts:
        try:
            out.append(int(p.strip()))
        except ValueError:
            out.append(0)
    return tuple(out)  # type: ignore[return-value]


def parse_sinfo(stdout: str) -> list[PartitionInfo]:
    """Parse `sinfo --summarize --format=_SINFO_FORMAT` into PartitionInfo.

    `--summarize` still emits one row per *node configuration*, so a partition
    with mixed hardware (different memory or GRES) appears several times — the
    rows are summed here so each partition shows up exactly once. Trailing
    fields are optional, which keeps the cluster bar working against the
    shorter `%P|%a|%F` output too.
    """
    by_name: dict[str, PartitionInfo] = {}
    for line in stdout.strip().splitlines():
        fields = [f.strip() for f in line.split("|")]
        if len(fields) < 3:
            continue
        name = fields[0].rstrip("*")
        n_a, n_i, n_o, n_t = _aiot(fields[2])
        c_a, c_i, c_o, c_t = _aiot(fields[3]) if len(fields) > 3 else (0, 0, 0, 0)
        gres = fields[5] if len(fields) > 5 else ""
        if gres in ("(null)", "N/A"):
            gres = ""

        part = by_name.get(name)
        if part is None:
            part = PartitionInfo(
                name=name,
                avail=fields[1],
                time_limit=fields[4] if len(fields) > 4 else "",
                gres=gres,
            )
            by_name[name] = part
        # Membership over the specs collected so far, not a substring test on
        # the joined string: "gpu:a100:8" is a substring of "gpu:a100:80".
        elif gres and gres not in part.gres.split(","):
            part.gres = f"{part.gres},{gres}" if part.gres else gres
        part.nodes_alloc += n_a
        part.nodes_idle += n_i
        part.nodes_other += n_o
        part.nodes_total += n_t
        part.cpus_alloc += c_a
        part.cpus_idle += c_i
        part.cpus_other += c_o
        part.cpus_total += c_t
    return list(by_name.values())


def order_partitions(parts: list[PartitionInfo], config: Config | None = None) -> list[PartitionInfo]:
    """Apply cfg.partition_order, appending anything not named in it."""
    cfg = config or _config
    if not cfg.partition_order:
        return parts
    by_name = {p.name: p for p in parts}
    ordered = [by_name[n] for n in cfg.partition_order if n in by_name]
    ordered += [p for p in parts if p.name not in cfg.partition_order]
    return ordered


def parse_partition_job_counts(stdout: str) -> dict[str, tuple[int, int]]:
    """Count running/pending jobs per partition from `squeue %P|%T` output.

    A pending job may list several partitions ("gpu,gpu-long"); it counts
    towards each, since it could start on any of them.
    """
    counts: dict[str, list[int]] = {}
    for line in stdout.strip().splitlines():
        fields = line.split("|")
        if len(fields) < 2:
            continue
        state = fields[1].strip()
        for name in fields[0].strip().split(","):
            name = name.strip().rstrip("*")
            if not name:
                continue
            entry = counts.setdefault(name, [0, 0])
            if state == "RUNNING":
                entry[0] += 1
            elif state == "PENDING":
                entry[1] += 1
    return {name: (run, pend) for name, (run, pend) in counts.items()}


async def get_partition_job_counts() -> dict[str, tuple[int, int]]:
    """Running/pending job counts per partition, across all users."""
    stdout, _, rc = await _run_cmd(
        "squeue", "--noheader", "--format=%P|%T", "--states=RUNNING,PENDING",
    )
    if rc != 0 or not stdout.strip():
        return {}
    return parse_partition_job_counts(stdout)


async def get_partitions(config: Config | None = None) -> list[PartitionInfo]:
    """Fetch every partition's node/CPU state from `sinfo`.

    Unlike the cluster bar this keeps unavailable ("down") partitions — the
    monitor screen shows them greyed out rather than hiding them.
    """
    cfg = config or _config
    stdout, counts = await asyncio.gather(
        _run_cmd("sinfo", "--noheader", "--summarize", f"--format={_SINFO_FORMAT}"),
        get_partition_job_counts(),
    )
    out, _, rc = stdout
    if rc != 0 or not out.strip():
        return []
    parts = parse_sinfo(out)
    for part in parts:
        part.running, part.pending = counts.get(part.name, (0, 0))
    return order_partitions(parts, cfg)


_cluster_name: str = ""


def _remote_host(remote: str) -> str:
    """The host part of an SSH target: ``me@login.hpc.edu:22`` -> ``login.hpc.edu``."""
    host = (remote or "").strip().rpartition("@")[2]
    return host.partition(":")[0].strip().lower()


async def get_cluster_name(config: Config | None = None) -> str:
    """What to call the cluster these job ids belong to.

    Job ids are per-cluster, so the caches need to know which one they are
    holding (#61). Three sources, in decreasing order of how well they identify
    a cluster rather than a route to one:

    1. ``cluster_name`` in config.toml -- the escape hatch, and the only thing
       that can be right when the rest is not.
    2. Slurm's own ``ClusterName``. This is precisely the identity wanted: it is
       what Slurm uses to tell clusters apart in accounting, it is the same from
       every login node, and it does not change with how you connected. One
       command, once per session.
    3. The ``--remote`` host, lowercased. Free, but a route rather than an
       identity: ``login.hpc.edu`` and ``hpc.edu`` would be two names for one
       cluster.

    Deliberately not the login node's IP address: those are routinely
    round-robin, so keying on one would split a single cluster's cache into
    several and reintroduce the miss this exists to prevent.
    """
    global _cluster_name
    cfg = config or _config
    if _cluster_name:
        return _cluster_name

    configured = (cfg.cluster_name or "").strip()
    if configured:
        _cluster_name = configured
        return _cluster_name

    stdout, _, rc = await _run_cmd("scontrol", "show", "config")
    if rc == 0:
        for line in stdout.splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "ClusterName" and value.strip():
                _cluster_name = value.strip()
                return _cluster_name

    _cluster_name = _remote_host(cfg.remote) or "local"
    return _cluster_name


async def get_partition_availability(
    config: Config | None = None, *, force: bool = False,
) -> list[str]:
    """Return per-partition node availability strings from `sinfo`.

    Each entry looks like "gpu:10/5/0/15" (allocated/idle/other/total).
    Honors cfg.partition_order for display ordering; down partitions are
    dropped.

    Cached for `_SINFO_TTL`. This feeds the cluster bar, which describes the
    shape of the whole machine -- how many nodes a partition has, and roughly
    how busy it is. That is not a five-second quantity, and asking for it at
    five-second granularity costs a command (and, remotely, a round trip) to
    redraw the same string. `force=True` on the manual refresh, where the user
    has asked for everything to be current.
    """
    global _partition_cache
    cfg = config or _config
    key = tuple(cfg.partition_order or ())
    now = datetime.now()
    cached = _partition_cache
    if (
        not force
        and cached is not None
        and cached[0] == key
        and now - cached[1] < _SINFO_TTL
    ):
        return cached[2]

    stdout, _, _ = await _run_cmd(
        "sinfo", "--noheader", "--summarize", f"--format={_SINFO_FORMAT}",
    )
    if not stdout.strip():
        # Nothing to cache: an empty answer here usually means sinfo failed,
        # and holding onto that for a minute would hide the recovery.
        return []
    parts = [p for p in parse_sinfo(stdout) if p.avail == "up"]
    result = [f"{p.name}:{p.nodes_aiot}" for p in order_partitions(parts, cfg)]
    _partition_cache = (key, now, result)
    return result


# ---------------------------------------------------------------------------
# Nodes of a partition (sinfo -N)
# ---------------------------------------------------------------------------

# The long form is the only one that can report GresUsed — how many GPUs of a
# node are actually taken, which is the question worth asking on a GPU cluster.
# ":|" makes sinfo pad each field with "|" instead of spaces.
_SINFO_NODE_FIELDS = (
    "NodeHost:|,StateLong:|,CPUsState:|,Memory:|,FreeMem:|,"
    "CPUsLoad:|,Gres:|,GresUsed:|,Reason:|"
)
# Fallback for Slurm versions without those -O field names; no GresUsed.
_SINFO_NODE_FORMAT = "%N|%T|%C|%m|%e|%O|%G||%E"


def _as_opt_int(value: str) -> int | None:
    """Like _as_int, but None for a field the node never reported.

    sinfo prints "N/A" for FreeMem on a node that is down or unreachable.
    Reading that as 0 would make the node look completely full.
    """
    text = (value or "").strip()
    if not text or text in ("N/A", "(null)", "none", "Unknown"):
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _as_float(value: str) -> float:
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return 0.0


def parse_sinfo_nodes(stdout: str) -> list[NodeInfo]:
    """Parse per-node sinfo output into NodeInfo, one entry per node."""
    nodes: dict[str, NodeInfo] = {}
    for line in stdout.strip().splitlines():
        raw = line.split("|")
        fields = [f.strip() for f in raw]
        if len(fields) < 3 or not fields[0]:
            continue
        name = fields[0]
        if name in nodes:  # a node listed in several partitions
            continue

        def field(index: int) -> str:
            return fields[index] if len(fields) > index else ""

        c_a, c_i, c_o, c_t = _aiot(field(2))
        # Reason is free text an admin typed, and it is the last field, so a
        # "|" in it splits only itself — rejoin the tail rather than truncate.
        # The sinfo format ends with a "|", so the row has an empty last piece;
        # drop those before rejoining or every reason gains a trailing pipe.
        tail = raw[8:]
        while tail and not tail[-1].strip():
            tail.pop()
        reason = "|".join(tail).strip()
        if reason in ("none", "(null)", "N/A"):
            reason = ""
        gres = field(6)
        if gres in ("(null)", "N/A"):
            gres = ""
        gres_used = field(7)
        if gres_used in ("(null)", "N/A"):
            gres_used = ""
        load_raw = field(5)
        nodes[name] = NodeInfo(
            name=name,
            state=field(1),
            cpus_alloc=c_a, cpus_idle=c_i, cpus_other=c_o, cpus_total=c_t,
            memory_mb=_as_int(field(3)),
            free_mem_mb=_as_opt_int(field(4)),
            cpu_load=_as_float(load_raw),
            gres=gres,
            gres_used=gres_used,
            reason=reason,
        )
    return list(nodes.values())


async def get_partition_nodes(
    partition: str, config: Config | None = None
) -> list[NodeInfo]:
    """Fetch every node of `partition` with its state, load, memory and GPUs."""
    if not partition:
        return []
    stdout, _, rc = await _run_cmd(
        "sinfo", "-N", "-p", partition, "--noheader", "-O", _SINFO_NODE_FIELDS,
    )
    if rc != 0 or not stdout.strip():
        # Older Slurm: retry with the short format (loses GresUsed).
        stdout, _, rc = await _run_cmd(
            "sinfo", "-N", "-p", partition, "--noheader",
            f"--format={_SINFO_NODE_FORMAT}",
        )
        if rc != 0 or not stdout.strip():
            return []
    nodes = parse_sinfo_nodes(stdout)
    nodes.sort(key=lambda n: n.name)
    return nodes


async def get_node_jobs(node: str, config: Config | None = None) -> list[PartitionJob]:
    """All users' jobs currently running on one node."""
    if not node:
        return []
    stdout, _, rc = await _run_cmd(
        "squeue", "-w", node, "--noheader",
        f"--format={_PARTITION_JOB_FORMAT}", "--states=RUNNING",
    )
    if rc != 0 or not stdout.strip():
        return []
    jobs = parse_partition_jobs(stdout)
    jobs.sort(key=lambda j: job_sort_key(j.job_id), reverse=True)
    return jobs


# ---------------------------------------------------------------------------
# Jobs on a partition, across all users (squeue without -u)
# ---------------------------------------------------------------------------

# jobid|user|name|state|elapsed|time limit|nodes|cpus|tres-per-node|nodelist(reason)
_PARTITION_JOB_FORMAT = "%i|%u|%j|%T|%M|%l|%D|%C|%b|%R"


def parse_partition_jobs(stdout: str) -> list[PartitionJob]:
    """Parse the partition squeue output into PartitionJob rows."""
    jobs: list[PartitionJob] = []
    for line in stdout.strip().splitlines():
        row = _split_row(line, 10, free_text=2)     # %j, the job name
        if row is None:
            continue
        fields = [f.strip() for f in row]
        gres = fields[8]
        if gres in ("N/A", "(null)"):
            gres = ""
        jobs.append(PartitionJob(
            job_id=fields[0],
            user=fields[1],
            name=fields[2],
            state=fields[3],
            elapsed=fields[4],
            time_limit=fields[5],
            nodes=fields[6],
            cpus=fields[7],
            gres=gres,
            nodelist=fields[9],
        ))
    return jobs


async def get_partition_jobs(
    partition: str,
    config: Config | None = None,
    states: str = "RUNNING,PENDING",
) -> list[PartitionJob]:
    """Fetch all users' jobs on `partition`, running first, newest first."""
    if not partition:
        return []
    stdout, _, rc = await _run_cmd(
        "squeue", "-p", partition,
        "--noheader",
        f"--format={_PARTITION_JOB_FORMAT}",
        f"--states={states}",
    )
    if rc != 0 or not stdout.strip():
        return []
    jobs = parse_partition_jobs(stdout)
    # Running first, then newest first — array tasks ordered like everywhere else.
    jobs.sort(key=lambda j: (j.state != "RUNNING", tuple(-k for k in job_sort_key(j.job_id))))
    return jobs


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
    # Count array *tasks*, not squeue rows: a single pending "123_[3-11]" row
    # stands for nine jobs, and the tables now say so too.
    running = sum(array_task_count(j.job_id) for j in running_jobs if j.state == "RUNNING")
    pending = sum(array_task_count(j.job_id) for j in running_jobs if j.state == "PENDING")

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


# ---------------------------------------------------------------------------
# Structured node sampling — the meter/graph resource monitor
# ---------------------------------------------------------------------------
#
# One round trip per tab refresh, exactly as the text mode costs: the script
# below does all the reading itself, including the two /proc/stat snapshots that
# a utilisation figure needs. Diffing across refresh ticks instead would avoid
# the sleep, but would report a five-second average and lose the series every
# time the user switched tabs.

_SAMPLE_SLEEP = 0.5  # seconds between the two /proc/stat snapshots

# Sections are marked rather than positional, so a node missing one of these
# files (no cgroup, no loadavg) drops that section instead of shifting the rest.
_NODE_SAMPLE_BODY = """
echo '##stat1'; grep '^cpu' /proc/stat
echo '##affinity'; grep -i '^Cpus_allowed_list' /proc/self/status
echo '##load'; cat /proc/loadavg
echo '##meminfo'; grep -E '^(MemTotal|MemAvailable|MemFree):' /proc/meminfo
echo '##cgroup'
cg=$(awk -F: '$1 == 0 {{print $3}}' /proc/self/cgroup 2>/dev/null)
p="/sys/fs/cgroup$cg"
while [ -n "$cg" ] && [ "$p" != "/sys/fs/cgroup" ] && [ "$p" != "/" ]; do
  if [ -r "$p/memory.max" ] && [ -r "$p/memory.current" ]; then
    lim=$(cat "$p/memory.max")
    if [ "$lim" != "max" ]; then
      echo "used $(cat "$p/memory.current")"; echo "limit $lim"; break
    fi
  fi
  p=$(dirname "$p")
done
cg1=$(awk -F: '$2 ~ /(^|,)memory(,|$)/ {{print $3}}' /proc/self/cgroup 2>/dev/null | head -1)
m1="/sys/fs/cgroup/memory$cg1"
while [ -n "$cg1" ] && [ "$m1" != "/sys/fs/cgroup/memory" ] && [ "$m1" != "/" ]; do
  if [ -r "$m1/memory.limit_in_bytes" ] && [ -r "$m1/memory.usage_in_bytes" ]; then
    echo "used $(cat "$m1/memory.usage_in_bytes")"
    echo "limit $(cat "$m1/memory.limit_in_bytes")"; break
  fi
  m1=$(dirname "$m1")
done
""".strip()

# Taking the second snapshot on the node is what makes utilisation
# instantaneous rather than an average over the refresh interval -- but it also
# makes every sample cost half a second of wall clock, which is most of what a
# refresh of the cpu tab feels like.
#
# It is only needed when there is nothing to subtract from. Once a snapshot has
# been kept from the previous sample, the delta can be taken against that, and
# the script returns as fast as srun can start it.
_NODE_SAMPLE_SCRIPT = f"{_NODE_SAMPLE_BODY}\nsleep {_SAMPLE_SLEEP}\necho '##stat2'; grep '^cpu' /proc/stat"

# How stale a kept snapshot may be and still be worth subtracting from.
#
# The delta then covers the gap since the last sample, the way htop reports the
# gap since its last draw. Past a minute that stops being "now" in any useful
# sense -- a job that finished its epoch two minutes ago would still be shown
# busy -- so beyond this the sample pays for its own second snapshot again.
_SAMPLE_MAX_AGE = timedelta(seconds=60)

# (node, job_id) -> (per-cpu jiffy counters, when they were read)
_stat_cache: dict[tuple[str, str], tuple[dict[int, tuple[float, float]], datetime]] = {}

# nounits keeps the CSV free of "MiB"/"W" suffixes; unsupported fields still
# come back as "[N/A]" or "[Not Supported]", which the parser drops to None.
_GPU_QUERY_FIELDS = (
    "index,name,utilization.gpu,memory.used,memory.total,"
    "temperature.gpu,power.draw,power.limit"
)
_GPU_SAMPLE_CMD = (
    f"nvidia-smi --query-gpu={_GPU_QUERY_FIELDS} --format=csv,noheader,nounits"
)

# A v1 cgroup with no limit reports a number near 2**63 rather than "max".
_UNLIMITED = 1 << 62


def _split_sections(text: str) -> dict[str, list[str]]:
    """Split ``##name``-marked output into ``{name: [lines]}``."""
    sections: dict[str, list[str]] = {}
    current = ""
    for line in text.splitlines():
        if line.startswith("##"):
            current = line[2:].strip()
            sections.setdefault(current, [])
        elif current:
            sections[current].append(line)
    return sections


def _parse_stat(lines: list[str]) -> dict[int, tuple[float, float]]:
    """``cpu<N>`` lines from /proc/stat as ``{cpu: (busy, total)}`` jiffies.

    The aggregate ``cpu`` line is skipped — it is the sum of the others, and the
    meters are per core.
    """
    out: dict[int, tuple[float, float]] = {}
    for line in lines:
        fields = line.split()
        if len(fields) < 5 or not fields[0].startswith("cpu"):
            continue
        index = fields[0][3:]
        if not index.isdigit():
            continue
        try:
            values = [float(v) for v in fields[1:]]
        except ValueError:
            continue
        # user nice system idle iowait irq softirq steal guest guest_nice.
        # idle+iowait is the idle share; guest time is already counted in user,
        # so summing every field would double it.
        total = sum(values[:8]) if len(values) >= 8 else sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0.0)
        out[int(index)] = (total - idle, total)
    return out


def parse_cpu_list(spec: str) -> list[int]:
    """``0-3,8,12-13`` -> ``[0, 1, 2, 3, 8, 12, 13]``."""
    cpus: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        lo, _, hi = part.partition("-")
        if lo.isdigit() and hi.isdigit():
            cpus.extend(range(int(lo), int(hi) + 1))
        elif lo.isdigit():
            cpus.append(int(lo))
    return cpus


def parse_node_sample(
    text: str,
    node: str = "",
    scope: str = "node",
    previous: dict[int, tuple[float, float]] | None = None,
) -> NodeSample:
    """Turn the sampling script's output into a :class:`NodeSample`.

    The script emits either two ``/proc/stat`` snapshots half a second apart, or
    a single one -- in which case `previous` is the snapshot the last sample
    ended at, and the delta spans the gap between the two samples instead.
    """
    sections = _split_sections(text)
    sample = NodeSample(node=node, scope=scope)

    stat1 = _parse_stat(sections.get("stat1", []))
    stat2 = _parse_stat(sections.get("stat2", []))
    if stat2:
        first, second = stat1, stat2
    else:
        first, second = previous or {}, stat1
    # Kept so the next sample can subtract from it rather than sleeping.
    sample.counters = second

    allowed: list[int] = []
    for line in sections.get("affinity", []):
        _, _, spec = line.partition(":")
        allowed = parse_cpu_list(spec)
    # The affinity mask is what makes this the *job's* cores rather than the
    # node's: inside the cgroup it is exactly the allocation. Without it (or on
    # the SSH fallback, where it covers the machine) every core is shown.
    cpus = [c for c in allowed if c in second] or sorted(second)
    if not stat2:
        # Only cores the kept snapshot also has can be differenced. A core it
        # is missing would otherwise difference against zero and report the
        # machine's lifetime average as if it were the current load.
        cpus = [c for c in cpus if c in first]

    for cpu in cpus:
        busy_now, total_now = second[cpu]
        busy_before, total_before = first.get(cpu, (0.0, 0.0))
        d_total = total_now - total_before
        d_busy = busy_now - busy_before
        # A counter that did not move (or went backwards, after a CPU hotplug)
        # says nothing about utilisation — report it as idle, not as 100%.
        ratio = d_busy / d_total if d_total > 0 else 0.0
        sample.cores.append(CoreSample(cpu=cpu, busy=min(1.0, max(0.0, ratio))))

    for line in sections.get("load", []):
        fields = line.split()
        if len(fields) >= 3:
            try:
                sample.load = (float(fields[0]), float(fields[1]), float(fields[2]))
            except ValueError:
                pass

    meminfo: dict[str, float] = {}
    for line in sections.get("meminfo", []):
        key, _, value = line.partition(":")
        parts = value.split()
        if parts and parts[0].isdigit():
            meminfo[key.strip()] = float(parts[0]) * 1024  # /proc/meminfo is kB

    cgroup: dict[str, float] = {}
    for line in sections.get("cgroup", []):
        key, _, value = line.partition(" ")
        value = value.strip()
        if key in ("used", "limit") and value.isdigit() and key not in cgroup:
            cgroup[key] = float(value)

    limit = cgroup.get("limit", 0)
    # An unlimited cgroup (a step with no memory limit of its own) says nothing
    # about the job's headroom, so the node's own numbers are the better answer.
    if "used" in cgroup and 0 < limit < _UNLIMITED:
        sample.mem_used, sample.mem_total = cgroup["used"], limit
        sample.mem_scope = "job"
    elif "MemTotal" in meminfo:
        total = meminfo["MemTotal"]
        available = meminfo.get("MemAvailable", meminfo.get("MemFree"))
        sample.mem_total = total
        sample.mem_used = total - available if available is not None else None
        sample.mem_scope = "node"

    return sample


def _gpu_float(raw: str) -> float | None:
    """A CSV cell as a number, or None for nvidia-smi's [N/A]/[Not Supported]."""
    raw = raw.strip()
    if not raw or raw.startswith("["):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_gpu_sample(text: str, node: str = "", scope: str = "node") -> GpuReading:
    """Parse ``nvidia-smi --query-gpu`` CSV into a :class:`GpuReading`."""
    reading = GpuReading(node=node, scope=scope)
    for line in text.splitlines():
        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 5 or not fields[0].isdigit():
            continue
        util = _gpu_float(fields[2])
        mem_used = _gpu_float(fields[3])
        mem_total = _gpu_float(fields[4])
        reading.gpus.append(GpuSample(
            index=int(fields[0]),
            name=fields[1],
            util=util / 100 if util is not None else None,
            # nounits reports memory in MiB.
            mem_used=mem_used * 1024 ** 2 if mem_used is not None else None,
            mem_total=mem_total * 1024 ** 2 if mem_total is not None else None,
            temperature=_gpu_float(fields[5]) if len(fields) > 5 else None,
            power=_gpu_float(fields[6]) if len(fields) > 6 else None,
            power_limit=_gpu_float(fields[7]) if len(fields) > 7 else None,
        ))
    return reading


async def get_node_sample(node: str, job_id: str = "") -> NodeSample:
    """Sample per-core utilisation, memory and load, scoped to the job.

    Preferred path is `srun --overlap`, which lands inside the job's cgroup and
    so reports the allocated cores and the job's memory limit. Falls back to SSH,
    which sees the whole machine — the same trade-off the gpu tab already makes.
    """
    if not node or node in ("N/A", "None", "(null)"):
        return NodeSample(error="[dim]No node assigned[/]")

    first_node = _first_node(node)
    key = (first_node, job_id)
    kept = _stat_cache.get(key)
    previous = (
        kept[0]
        if kept is not None and datetime.now() - kept[1] <= _SAMPLE_MAX_AGE
        else None
    )
    sample = await _sample_node(first_node, job_id, previous)

    if previous is not None and not sample.cores and not sample.error:
        # The kept snapshot had no core in common with this one -- the job moved
        # node, or the allocation changed under us. Pay for the slow path once
        # rather than showing an empty meter.
        _stat_cache.pop(key, None)
        sample = await _sample_node(first_node, job_id, None)

    if sample.counters:
        _stat_cache[key] = (sample.counters, datetime.now())
    return sample


async def _sample_node(
    node: str, job_id: str, previous: dict[int, tuple[float, float]] | None,
) -> NodeSample:
    """One sampling round trip. Sleeps on the node only without a `previous`."""
    script = _NODE_SAMPLE_BODY if previous is not None else _NODE_SAMPLE_SCRIPT
    done = "##stat1" if previous is not None else "##stat2"

    if job_id:
        stdout, _, rc = await _run_cmd(
            "srun", "--overlap", f"--jobid={job_id}", "bash", "-c", script,
        )
        if rc == 0 and done in stdout:
            return parse_node_sample(stdout, node, scope="job", previous=previous)

    stdout, rc = await _ssh_cmd(node, script)
    if rc != 0 or done not in stdout:
        return NodeSample(
            node=node,
            error=f"[dim]Could not sample {node} (srun and SSH both failed)[/]",
        )
    return parse_node_sample(stdout, node, scope="node", previous=previous)


async def get_gpu_sample(node: str, job_id: str = "") -> GpuReading:
    """Per-GPU utilisation, memory, temperature and power for a job's GPUs."""
    if not node or node in ("N/A", "None", "(null)"):
        return GpuReading(error="[dim]No node assigned[/]")

    first_node = _first_node(node)
    if job_id:
        stdout, _, rc = await _run_cmd(
            "srun", "--overlap", f"--jobid={job_id}",
            "bash", "-c", _GPU_SAMPLE_CMD,
        )
        if rc == 0 and stdout.strip():
            reading = parse_gpu_sample(stdout, first_node, scope="job")
            if reading.gpus:
                return reading

    stdout, rc = await _ssh_cmd(first_node, _GPU_SAMPLE_CMD)
    if rc != 0 or not stdout.strip():
        return GpuReading(
            node=first_node,
            error=f"[dim]Could not reach {first_node}, or nvidia-smi is not available[/]",
        )
    reading = parse_gpu_sample(stdout, first_node, scope="node")
    if not reading.gpus:
        reading.error = f"[dim]No GPUs reported by nvidia-smi on {first_node}[/]"
    return reading


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
