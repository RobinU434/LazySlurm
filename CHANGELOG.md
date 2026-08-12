# Changelog

All notable changes to LazySlurm are documented here. The distribution is
published on PyPI as [`lazyslurm-py`](https://pypi.org/project/lazyslurm-py/);
the command, the import package and the config directory are all `lazyslurm`.

## 0.2.0 — 2026-08-12

The project is now `lazyslurm` throughout, and this release adds job editing, a
partition monitor, and a rebuilt remote mode that works on clusters with
two-factor authentication.

### Added

- **Edit pending jobs (`u`)** — a compact editor for runtime, partition, nodes,
  CPUs and memory, applied with `scontrol update`. Fields are prefilled from the
  job; blank means "leave unchanged", so only what you touched is sent. Works on
  a `Ctrl+V` multi-selection to retune many jobs at once. Memory accepts `40G`,
  `4000M` or plain MB. Running jobs are refused — Slurm fixes the allocation once
  a job starts.
- **Partition monitor (`p`)** — a full-screen view of every partition: node and
  CPU allocated/idle/other/total counters, a load bar, running and pending job
  counts, time limit and GRES. Below it, the jobs on the highlighted partition
  from **all users** (the main tables only ever show yours), with your own rows
  marked and pending jobs showing Slurm's reason instead of a node list.
- **Pager for logs (`l`)** — opens the active log tab in `less` (configurable via
  `pager`). It seeks instead of loading, so a multi-gigabyte log opens instantly;
  `/` searches and `F` follows a running job. In remote mode the pager runs on the
  cluster, so nothing is copied down.

### Changed

- **Remote mode now uses one persistent SSH session.** Instead of spawning
  `ssh host <cmd>` per Slurm call, LazySlurm opens a single master connection at
  startup and keeps a shell channel over it for the whole run. Every command is
  written into that channel — no new connection, no new process, no
  re-authentication.
- **Two-factor authentication is supported.** The master runs on a pty, so the
  cluster's own prompt (`Verification code:`, `Passcode or option (1-3):`, ...)
  is shown in a modal with masked input, once, at startup. Everything else that
  shells out — the compute-node hop, `o`, and the editor's `scp` — reuses that
  authenticated connection, so you are never asked twice. `r` retries a failed or
  cancelled login.
- Renamed from SlurmTop: the command is `lazyslurm`, config lives in
  `~/.config/lazyslurm/`, and the SSH control directory is `~/.ssh/cm-lazyslurm/`.
  **Migrating from 0.1.0:** `mv ~/.config/slurmtop ~/.config/lazyslurm` to keep
  your settings and cached job paths.

### Documentation

- The README now shows real screenshots of the TUI — main view, partition monitor, job
  editor, stats, help and the two-factor prompt. They are generated headlessly from
  synthetic data by `scripts/make_screenshots.py`, so no real account, job or path is
  ever pictured.

### Fixed

- **Log tails no longer read the whole file.** `read_log_file` kept the last 500
  lines by iterating the entire log, so selecting a job with a large log stalled
  the UI — 74 s for a 168 MB file, twice per selection. It now seeks backwards
  from the end: 0.03 s for the same file.
- **Array tasks sort correctly.** `2736118_11` is not an integer, so every array
  task collapsed to the same sort key and scattered through the table. Arrays now
  stay together in submission order with their tasks ascending (`_0, _1, _2, _11`,
  not `_0, _1, _10, _11`), in both job tables and the partition monitor.
- **The cluster bar counts every node.** `sinfo --summarize` emits one row per
  node *configuration*, and only the last row per partition was kept — a
  mixed-hardware partition reported a fraction of its nodes. Rows are now summed.
- The pending-job editor rendered its focused field blank, because Textual's own
  `Input:focus` rule re-added a border that collapsed the row to zero height.

## 0.1.0

Initial release: job tables from `squeue`/`sacct`, stdout/stderr/CPU/GPU/stats
detail tabs, metadata panel, cancel, force-cancel and resubmit, multi-select,
bookmarks, search, sbatch-script viewing with a local archive, log-path caching,
desktop notifications, colors and column-width settings, and remote mode over
SSH.
