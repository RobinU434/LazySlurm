# Changelog

All notable changes to LazySlurm are documented here. The distribution is
published on PyPI as [`lazyslurm-py`](https://pypi.org/project/lazyslurm-py/);
the command, the import package and the config directory are all `lazyslurm`.

## Unreleased

### Added

- **Fold a node open into its GPUs, in the partition monitor**
  ([#66](https://github.com/RobinU434/LazySlurm/issues/66)). The node view stopped one
  level too early on a GPU cluster: `7/8` says how full a node is, but not *which* device
  is free, who has the rest, or whether they are being used. `Enter` on a node now unfolds
  one row per GPU, each cell carrying the same kind of thing as the node cell above it —
  identity stays identity, `Load` is already a bar on the same 0–1 scale, `Memory` keeps
  its used/total shape.

  Which devices are taken costs nothing: `sinfo` reports the allocated indices
  (`gpu:a100:7(IDX:0-4,6-7)`), and the node view already fetches that. The two columns
  that do cost something wait for a key — `g` resolves which job holds each GPU (one
  `scontrol` per job on the node), `Shift+G` reads live utilisation and memory (one round
  trip). Neither runs on the refresh: browsing a large partition would otherwise turn
  every cursor move into a burst of round trips.

  `node_expand` chooses what a row unfolds into (`gpu` by default, falling back to the CPU
  allocation on a node with no GRES) and `gpu_column` chooses whether the GPUs column reads
  `7/8` or `▣▣▢▣▣▣▣▣`, which shows the free slot without expanding anything.

### Changed

- **The partition monitor opens on what it already knows**
  ([#72](https://github.com/RobinU434/LazySlurm/issues/72)). Pressing `p` used to await
  three Slurm calls before drawing anything: `sinfo`, a **cluster-wide `squeue`** across
  every user and partition (only to fill the running/pending column), and then a third
  call for the highlighted partition's jobs. Nothing was cached and the screen was
  rebuilt on every open, so closing and reopening it paid the whole bill again. On a
  busy controller — or over SSH, where the shared shell serialises all three behind
  whatever poll is already in flight — that is seconds of empty tables.

  Now `sinfo` paints the table (served from the same 45-second cache the cluster bar
  already fills, so opening the monitor after a poll usually costs no command at all),
  and the two `squeue` calls fill the job list and the running/pending column behind the
  paint. The cluster-wide count is cached for 15 seconds, the screen is kept alive
  between opens so reopening is instant, and moving the cursor debounces by 150ms
  instead of firing one `squeue` per row it passes over. `r` still bypasses every cache.

- **A drained node's GPUs no longer read as free.** The GPUs column coloured `0/8` green
  on a node nothing can be scheduled onto, which is exactly the wrong conclusion — the
  same reasoning the Load column already applied by showing `—` there. Both readings of
  the column are now dimmed for a node that is down, drained, failing or in maintenance.

- **A refresh no longer re-reads the whole job history**
  ([#63](https://github.com/RobinU434/LazySlurm/issues/63)). `sacct` was 69% of every
  poll and returned a byte-identical answer each time, because a job that has ended does
  not change again. It scaled badly with the window: 112ms at `days = 7`, **1.5s at
  `days = 30`**, 4.5s at 90 — paid on every refresh.

  The window is now read once and kept in memory, and each refresh re-reads only the
  couple of minutes that can still have moved, merging the result. This is safe because
  `sacct --starttime` selects jobs in any state *during* the window rather than jobs that
  started in it, so a job that began days ago and ended a moment ago still shows up in a
  two-minute window. The whole window is re-read every ten minutes anyway, in case an
  accounting row was revised after the fact.

  Two smaller ones alongside it: the cluster bar's `sinfo` call is cached for 45 seconds
  (`r` bypasses that, since it is a plain staleness cache), and the job tables no longer
  render every row to discover that nothing changed.

  Measured on a login node, steady-state poll: **132ms → 33ms** at `days = 7`, and
  **~1.6s → 44ms** at `days = 30`. Pressing `r` deliberately does *not* force the
  expensive re-read — the incremental query is already current, and with
  `refresh = 0` that keypress is the only path there is.

- **The cpu and gpu tabs no longer stall a refresh**
  ([#63](https://github.com/RobinU434/LazySlurm/issues/63)). Two things made the meter
  and graph modes feel slow to refresh.

  The sample paused for half a second *on the node*, to take the second `/proc/stat`
  snapshot a utilisation figure needs. That snapshot is now kept from the previous
  sample and subtracted instead, so only the first sample after opening a job pays for
  it — the reading then covers the gap since the last refresh, the way htop reports the
  gap since its last draw. A snapshot older than a minute is not used, since that would
  report a minute's average as if it were the current load.

  And pressing `r` sampled *both* tabs regardless of which one was on screen, spending a
  whole extra round trip (a serialized one, in remote mode) on a panel nobody was
  looking at. It now refreshes the visible tab, as the auto-refresh already did.

  Pressing `r` on the cpu tab in graph mode, measured against a live job: **~690ms →
  187ms**, of which the sample itself is 625ms → 99ms.

  A follow-up, because the first cut of this helped the wrong person. The kept snapshot
  expired after a minute, so it never survived from one manual `r` to the next — and with
  `refresh = 0` that keypress is the only path there is. It is kept for ten minutes now,
  and the panel says what the percentages average over (`· last 3m 12s`) rather than
  implying they are current; a sample that timed itself covers half a second and still
  reads as "now". The node sample is also started before the poll rather than after it,
  since nothing in the poll depends on it, and a refresh no longer resets the meters to
  their placeholder when the job on screen has not changed. A manual `r` in graph mode:
  **~865ms → 150ms**.

### Fixed

- **The job list kept polling into a screen it could not see**
  ([#72](https://github.com/RobinU434/LazySlurm/issues/72)). With `refresh` on, a poll
  that landed while the partition monitor was open looked for the job tables on the
  *visible* screen and raised `NoMatches`, taking the tick down with it. It now
  addresses the job list directly, and the cpu/gpu sampler skips its ssh round trip
  entirely while a full-screen monitor is on top of it.

### Added

- **The running version, in the footer**. The right edge of the footer now names the
  build: `v0.3.0` for a release from PyPI, `v0.3.0+g1a2b3c4` when the code came from
  git. The commit is read from the work tree when there is one — an editable install or
  a clone run in place — and otherwise from what `pip` recorded when installing from a
  VCS URL ([PEP 610](https://peps.python.org/pep-0610/)'s `direct_url.json`), so
  `pip install git+https://...` is identified too. `lazyslurm --version` (`-V`) prints
  the same string. Anyone running LazySlurm on more than one machine has at some point
  wondered which of them is out of date; now the answer is on screen, and a bug report
  can name a commit rather than a version.

- **Resource monitoring: htop/nvtop-style CPU and GPU meters with history**
  ([#59](https://github.com/RobinU434/LazySlurm/issues/59)). The **cpu** tab was a `ps`
  listing and the **gpu** tab was raw `nvidia-smi` — both answered "what is running right
  now" and nothing else. They now draw one bar per *allocated* core (the job's cgroup, not
  the node's core count) with the job's memory and the node's load beneath, and per-device
  utilisation, memory, temperature and power for each GPU. In `graph` mode every metric
  also carries a history band, which is what distinguishes a job that is ramping up from
  one that has plateaued and one that has stalled.

  `Shift+M` cycles the two tabs through `text` → `meter` → `graph` for the session;
  `resource_monitor` in `config.toml` sets what they open on (`graph` by default). `text`
  is kept as a full mode rather than dropped, because the meters cannot show what the raw
  output carries: the process list, ECC counters and MIG partitions.

  The cost is unchanged — one round trip per tab refresh, as before. The two `/proc/stat`
  snapshots that a utilisation figure needs are taken on the node itself, half a second
  apart, so the numbers are instantaneous rather than a five-second average, and the
  remote path gains no extra hops through the shared session. The sample runs inside the
  job's cgroup via `srun --overlap`, falling back to SSH and saying when it does.
  `--no-live` still switches everything off.

## 0.3.0 — 2026-08-13

Job arrays fold into one row, a failed job can be resubmitted with more of what it
ran out of, and remote mode is materially more reliable — the two-factor login no
longer asks for the code repeatedly, and a slow one no longer leaks threads or
loses the prompt. Alongside that, sixteen fixes: a job name containing a "|"
misparsed the whole row, a down node was reported as out of memory when its
memory was simply unknown, and the sizing hint could advise a zero time limit —
which Slurm reads as no limit at all.

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
- **Coverage reporting.** `uv run pytest --cov` produces a report with no further setup
  (`--cov-report=html` for a browsable one). Branch coverage is on and the run fails
  below 68%, a floor to catch a drop rather than a target.

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

### Documentation

- The README is now a menu: each section folds open, so finding one feature no longer
  means scrolling past a dozen others. Headings stay visible, so every cross-link still
  lands somewhere you can see.
- New sections on the [ssh vs srun trade-off](README.md#interactive-shell-ssh-vs-srun)
  — including why `nvidia-smi` may show more GPUs than you allocated — and on
  resubmitting with more resources.

### Fixed

- **Remote mode asked for the one-time password several times**
  ([#56](https://github.com/RobinU434/LazySlurm/issues/56)). The refresh timer started at
  mount rather than after the SSH login, so every tick that landed while the user was
  still typing their code found the session "not connected", tried to reconnect, saw no
  live master — because the first one was still authenticating — and started another
  `ssh -M`, which asked for another code. `connect()` now holds the same lock `run()`
  takes, so a command arriving mid-login waits for that login, and the timers do not
  start until the session is up. A genuinely dropped connection still re-authenticates.

- **Starting off a cluster crashed instead of explaining**
  ([#54](https://github.com/RobinU434/LazySlurm/issues/54)). With no Slurm commands on
  `PATH`, the first poll raised `FileNotFoundError: 'squeue'` and dumped a traceback over
  the TUI. LazySlurm now checks before starting and exits with a message that points at
  `--remote`, and a command that goes missing while it runs degrades to an empty panel
  with one line in the Command Log rather than taking the app down. In remote mode the
  only local requirement is `ssh`.

- **A slow SSH login leaked a thread per second, and could swallow the 2FA prompt**
  ([#52](https://github.com/RobinU434/LazySlurm/issues/52)). The auth pump started a
  fresh pty read every second; `asyncio.wait_for` cancels the future on timeout but
  cannot cancel a thread already blocked in `os.read`, so each quiet second left another
  reader stuck on the same descriptor. When ssh finally wrote, the kernel could hand the
  text to a reader nobody was awaiting — the prompt vanished and the login hung until the
  120s timeout. The leaked threads also came out of the default executor that log reads
  and file checks share, so a slow login degraded the rest of the session. One read now
  stays pending across polls. This bit hardest on exactly the clusters the feature exists
  for: a Duo push waits on a phone, and ten quiet seconds meant ten leaked threads.
- **A `|` in a job name shifted every column of its row**
  ([#47](https://github.com/RobinU434/LazySlurm/issues/47)). Slurm does not escape the
  delimiter it prints between fields, so a job named `train|v2` produced an extra field
  and every column after the name was read one place to the right — including `state`,
  which drives colours and filters, and `work_dir`, which resubmit passes as `--chdir`.
  The row was not rejected, just silently wrong. Surplus pieces are now folded back into
  the name. A node's drain `Reason` keeps its pipes and spacing too.
- **A down node was shown as 100% memory used**
  ([#48](https://github.com/RobinU434/LazySlurm/issues/48)). `sinfo` reports `FreeMem` as
  `N/A` for a node that has not reported, which was read as zero free — so an unreachable
  node rendered a red `503/503G`. Unknown now renders `—`, as the load column in the same
  row already did.
- **The stats tab could advise `--time=00:00:00`**
  ([#49](https://github.com/RobinU434/LazySlurm/issues/49)). The sizing hint truncated to
  whole minutes with no floor, so a job shorter than 40 seconds was told to ask for zero —
  which Slurm reads as *unlimited*, the opposite of the advice. Suggestions now round up
  and never fall below what the job already used.

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
