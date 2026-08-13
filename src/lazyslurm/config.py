"""Persistent configuration file for LazySlurm (~/.config/lazyslurm/config.toml)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import tomlkit

# Use tomllib (3.11+) for reading; tomlkit does the writing, because it is the
# only one of the two that preserves the file's comments and layout.
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "lazyslurm"
CONFIG_FILE = CONFIG_DIR / "config.toml"
LOG_CACHE_FILE = CONFIG_DIR / "log_cache.json"
SCRIPT_CACHE_DIR = CONFIG_DIR / "scripts"


def load() -> dict:
    """Load persistent config. Returns empty dict if file doesn't exist."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        # A broken or unreadable config must not stop the app from starting.
        return {}


def _template_text() -> str:
    """The packaged commented template, or "" if it cannot be read."""
    try:
        from importlib.resources import files
        return files("lazyslurm").joinpath("templ", "config.toml").read_text()
    except (OSError, ModuleNotFoundError):
        return ""  # packaged template unavailable; start from an empty file


def save(data: dict) -> None:
    """Write ``data`` into config.toml, keeping the rest of the file intact.

    The file is edited, not regenerated: ~38 of the template's 41 lines are
    comments documenting every setting, and they are the only in-product
    reference for options like ``partition_colors``. Keys the loader does not
    know about survive for the same reason.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    existing = CONFIG_FILE.read_text() if CONFIG_FILE.exists() else _template_text()
    doc = tomlkit.parse(existing)
    for key, value in data.items():
        doc[key] = value
    CONFIG_FILE.write_text(tomlkit.dumps(doc))


def get_partition_order() -> list[str] | None:
    """Load partition order from config file."""
    data = load()
    order = data.get("partition_order")
    if isinstance(order, list) and order:
        return order
    return None


def set_partition_order(order: list[str]) -> None:
    """Save partition order to config file, preserving other settings."""
    data = load()
    data["partition_order"] = order
    save(data)


def get_partition_colors() -> dict[str, str] | None:
    """Load custom partition→color mapping from config file."""
    data = load()
    colors = data.get("partition_colors")
    if isinstance(colors, dict) and colors:
        return colors
    return None


def set_partition_colors(colors: dict[str, str]) -> None:
    """Save custom partition→color mapping to config file."""
    data = load()
    data["partition_colors"] = colors
    save(data)


# ---------------------------------------------------------------------------
# Log path cache — remembers StdOut/StdErr paths from scontrol
# ---------------------------------------------------------------------------
# Format: {"<job_id>": {"stdout": "...", "stderr": "...", "command": "...", "workdir": "...", "ts": <epoch>}, ...}


def _load_log_cache() -> dict:
    if not LOG_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(LOG_CACHE_FILE.read_text())
    except (OSError, ValueError):
        # Truncated or corrupt cache — it is a cache, so rebuild it.
        return {}


def _save_log_cache(cache: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Write via a temp file + rename so a concurrent reader never sees a
    # half-written file (the TUI and a resubmit can both touch this).
    tmp = LOG_CACHE_FILE.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(cache))
    os.replace(tmp, LOG_CACHE_FILE)


def cache_job_paths(
    job_id: str,
    stdout_path: str | None = None,
    stderr_path: str | None = None,
    command: str | None = None,
    work_dir: str | None = None,
    submit_line: str | None = None,
) -> None:
    """Store paths for a job (called when scontrol provides them).

    ``command`` (scontrol Command=, a bare script path) and ``submit_line``
    (scontrol SubmitLine=, the full "sbatch --array=1-4 job.sh") are stored
    separately so resubmission can prefer the richer one instead of depending
    on which field happened to be written last.
    """
    if not any((stdout_path, stderr_path, command, work_dir, submit_line)):
        return
    cache = _load_log_cache()
    entry = dict(cache.get(job_id, {}))
    if stdout_path:
        entry["stdout"] = stdout_path
    if stderr_path:
        entry["stderr"] = stderr_path
    if command:
        entry["command"] = command
    if submit_line:
        entry["submit_line"] = submit_line
    if work_dir:
        entry["workdir"] = work_dir
    old = cache.get(job_id)
    unchanged = old is not None and all(
        old.get(k) == v for k, v in entry.items() if k != "ts"
    )
    # This runs on every job selection, and a full read-modify-write of a cache
    # holding thousands of entries is not worth doing when nothing changed. The
    # timestamp is still refreshed once a day so a job the user keeps visiting
    # does not age out of the cache under their cursor.
    if unchanged and time.time() - float(old.get("ts", 0) or 0) < 86400:
        return
    entry["ts"] = time.time()
    cache[job_id] = entry
    _save_log_cache(cache)


# Backwards-compatible alias
cache_log_paths = cache_job_paths


def get_cached_log_paths(job_id: str) -> tuple[str | None, str | None]:
    """Retrieve cached stdout/stderr paths for a job."""
    cache = _load_log_cache()
    entry = cache.get(job_id)
    if not entry:
        return None, None
    return entry.get("stdout") or None, entry.get("stderr") or None


def get_cached_command(job_id: str) -> tuple[str | None, str | None]:
    """Retrieve cached command and workdir for a job. Returns (command, workdir).

    Prefers the full submit line over the bare script path — it carries the
    sbatch flags (--array etc.) that resubmission needs.
    """
    cache = _load_log_cache()
    entry = cache.get(job_id)
    if not entry:
        return None, None
    command = entry.get("submit_line") or entry.get("command") or None
    return command, entry.get("workdir") or None


def prune_log_cache(max_age_days: int | None = 30) -> None:
    """Remove cache entries older than max_age_days. None = never prune."""
    if max_age_days is None:
        return
    cache = _load_log_cache()
    cutoff = time.time() - max_age_days * 86400
    pruned = {k: v for k, v in cache.items() if v.get("ts", 0) > cutoff}
    if len(pruned) < len(cache):
        _save_log_cache(pruned)


# ---------------------------------------------------------------------------
# Batch script cache — archives the sbatch script text itself
# ---------------------------------------------------------------------------
# Slurm only keeps a job's batch script until MinJobAge seconds after it ends
# (300s on many clusters), so `scontrol write batch_script` fails for anything
# older. Archiving the text — rather than the path, which the user may edit,
# move, or delete — is what makes an old job's script recoverable at all.
# Layout: <SCRIPT_CACHE_DIR>/<base_job_id>.sh


def set_script_cache_dir(path: str | Path | None) -> None:
    """Point the script cache at a custom directory (from config.toml)."""
    global SCRIPT_CACHE_DIR
    if not path:
        return
    SCRIPT_CACHE_DIR = Path(path)


def base_job_id(job_id: str) -> str:
    """Reduce any Slurm job id to the base id that owns the batch script.

    All tasks of an array share one script, so ``123_11``, ``123_[1-40]``,
    ``123+0`` (heterogeneous) and ``123.batch`` (a step, as sacct reports it)
    all map to ``123``. Returns "" if no digits remain — callers treat that as
    "no valid id", which also keeps arbitrary text out of cache filenames.
    """
    head = job_id.strip()
    for sep in ("_", "+", "."):
        head = head.split(sep, 1)[0]
    return head if head.isdigit() else ""


def script_cache_path(job_id: str) -> Path | None:
    """Path where this job's script is (or would be) cached."""
    base = base_job_id(job_id)
    if not base:
        return None
    return SCRIPT_CACHE_DIR / f"{base}.sh"


def get_cached_script(job_id: str) -> Path | None:
    """Return the cached script path, or None if not cached."""
    path = script_cache_path(job_id)
    if path is None:
        return None
    try:
        # Require non-empty: a truncated write must not open as a blank buffer.
        if path.stat().st_size > 0:
            return path
    except OSError:
        pass
    return None


def cache_script(job_id: str, text: str) -> Path | None:
    """Archive a job's batch script text. Returns the path, or None on failure."""
    path = script_cache_path(job_id)
    if path is None or not text.strip():
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        # Batch scripts routinely hold tokens and private paths, so keep the
        # archive owner-only. No exec bit — this copy is for reading, not running.
        tmp = path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def prune_script_cache(max_age_days: int | None = 30) -> None:
    """Remove archived scripts older than max_age_days. None = never prune."""
    if max_age_days is None:
        return
    cutoff = time.time() - max_age_days * 86400
    try:
        scripts = list(SCRIPT_CACHE_DIR.glob("*.sh"))
    except OSError:
        return
    for path in scripts:
        try:
            # mtime, not a JSON timestamp — the file itself is the record here.
            if path.stat().st_mtime <= cutoff:
                path.unlink()
        except OSError:
            pass
