# Changelog

All notable changes to LazySlurm are documented here. The distribution is
published on PyPI as [`lazyslurm-py`](https://pypi.org/project/lazyslurm-py/);
the command, the import package and the config directory are all `lazyslurm`.

## Unreleased

### Added

- **Job arrays collapse into one row** ([#7](https://github.com/RobinU434/LazySlurm/issues/7)).
  A 40-task array no longer fills the table: its tasks fold into a single
  `▸ 123_[0-39] ×40` row carrying the id range, the shared name and a state tally.
  `Enter` expands and collapses it, and the expansion survives refreshes and filter
  changes. `c` cancels the whole array with one `scancel`, `u` edits every pending task
  in it, `m` bookmarks the group. Switch off with `collapse_arrays = false`.
- **Resubmit with modified resources (`Shift+S`)** ([#11](https://github.com/RobinU434/LazySlurm/issues/11)).
  The loop after a failure is "run it again, but bigger", and `u` cannot help — Slurm
  fixes an allocation once the job starts. `Shift+S` opens the property editor for a
  *terminated* job, prefilled from what it had, and submits the changed fields as
  sbatch flags. After a `TIMEOUT` the runtime is prefilled with double the old limit,
  after `OUT_OF_MEMORY` the memory with double the old request. An override replaces
  the same option in the original submit line rather than being appended after it, so
  the `sbatch` line written to the Command Log is what actually ran.
- **Configurable compute-node access** ([#21](https://github.com/RobinU434/LazySlurm/issues/21)).
  `interactive_shell = "ssh" | "srun"` chooses how `o` opens a shell on a job's node:
  `ssh` lands on the machine (no `CUDA_VISIBLE_DEVICES`, every node GPU visible, no job
  step), `srun` lands inside the allocation (correct GPUs and limits, but the step shows
  up in `sacct` and lowers the job's reported efficiency). Default `ssh`, unchanged from
  before; `Shift+O` uses the other one for a single shell. Needed on clusters where
  `pam_slurm_adm` refuses SSH without an allocation. The README explains the trade-off.
- **Unknown config keys are reported** ([#27](https://github.com/RobinU434/LazySlurm/issues/27)).
  A misspelled setting used to be silently inert. It is now listed in the Command Log at
  startup and after an in-app config reload. The file is never rejected over a typo.
- A `py.typed` marker, so the package's annotations are visible to type checkers
  ([#34](https://github.com/RobinU434/LazySlurm/issues/34)).
- A one-line status bar above the key bar: refusals like "Only pending jobs can be
  edited" no longer scroll away in the Command Log
  ([#34](https://github.com/RobinU434/LazySlurm/issues/34)).

### Changed

- The cluster bar counts array *tasks* rather than squeue rows, so a pending
  `123_[3-11]` row contributes nine pending jobs — matching what the table now shows.
- **Saving config preserves the file** ([#25](https://github.com/RobinU434/LazySlurm/issues/25)).
  `--partition-order` and partition colours used to rewrite `config.toml` from a dict,
  deleting the template's ~38 lines of documentation, the original ordering and any key
  the loader did not recognise. The file is now edited in place via `tomlkit` (a new
  dependency).
- **`cache_max_age_days = 0` means never** ([#26](https://github.com/RobinU434/LazySlurm/issues/26)).
  The template documented `null = never`, which TOML cannot express; `0`, the value most
  people would try instead, was taken literally and deleted the entire script archive on
  the next launch. `0` and `false` now both disable pruning.
- The stats tab's log cache is no longer rewritten in full on every job selection — the
  write is skipped when nothing about the job changed
  ([#33](https://github.com/RobinU434/LazySlurm/issues/33)).
- Log-path guessing no longer probes the same candidate twice, saving two SSH round
  trips per selection in remote mode
  ([#32](https://github.com/RobinU434/LazySlurm/issues/32)).
- Failures that used to be swallowed by bare `except Exception` are now narrowed to what
  they actually defend against, and a batch script that could not be archived says so in
  the Command Log instead of disappearing
  ([#31](https://github.com/RobinU434/LazySlurm/issues/31)).
- **`Enter` in the filter bar accepts the filter** ([#38](https://github.com/RobinU434/LazySlurm/issues/38)).
  It closes the bar, keeps the query, and puts the cursor back on the matching rows.
  `Escape` still abandons the filter. Previously every way out of the bar cleared the
  query, so the rows a filter found could not be reached from the keyboard.
- **`Tab` cycles the three panels** — job tables → Job Details → Job Metadata → job
  tables — instead of walking every focusable widget in the layout. `Shift+Tab` goes the
  other way. `action_focus_next_right` never actually moved focus before; the panel it
  claimed to focus was reached only by Textual's default widget cycling
  ([#38](https://github.com/RobinU434/LazySlurm/issues/38)).

### Fixed

- **The job list on the partition and node screens jumped to the top every refresh**
  ([#43](https://github.com/RobinU434/LazySlurm/issues/43)). Those panes are rebuilt on
  the refresh timer, and rebuilding resets the cursor — so on a busy partition the list
  could not be browsed at all: every scroll was undone within seconds. The cursor is now
  tracked by row key, so it also *follows* its job when rows above it disappear. The
  account usage table had the same bug, visible only on `r`/`w`. All four tables now
  share one `KeyedTable` base instead of a per-table copy of the logic.
- **Blank fields in the detail panels** ([#22](https://github.com/RobinU434/LazySlurm/issues/22)).
  `sacct` emits empty columns rather than omitting them, so eleven accessors returned
  `""` instead of falling back to the other spelling of the field. Cancelled and failed
  jobs showed a blank where the value was available all along.
- **The stats tab's "CPU" sparkline plotted memory** ([#23](https://github.com/RobinU434/LazySlurm/issues/23)).
  Both series were memory readings, which looked like two measurements agreeing. It now
  plots real CPU — the `TotalCPU`/`Elapsed` delta between samples, normalised by
  `AllocCPUS` — on a fixed 0-100% scale.
- **A partition's GRES could go missing** ([#30](https://github.com/RobinU434/LazySlurm/issues/30)).
  Aggregating `sinfo` rows used a substring test, so `gpu:a100:8` was dropped whenever
  `gpu:a100:80` was already listed.
- Sparkline history no longer grows for the lifetime of the session: entries are dropped
  when the job stops running ([#28](https://github.com/RobinU434/LazySlurm/issues/28)).
- Importing `lazyslurm.slurm` no longer creates a directory in `~/.ssh` as a side effect
  — `--help`, a unit test or a docs build left one behind
  ([#29](https://github.com/RobinU434/LazySlurm/issues/29)).
- `slurm.py` defined `_as_int` twice, so the definition sitting next to the stats code
  was not the one that ran ([#24](https://github.com/RobinU434/LazySlurm/issues/24)).
- **Job names with CJK, emoji or combining marks broke column alignment**
  ([#37](https://github.com/RobinU434/LazySlurm/issues/37)). Truncation counted code
  points where a terminal lays out columns, so `実験-sweep` overflowed its column and
  pushed everything to its right out of line. Width is now measured in terminal columns,
  and a double-width character is never split down the middle.
- **Reading a remote log failed on a path containing an apostrophe**
  ([#39](https://github.com/RobinU434/LazySlurm/issues/39)). The "file not found" message
  was built on the cluster, with the path interpolated into a single-quoted `echo`, so
  `/work/bens'runs/x.out` closed the quote early and the rest was parsed as shell words.
  The message is a local UI string now, and the path reaches `tail` as one quoted word.
- **Opening a remote log in the editor failed on a path containing a space**
  ([#40](https://github.com/RobinU434/LazySlurm/issues/40)). A remote `scp` path passes
  through two shells and was quoted for only one, so `/work/my runs/x.out` was re-split
  on the far side. The failure message now also names the path.

## 0.2.1 — 2026-08-12

### Added

- **Node view** — press `Enter` on a partition in the monitor (`p`) to see its individual
  nodes: state, CPU allocation and actual load average, memory in use, GPUs taken over
  GPUs configured, and Slurm's drain reason where there is one. The panel below lists all
  users' jobs running on the highlighted node, so you can see who you would be sharing it
  with. `Up`/`Down` moves between nodes, `Tab` switches panels, `r` refreshes, `Escape`
  returns.

  GPU occupancy comes from `sinfo -O GresUsed`; Slurm builds without that output field
  fall back to a shorter query automatically and lose only the GPU column.

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
