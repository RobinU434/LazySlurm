"""Entry point for `python -m slurmtop` and the `slurmtop` CLI command."""

from __future__ import annotations

import argparse
import os
import sys

from slurmtop.models import Config


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


def main() -> None:
    from slurmtop import config as persistent_config

    # Load saved config for defaults
    saved = persistent_config.load()

    parser = argparse.ArgumentParser(
        prog="slurmtop",
        description="A TUI for monitoring Slurm HPC jobs.",
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
    max_name_width = int(saved.get("max_name_width", 16))
    max_partition_width = int(saved.get("max_partition_width", 16))
    abbreviate_states = bool(saved.get("abbreviate_states", False))
    raw_cache_age = saved.get("cache_max_age_days", 30)
    cache_max_age_days = None if raw_cache_age is None else int(raw_cache_age)
    script_cache_dir = os.path.expanduser(str(saved.get("script_cache_dir", "")))

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
        max_name_width=max_name_width,
        max_partition_width=max_partition_width,
        abbreviate_states=abbreviate_states,
        cache_max_age_days=cache_max_age_days,
        script_cache_dir=script_cache_dir,
    )

    from slurmtop.app import SlurmTopApp
    app = SlurmTopApp(config=config, config_overrides=overrides)
    app.run()


if __name__ == "__main__":
    main()
