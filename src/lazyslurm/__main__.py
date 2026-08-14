"""Entry point for `python -m lazyslurm` and the `lazyslurm` CLI command."""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from lazyslurm.models import Config
from lazyslurm.version import version_string


# Map from config file key → (CLI dest, type converter)
_CONFIG_KEYS = {
    "refresh": ("refresh", float),
    "days": ("days", int),
    "user": ("user", str),
    "partition": ("partition", str),
    "no_gpu": ("no_gpu", bool),
    "no_live": ("no_live", bool),
    "remote": ("remote", str),
    "partition_order": ("partition_order", list),
}

# Settings that exist only in the config file — no CLI equivalent. Together with
# _CONFIG_KEYS this is the whole vocabulary of config.toml, and the one place to
# add a key so that both the reader and the unknown-key check see it.
_FILE_ONLY_KEYS = frozenset({
    "partition_colors",
    "editor",
    "pager",
    "max_name_width",
    "max_partition_width",
    "abbreviate_states",
    "collapse_arrays",
    "cache_max_age_days",
    "script_cache_dir",
    "interactive_shell",
    "resource_monitor",
    "node_expand",
    "gpu_column",
    "config_version",
})

KNOWN_CONFIG_KEYS = frozenset(_CONFIG_KEYS) | _FILE_ONLY_KEYS


def unknown_config_keys(saved: dict) -> list[str]:
    """Warnings for config-file keys nothing reads — a typo, or a rename.

    Such a key is silently inert today, which is the hardest kind of config
    problem to diagnose because the file looks right. Reported, never rejected:
    one typo must not cost the user every other setting they configured.
    Nested tables are checked by table name only.

    A key that used to be real is reported by `deprecated_config_keys` instead,
    which can say what replaced it — much more useful than "unknown".
    """
    from lazyslurm.config import DEPRECATED

    return [
        f"ignoring unknown setting: {key}"
        for key in sorted(saved)
        if key not in KNOWN_CONFIG_KEYS and key not in DEPRECATED
    ]


def deprecated_config_keys(saved: dict) -> list[str]:
    """Warnings for settings that were real once, naming their replacement."""
    from lazyslurm.config import DEPRECATED

    notes = []
    for key in sorted(saved):
        if key not in DEPRECATED:
            continue
        replacement = DEPRECATED[key]
        notes.append(
            f"{key} has been renamed to {replacement}" if replacement
            else f"{key} is no longer used and is ignored"
        )
    return notes


# What the main view cannot work without. Everything else (sinfo, sstat, sprio,
# sreport, sshare) degrades to a message in its own panel.
REQUIRED_COMMANDS = ("squeue", "sacct")


def missing_commands(remote: str = "") -> list[str]:
    """Which required binaries are absent locally. Empty in remote mode.

    In remote mode the Slurm commands run on the cluster, so the only local
    requirement is ssh.
    """
    needed = ("ssh",) if remote else REQUIRED_COMMANDS
    return [name for name in needed if shutil.which(name) is None]


def _no_slurm_message(missing: list[str], remote: str = "") -> str:
    """What to print when the tool cannot possibly work here."""
    names = ", ".join(missing)
    if remote:
        return (
            f"lazyslurm: {names} not found on this machine.\n\n"
            "Remote mode runs the Slurm commands on the cluster over ssh, so "
            "an ssh client has to be installed locally."
        )
    return (
        f"lazyslurm: no Slurm commands found on this machine ({names}).\n\n"
        "LazySlurm reads jobs from the Slurm CLI, so it has to run somewhere "
        "those exist:\n"
        "  - on a cluster login node, or\n"
        "  - on your own machine against a cluster, with --remote:\n\n"
        "      lazyslurm --remote user@login.hpc.edu\n"
    )


def parse_cache_max_age(raw: object) -> int | None:
    """Resolve ``cache_max_age_days`` from config. None means "never prune".

    TOML has no null literal, so "never" has to be expressible some other way:
    ``0`` and ``false`` both mean it. Taking ``0`` literally would prune every
    cached script on the next launch, which is the opposite of what a user
    reaching for "0 = off" intends. Anything unparsable falls back to 30 days.
    """
    if raw is None or raw is False or raw == 0:
        return None
    if raw is True:
        return 30
    try:
        days = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 30
    return days if days > 0 else None


def parse_interactive_shell(raw: object) -> tuple[str, str]:
    """Validate ``interactive_shell``. Returns (value, warning or "").

    An unrecognised value falls back to the default rather than failing, but
    says so — silently ignoring it would leave `o` doing the opposite of what
    the file asks for.
    """
    from lazyslurm.slurm import INTERACTIVE_SHELLS

    value = str(raw).strip().lower()
    if value in INTERACTIVE_SHELLS:
        return value, ""
    options = " | ".join(INTERACTIVE_SHELLS)
    return "ssh", f"interactive_shell: {raw!r} is not one of {options} — using ssh"


def parse_resource_monitor(raw: object) -> tuple[str, str]:
    """Validate ``resource_monitor``. Returns (value, warning or "").

    Same contract as ``parse_interactive_shell``: an unknown value falls back to
    the default and says so, because the cpu/gpu tabs would otherwise look
    unchanged with no hint that the setting was rejected.
    """
    from lazyslurm.models import RESOURCE_MONITOR_MODES

    value = str(raw).strip().lower()
    if value in RESOURCE_MONITOR_MODES:
        return value, ""
    options = " | ".join(RESOURCE_MONITOR_MODES)
    return "graph", f"resource_monitor: {raw!r} is not one of {options} — using graph"


def _parse_mode(raw: object, modes: tuple[str, ...], key: str) -> tuple[str, str]:
    """Validate one of the fixed-vocabulary settings. Returns (value, warning).

    The first mode is the default, and an unknown value falls back to it with a
    warning rather than being rejected -- the panel would otherwise look
    unchanged with no hint that the setting was ignored.
    """
    value = str(raw).strip().lower()
    if value in modes:
        return value, ""
    return modes[0], f"{key}: {raw!r} is not one of {' | '.join(modes)} — using {modes[0]}"


def parse_node_expand(raw: object) -> tuple[str, str]:
    """Validate ``node_expand``: what a node row unfolds into."""
    from lazyslurm.models import NODE_EXPAND_MODES

    return _parse_mode(raw, NODE_EXPAND_MODES, "node_expand")


def parse_gpu_column(raw: object) -> tuple[str, str]:
    """Validate ``gpu_column``: the node table's GPUs column, count or marks."""
    from lazyslurm.models import GPU_COLUMN_MODES

    return _parse_mode(raw, GPU_COLUMN_MODES, "gpu_column")


def main() -> None:
    from lazyslurm import config as persistent_config

    # Bring the file up to the packaged template first, so the values read
    # below are the migrated ones and the notes reach the same warning list.
    migration_notes = persistent_config.migrate()
    saved = persistent_config.load()

    parser = argparse.ArgumentParser(
        prog="lazyslurm",
        description="A TUI for monitoring Slurm HPC jobs.",
    )
    parser.add_argument(
        "-V", "--version",
        action="version",
        version=f"lazyslurm {version_string()}",
        help="Show the version (plus the commit, when running from a checkout)",
    )
    parser.add_argument(
        "-r", "--refresh",
        type=str,
        default=None,
        metavar="SEC",
        help="Auto-refresh interval in seconds (default: 5). Set to 0 or 'off' to disable.",
    )
    parser.add_argument(
        "-d", "--days",
        type=int,
        default=None,
        metavar="N",
        help="How many days back to show terminated jobs (default: 7)",
    )
    parser.add_argument(
        "-u", "--user",
        default=None,
        help="Slurm user to monitor (default: current user)",
    )
    parser.add_argument(
        "-p", "--partition",
        default=None,
        help="Filter jobs by partition",
    )
    parser.add_argument(
        "--no-gpu",
        action="store_true",
        default=None,
        help="Disable live GPU monitoring tab (nvidia-smi)",
    )
    parser.add_argument(
        "--no-live",
        action="store_true",
        default=None,
        help="Disable live CPU/GPU monitoring tabs (no SSH to nodes)",
    )
    parser.add_argument(
        "--partition-order",
        default=None,
        metavar="P1,P2,...",
        help="Comma-separated partition display order for the cluster bar "
             "(e.g. gpu,cpu,fat). Saved to config file for future sessions.",
    )
    parser.add_argument(
        "-H", "--remote",
        default=None,
        metavar="HOST",
        help="SSH target for remote mode, e.g. user@login.hpc.edu. "
             "All Slurm commands are tunneled via SSH.",
    )

    args = parser.parse_args()

    # Parse refresh: support "off" / "0" to disable
    cli_refresh = None
    if args.refresh is not None:
        if args.refresh.lower() in ("off", "none", "null", "0"):
            cli_refresh = 0.0
        else:
            try:
                cli_refresh = float(args.refresh)
            except ValueError:
                parser.error(f"Invalid refresh value: {args.refresh}")

    # If remote is specified and no explicit --user, extract user from user@host
    remote_val = args.remote
    default_user = os.environ.get("USER", os.environ.get("LOGNAME", ""))
    if remote_val and "@" in remote_val and args.user is None:
        default_user = remote_val.split("@")[0]

    # Hard defaults (when neither CLI nor config file provides a value)
    defaults = {
        "refresh": 5.0,
        "days": 7,
        "user": default_user,
        "partition": "",
        "no_gpu": False,
        "no_live": False,
        "remote": "",
        "partition_order": None,
    }

    # Resolve: CLI arg > config file > hard default
    # Track overrides where CLI differs from config file
    overrides: list[str] = []
    resolved: dict[str, object] = {}

    cli_values = {
        "refresh": cli_refresh,
        "days": args.days,
        "user": args.user,
        "partition": args.partition,
        "no_gpu": args.no_gpu if args.no_gpu else None,
        "no_live": args.no_live if args.no_live else None,
        "remote": args.remote,
        "partition_order": args.partition_order,
    }

    for key, hard_default in defaults.items():
        cli_val = cli_values.get(key)
        file_val = saved.get(key)

        if cli_val is not None:
            # CLI was explicitly provided
            # Special handling for partition_order (comes as comma string)
            if key == "partition_order" and isinstance(cli_val, str):
                cli_val = [p.strip() for p in cli_val.split(",") if p.strip()] or None

            if file_val is not None and file_val != cli_val:
                overrides.append(f"{key}: config={file_val} -> cli={cli_val}")

            resolved[key] = cli_val
        elif file_val is not None:
            resolved[key] = file_val
        else:
            resolved[key] = hard_default

    # If partition_order was set via CLI, persist it
    part_order = resolved["partition_order"]
    if args.partition_order is not None and part_order:
        persistent_config.set_partition_order(part_order)

    # Config-file-only settings
    partition_colors = persistent_config.get_partition_colors()
    editor = saved.get("editor", "vim")
    pager = saved.get("pager", "less")
    max_name_width = int(saved.get("max_name_width", 16))
    max_partition_width = int(saved.get("max_partition_width", 16))
    abbreviate_states = bool(saved.get("abbreviate_states", False))
    collapse_arrays = bool(saved.get("collapse_arrays", True))
    cache_max_age_days = parse_cache_max_age(saved.get("cache_max_age_days", 30))
    script_cache_dir = os.path.expanduser(str(saved.get("script_cache_dir", "")))
    interactive_shell, shell_warning = parse_interactive_shell(
        saved.get("interactive_shell", "ssh")
    )
    resource_monitor, monitor_warning = parse_resource_monitor(
        saved.get("resource_monitor", "graph")
    )
    node_expand, expand_warning = parse_node_expand(saved.get("node_expand", "gpu"))
    gpu_column, column_warning = parse_gpu_column(saved.get("gpu_column", "count"))
    warnings = migration_notes + deprecated_config_keys(saved) + unknown_config_keys(saved)
    for warning in (shell_warning, monitor_warning, expand_warning, column_warning):
        if warning:
            warnings.append(warning)

    config = Config(
        refresh=float(resolved["refresh"]),
        days=int(resolved["days"]),
        user=str(resolved["user"]),
        partition=str(resolved["partition"]),
        no_gpu=bool(resolved["no_gpu"]),
        no_live=bool(resolved["no_live"]),
        remote=str(resolved["remote"]),
        partition_order=resolved["partition_order"],
        partition_colors=partition_colors,
        editor=str(editor),
        pager=str(pager),
        max_name_width=max_name_width,
        max_partition_width=max_partition_width,
        abbreviate_states=abbreviate_states,
        collapse_arrays=collapse_arrays,
        cache_max_age_days=cache_max_age_days,
        script_cache_dir=script_cache_dir,
        interactive_shell=interactive_shell,
        resource_monitor=resource_monitor,
        node_expand=node_expand,
        gpu_column=gpu_column,
    )

    # Bail out before the TUI starts rather than crashing on the first poll:
    # every Slurm call would raise FileNotFoundError, and the traceback said
    # nothing about what to do instead.
    absent = missing_commands(str(resolved["remote"]))
    if absent:
        print(_no_slurm_message(absent, str(resolved["remote"])), file=sys.stderr)
        raise SystemExit(1)

    from lazyslurm.app import LazySlurmApp
    app = LazySlurmApp(
        config=config,
        config_overrides=overrides,
        config_warnings=warnings,
    )
    app.run()


if __name__ == "__main__":
    main()
