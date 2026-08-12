<p align="center">
  <img src="https://raw.githubusercontent.com/RobinU434/LazySlurm/main/img/Logo-LazySlurm.png" alt="LazySlurm" width="260">
</p>

<p align="center">
  A terminal UI for monitoring Slurm HPC jobs — like <code>htop</code> for your cluster.
</p>

<p align="center">
  <a href="https://pypi.org/project/lazyslurm-py/"><img src="https://img.shields.io/pypi/v/lazyslurm-py.svg?color=blue" alt="PyPI version"></a>
  <a href="https://pypi.org/project/lazyslurm-py/"><img src="https://img.shields.io/pypi/pyversions/lazyslurm-py.svg" alt="Python versions"></a>
  <a href="https://pypi.org/project/lazyslurm-py/"><img src="https://img.shields.io/pypi/dm/lazyslurm-py.svg?color=blue" alt="PyPI downloads"></a>
  <a href="LICENSE"><img src="https://img.shields.io/pypi/l/lazyslurm-py.svg?color=green" alt="License: MIT"></a>
  <a href="https://github.com/RobinU434/LazySlurm"><img src="https://img.shields.io/github/stars/RobinU434/LazySlurm.svg?style=social" alt="GitHub stars"></a>
  <br>
  <a href="https://github.com/Textualize/textual"><img src="https://img.shields.io/badge/built%20with-Textual-5967FF.svg" alt="Built with Textual"></a>
  <a href="https://slurm.schedmd.com/"><img src="https://img.shields.io/badge/Slurm-HPC-informational.svg" alt="Slurm"></a>
  <img src="https://img.shields.io/badge/platform-Linux-lightgrey.svg" alt="Platform: Linux">
  <img src="https://img.shields.io/badge/status-beta-orange.svg" alt="Status: beta">
</p>

LazySlurm gives you a live overview of your running and past jobs, lets you read
stdout/stderr logs, inspect resource usage, monitor CPU and GPU activity on compute
nodes, cancel, edit or resubmit jobs, and watch how busy each partition is — all from a
single terminal. It runs against the local cluster or, with `--remote`, over a single
background SSH connection that handles two-factor authentication once at startup.

<p align="center">
  <img src="https://raw.githubusercontent.com/RobinU434/LazySlurm/main/img/main.png" alt="LazySlurm main view" width="100%">
</p>

## Installation

Requires Python 3.10+ and access to Slurm CLI tools (`squeue`, `sacct`, `scontrol`).

```bash
# Install from PyPI (the distribution is named lazyslurm-py, the command is lazyslurm)
pip install lazyslurm-py

# Or with uv
uv tool install lazyslurm-py
```

From source:

```bash
# Clone the repository
git clone https://github.com/RobinU434/LazySlurm.git
cd LazySlurm

# Install with uv (recommended)
uv pip install -e .

# Or with pip
pip install -e .
```

You can also install the repository directly from remote via: 

```bash
# Install with uv (recommended)
uv tool install git+ssh://git@github.com/RobinU434/LazySlurm.git

# Or via pip into the local python environment
pip install git+ssh://git@github.com/RobinU434/LazySlurm.git
```

## Quick Start

```bash
# Run on a cluster login node
lazyslurm

# Run from your local machine, monitoring a remote cluster
lazyslurm --remote user@login.hpc.edu

# Customize refresh rate and time window
lazyslurm --refresh 3 --days 14

# Disable auto-refresh (manual refresh only with 'r')
lazyslurm --refresh off
```

## Layout

LazySlurm has a five-panel main view:

| Panel | Position | Content |
|-------|----------|---------|
| **Active Jobs** | Top-left | Running and pending jobs from `squeue` |
| **Terminated Jobs** | Bottom-left | Completed, failed, timed-out, and cancelled jobs from `sacct` |
| **Job Details** | Top-right (2/3) | Tabbed view: stdout, stderr, live CPU, live GPU, resource stats |
| **Job Metadata** | Middle-right | Tabbed view: Resources, Submission info, Raw scontrol output |
| **Command Log** | Bottom-right | Timestamped log of actions and responses |

A **cluster overview bar** at the top shows your running/pending counts and partition
availability. Press `p` for the separate full-screen [partition monitor](#partition-monitor),
and `/` to reveal the search bar above the job tables.

## Key Bindings

Press `?` at any time for this list inside the app:

<img src="https://raw.githubusercontent.com/RobinU434/LazySlurm/main/img/help.png" alt="Help screen" width="100%">

### Navigation

| Key | Action |
|-----|--------|
| `Up` / `Down` | Navigate job list (wraps between Active and Terminated). The selected job's details update immediately. |
| `Tab` / `Shift+Tab` | Switch focus between right-side panels |
| `Left` / `Right` | Switch focus between right-side panels |
| `[` / `]` | Switch tabs in the **Job Details** panel (stdout, stderr, cpu, gpu, stats) |
| `(` / `)` | Switch tabs in the **Job Metadata** panel (Resources, Submission, Raw) |
| `Escape` | Close search bar |

### Actions

| Key | Action |
|-----|--------|
| `/` | Open search bar — filter jobs by ID, name, or partition. Press `Escape` to close and clear |
| `m` | Bookmark / unbookmark the selected job. Bookmarked jobs show a ★ prefix and are pinned to the top of their table |
| `c` | Cancel the selected job (with confirmation prompt) |
| `Shift+C` | **Force cancel** — sends SIGKILL immediately, no confirmation |
| `Ctrl+V` | Toggle multi-select mode (vim-visual style). Use Up/Down to extend the selection range from an anchor row, then press `c` or `Shift+C` to cancel all selected jobs. Press `Ctrl+V` again to exit. Detail panels freeze on the last single-selected job. |
| `s` | Resubmit a terminated job using its original sbatch script (with confirmation) |
| `u` | Edit a **pending** job's properties: runtime, partition, nodes, CPUs, memory. Works on a multi-selection too |
| `p` | Open the [partition monitor](#partition-monitor) — per-partition load and every user's jobs. `Escape` or `p` returns |
| `b` | View the job's sbatch script, read-only, in your editor |
| `l` | Open the active log tab (stdout or stderr) in a **pager** — the way to read a huge log |
| `e` | Open the job's **stdout** log in an external editor (suspends TUI) |
| `Shift+E` | Open the job's **stderr** log in an external editor |
| `o` | SSH to the selected job's compute node. Suspends the TUI; type `exit` to return |
| `,` | Edit config file (`~/.config/lazyslurm/config.toml`) in your editor |
| `r` | Force refresh all job data |
| `?` | Toggle the help screen (also closes with `Escape`) |
| `q` | Quit |

## Detail Tabs

Select a job in either table (Up/Down) and use `[` / `]` to switch between these tabs:

### stdout / stderr

Displays the tail of the job's standard output and error log files. LazySlurm finds log
files by reading `StdOut` / `StdErr` from `scontrol show job`. For older jobs no longer
in Slurm's memory, it checks the **log path cache** first (see
[Log Path Cache](#log-path-cache)), then falls back to searching the working directory
for common patterns (`slurm-JOBID.out`, `JOBNAME-JOBID.out`, `logs/` subdirectory, etc).

### cpu

Live process listing from the job's compute node, similar to `top`. Shows PID, %CPU,
%MEM, RSS, VSZ, elapsed time, and command name. Auto-refreshes while the tab is active.

### gpu

Live `nvidia-smi` output showing **only the GPUs allocated to the selected job**. Uses
`srun --overlap --jobid` to run nvidia-smi inside the job's cgroup, so GPU visibility
is automatically restricted to the job's allocation. The header shows
`CUDA_VISIBLE_DEVICES` for confirmation. Auto-refreshes while the tab is active.

### stats

Accounting statistics from `sstat` (running jobs) and `sacct`:

- **CPU** — average CPU time, total CPU, frequency, wall time
- **Memory** — requested, max/average RSS, max/average VM size, peak node/task
- **GPU** — allocated GPU count and type from TRES
- **Disk I/O** — average and max read/write

<img src="https://raw.githubusercontent.com/RobinU434/LazySlurm/main/img/stats.png" alt="Resource stats with sparkline history" width="100%">

## Metadata Tabs

Use `(` / `)` to switch between these tabs:

### Resources

Partition, node count, CPUs, memory, GPU/GRES allocation, TRES, time limit, runtime,
account, and QoS.

### Submission

Submit time, start/end times, working directory, stdout/stderr paths, and the original
submit command.

### Raw

All key-value pairs from `scontrol show job` or `sacct`, displayed verbatim.

## Visual Features

### Column Width Limits

Job names and partition names are truncated to 16 characters by default (with `…` when
truncated). This keeps the tables compact. Configure via `config.toml`:

```toml
max_name_width = 20       # wider name column
max_partition_width = 10  # narrower partition column
```

Set to `0` for unlimited width.

### State Abbreviations

For compact displays, enable abbreviated state names in the Terminated Jobs table:

```toml
abbreviate_states = true
```

| Full State | Abbreviation |
|------------|-------------|
| COMPLETED | COMP |
| FAILED | FAIL |
| TIMEOUT | TIME |
| CANCELLED | CAN |
| OUT_OF_MEMORY | OOM |
| NODE_FAIL | NFAIL |
| PREEMPTED | PREEMPT |

### Color-Coded Partitions

Each partition is assigned a consistent color across both job tables. Colors are
deterministic (based on the partition name) so they stay stable across sessions. You can
override colors in the config file (see [Configuration](#configuration)).

### Color-Coded Job States

**Active Jobs** (Job ID column):

| State | Color |
|-------|-------|
| RUNNING | Green |
| PENDING | Yellow |
| COMPLETING | Orange |
| SUSPENDED, REQUEUED | Dim |

**Terminated Jobs** (State column):

| State | Color |
|-------|-------|
| COMPLETED | Green |
| FAILED, OUT_OF_MEMORY, NODE_FAIL | Red |
| TIMEOUT | Yellow |
| CANCELLED | Dim grey |
| PREEMPTED | Dim yellow |

### Cluster Overview Bar

The top line shows a summary of your jobs and cluster partitions:

```
jdoe  5 running  2 pending    gpu:10/5/1/16  cpu:42/58/0/100
```

Partition format is `name:A/I/O/T`:

| Field | Meaning |
|-------|---------|
| **A** | Allocated — nodes currently running jobs |
| **I** | Idle — nodes available for new jobs |
| **O** | Other — nodes that are down, drained, or in maintenance |
| **T** | Total — total nodes in the partition |

### Bookmarks

Press `m` to bookmark any job. Bookmarked jobs are pinned to the top of their table with
a ★ prefix. Bookmarks persist for the duration of the session.

### Partition Monitor

Press `p` for a full-screen view of the cluster's partitions. The main job tables only
ever show **your** jobs — this screen shows everyone's, so you can see what a partition is
actually busy with before you queue into it.

<img src="https://raw.githubusercontent.com/RobinU434/LazySlurm/main/img/partitions.png" alt="Partition monitor" width="100%">

**Partition table** — `A/I/O/T` is Slurm's allocated / idle / other / total counter, given
for both nodes and CPUs. "Other" is down, drained, or reserved capacity. The **Load** bar
is allocated CPUs over *usable* CPUs (allocated + idle), so drained nodes don't make a
saturated partition look half empty; it turns yellow past 60% and red past 90%. `Run` and
`Pend` are job counts across all users, and a pending job that names several partitions
counts in each. Partitions that are `down` are shown struck-through instead of with a bar,
rather than hidden.

**Job table** — every user's jobs on the highlighted partition, running first then pending,
newest first. Your own jobs are marked `▸` and highlighted. For pending jobs the last
column shows Slurm's reason (`(Resources)`, `(QOSMaxGRESPerUser)`, `(Dependency)`, …)
instead of a node list.

| Key | Action |
|-----|--------|
| `Up`/`Down` | Move between partitions; the job table follows the highlighted one |
| `Tab` | Switch focus between the partition and job tables (to scroll a long job list) |
| `r` | Refresh now |
| `Escape` / `p` / `q` | Back to the main view |

The screen re-polls on your `--refresh` interval while it is open, and stops when you
leave. `--partition-order` also orders this table. Note that `sinfo --summarize` reports
one row per node *configuration*, so partitions with mixed hardware are summed into a
single row here.

### Edit Pending Jobs

Press `u` on a pending job to open the property editor:

| Field | `scontrol` key | Notes |
|-------|----------------|-------|
| Runtime | `TimeLimit` | Slurm time formats: `4:00:00`, `2-00:00:00`, `30` (minutes) |
| Partition | `Partition` | Comma-separated list is allowed, e.g. `gpu,gpu-long` |
| Nodes | `NumNodes` | |
| CPUs | `NumCPUs` | |
| Memory/node | `MinMemoryNode` | Accepts `40G`, `4000M`, or plain MB — converted to the MB integer Slurm expects |

<img src="https://raw.githubusercontent.com/RobinU434/LazySlurm/main/img/edit-job.png" alt="Editing a pending job" width="100%">

The modal reads like a small file you edit: numbered lines, the `scontrol` key as the
label, the job id as the "filename" in the border. The values are prefilled from the job.
`Up`/`Down` and `Tab`/`Shift+Tab` move between lines (wrapping at either end), `Left`/`Right`,
`Home`/`End` and `Backspace` edit within a line, `Ctrl+S` writes, `Escape` quits.
Only fields you actually changed are sent, as a single
`scontrol update jobid=<id> Key=Value ...` per job, and every command plus Slurm's reply is
written to the Command Log.

**Pending jobs only.** Once a job starts, Slurm fixes its runtime, partition, and resource
allocation, so the editor refuses to open for running jobs and says so in the status line.

With a `Ctrl+V` multi-selection active, `u` edits all selected pending jobs at once. Then
the fields start **blank** and only the ones you fill in are applied to every job —
non-pending jobs in the selection are skipped and listed in the Command Log.

### View sbatch Script

Press `b` to open the selected job's sbatch script in your editor, read-only. Works on
pending, running, and terminated jobs. The TUI suspends while the editor is open.

With `vim` the file opens via `-R`, so you can still `:w /some/other/path` to keep a copy
— useful when the original script has been changed since the job was submitted. Editors
without a known read-only flag open writable, and the Command Log says so.

The script **text** is archived, not just its path, so your copy stays readable even if you
later edit, move, or delete the original file. Archiving happens automatically whenever you
select a job that Slurm still knows about, and on demand when you press `b`. Array tasks
share one script: `123_11`, `123_[1-40]`, and `123` all resolve to the same file.

**Slurm only keeps a job's script until `MinJobAge` seconds after it ends** — often just
300. A job that finished before LazySlurm ever saw it has no archived copy and Slurm can no
longer produce one; pressing `b` reports that the script is unavailable. See
[Job Cache](#job-cache) for the cache location and `script_cache_dir`.

### Reading Big Logs

The stdout/stderr tabs show the **last 500 lines**, read by seeking backwards from the end
of the file — a 200 MB log tails as fast as an empty one, and the panel never blocks the
UI while a job floods its log.

To read more than the tail, press `l` to open the active tab's log in a pager (`less` by
default). This is the right tool for a multi-gigabyte log: it seeks instead of loading, so
it opens instantly at the end of the file, and inside it you get

| In `less` | |
|-----------|--|
| `/pattern`, `n` / `N` | search forwards and back |
| `G` / `g` | jump to end / start |
| `F` | **follow** the file as the job writes to it (like `tail -f`); `Ctrl+C` stops |
| `q` | back to LazySlurm |

The TUI suspends while the pager is open. In remote mode the pager runs **on the cluster**
over the existing SSH connection, so nothing is copied to your machine and you are not
asked for a 2FA code again.

Set your pager in `config.toml`:

```toml
pager = "less"   # or "more", "bat", "most"
```

`less` is opened with `-R +G` (keep the log's colors, start at the end); other known pagers
get their equivalent end-of-file flag. An unknown pager is run with no flags.

If a log has no line break in its last 4 MB — a progress bar writing `\r` forever — the
tail is cut there and the panel says so; press `l` to see the file properly.

### Open Logs in Editor

Press `e` to open the selected job's stdout log in an external text editor, or
`Shift+E` for stderr. The TUI suspends while the editor is open and resumes when you
close it.

The default editor is `vim`. To change it, set `editor` in your config file:

```toml
editor = "nano"    # or "vim", "less", "code", etc.
```

In **remote mode**, the log file is first copied to a local temp file via `scp`, opened
in the editor, and cleaned up when the editor closes.

If the configured editor is not found on your system, an error is shown in the Command
Log (e.g., `editor 'code' not found — set 'editor' in config.toml`).

### Job Completion Notifications

When a running job finishes (completes, fails, times out, etc.), LazySlurm:
- Rings the terminal bell
- Attempts a desktop notification via `notify-send` (Linux)
- Logs the event in the Command Log panel

### Command Log

The bottom-right panel shows a timestamped log of all actions and their results:

```
14:23:05 refresh
  >>> complete
14:23:12 scancel 2465400
  >>> Job 2465400 cancelled.
14:23:30 ssh galvani-cn109
  >>> session to galvani-cn109 closed
14:24:01 job completed
  >>> 2465485 COMPLETED
```

## Job Cache

Slurm forgets a job shortly after it ends — `MinJobAge` seconds, often just 300. Until
then, `scontrol` can tell you the job's exact `StdOut`/`StdErr` paths and hand you its
sbatch script; afterwards both are gone and only `sacct` remains, which knows neither.

LazySlurm caches both while a job is still live, so they survive the job.

Check your cluster's window with:

```bash
scontrol show config | grep MinJobAge
```

### Batch scripts

Archived as text under the base job ID, so all tasks of an array share one file. See
[View sbatch Script](#view-sbatch-script) for the `b` keybinding and its limitations.

### Log paths

Log paths are cached the same way, into `log_cache.json`, whenever you select a job that
Slurm still knows about. For older jobs LazySlurm falls back to guessing from filename
patterns (`slurm-JOBID.out`, `JOBNAME-JOBID.out`, `logs/` subdirectories), which can fail
if you use custom `--output`/`--error` names.

### Resubmit fallback

Resubmit (**`s`**) runs the job's original sbatch command. If the script file it names no
longer exists, LazySlurm substitutes the archived copy and says so in the Command Log. Not
available in remote mode, where the archive is local but `sbatch` runs on the login node.

### Cache files

| File | Purpose |
|------|---------|
| `~/.config/lazyslurm/log_cache.json` | Cached `StdOut`/`StdErr` paths, work dir, and submit command per job ID |
| `~/.config/lazyslurm/scripts/<job_id>.sh` | Archived sbatch scripts, mode `600` (they often contain tokens and private paths) |

Both are pruned on startup using `cache_max_age_days` (default 30, `null` to never prune).
Set `script_cache_dir` in `config.toml` to archive scripts somewhere else.

> Earlier versions shipped a `lazyslurm-daemon` that polled for log paths in the background.
> It has been removed — caching now happens inline. A leftover
> `~/.config/lazyslurm/daemon.pid` is inert and can be deleted.

## Remote Mode

Run LazySlurm on your local machine while monitoring a remote cluster:

```bash
lazyslurm --remote user@login.hpc.edu
```

### One connection, opened once

LazySlurm opens a **single SSH session in the background at startup** and runs everything
through it. It does not spawn `ssh` per Slurm call:

1. An SSH *master* connection is started on a pty and authenticated once.
2. A shell channel (`/bin/sh -s`) is opened over that master and kept alive for the whole
   session.
3. Each `squeue`, `sacct`, `scontrol`, `sstat`, `scancel`, `sbatch` — and every log-file
   read — is written into that shell and its output read back. No new connection, no new
   process, no re-authentication.

Commands are serialized on the channel, so they queue rather than interleave. If the
channel dies (network drop, remote logout), the next command reopens it automatically, and
only re-authenticates if the master itself is gone. The session is closed when you quit.

### Two-factor authentication

Because the master owns a pty, whatever the cluster asks at login is captured and shown to
you in a modal instead of being lost:

<img src="https://raw.githubusercontent.com/RobinU434/LazySlurm/main/img/two-factor.png" alt="Two-factor prompt" width="100%">

The label is the server's own prompt text, so it reads exactly as it would in a terminal —
`Password:`, `Verification code:`, `Passcode or option (1-3):`, `Token_Response:` and so
on. Input is masked (host-key `(yes/no)` questions are not). You are asked **once**, at
startup, because every later command reuses that connection.

If you mistype a code or press `Escape`, the Command Log says so and nothing is polled;
press `r` to retry the connection. Failures report the server's own message (for example
`Permission denied (keyboard-interactive)`) rather than a generic timeout.

Everything else that shells out reuses the same authenticated connection, so 2FA is never
requested twice:

| Feature | How it reaches the cluster |
|---------|---------------------------|
| Slurm commands, log reads | Written into the shared shell channel |
| Live CPU/GPU tabs | The hop to the compute node is made **from the login node**, inside the session |
| SSH to node (`o`) | A `ProxyCommand` over the session's control socket (not `-J`) |
| Fetching a log for the editor (`e`) | `scp` over the session's control socket |

The control socket lives in `~/.ssh/cm-lazyslurm/`. If you already have a master
connection to that host, it is reused and you are not prompted at all — passwordless keys
therefore still connect with no interaction.

### Other remote notes

When using `--remote user@host`, the username is automatically used as the default
`--user` for Slurm queries (no need to specify both).

The archived-script fallback for resubmission is local-only: the archive lives on your
machine while `sbatch` runs on the login node.

**Login node warning**: If the local or remote hostname contains "login", LazySlurm shows
a warning popup reminding you to be mindful of resource usage on shared login nodes.

## Configuration

LazySlurm stores persistent settings in `~/.config/lazyslurm/config.toml` (respects
`$XDG_CONFIG_HOME`). The file is created automatically when you use `--partition-order`,
or you can create it by hand.

### Example config file

```toml
# All CLI arguments can be set here as defaults.
# CLI arguments always override config file values.
# When a CLI arg overrides a config value, it is shown in the Command Log.

refresh = 3.0            # -r/--refresh: auto-refresh interval in seconds (0 = off)
days = 14                # -d/--days: how many days back for terminated jobs
user = "myuser"          # -u/--user: Slurm user to monitor
partition = ""           # -p/--partition: filter by partition (empty = all)
no_gpu = false           # --no-gpu: disable GPU monitoring tab
no_live = false          # --no-live: disable live CPU/GPU monitoring
remote = ""              # -H/--remote: SSH target for remote mode
editor = "vim"           # text editor for viewing logs ("vim", "nano", "less", etc.)
pager = "less"           # pager for browsing whole logs with 'l' ("less", "more", "bat")

# Column display settings
max_name_width = 16      # max characters for job name column (0 = unlimited)
max_partition_width = 16 # max characters for partition column (0 = unlimited)
abbreviate_states = false # use short state names: COMP, FAIL, TIME, CAN, OOM, ...

# Cache settings
# cache_max_age_days = 30  # auto-delete cached job info older than N days
                           # set to null to never delete (keep forever)
# script_cache_dir = ""    # where archived sbatch scripts live
                           # (default: ~/.config/lazyslurm/scripts)

# Partition display order in the cluster bar.
# Partitions not listed appear after these in their default order.
# Set via CLI: lazyslurm --partition-order gpu,cpu,fat
partition_order = ["gpu", "cpu", "fat"]

# Custom partition colors in the job tables.
# Overrides the automatic hash-based coloring.
# Valid color names: cyan, magenta, yellow, green, blue, red,
# bright_cyan, bright_magenta, bright_green, white, dim, bold,
# or any Rich color (e.g. "dark_orange", "grey50").
[partition_colors]
gpu = "green"
cpu = "cyan"
fat = "magenta"
debug = "dim"
```

### CLI vs config file

All CLI arguments can be set in the config file. The precedence is:

**CLI argument > config file > built-in default**

When a CLI argument overrides a config file value that differs, the override is logged in
the Command Log panel at startup.

### Partition order

To set a custom partition order for the cluster bar:

```bash
# Set once — automatically saved for future sessions
lazyslurm --partition-order gpu,cpu,fat
```

## CLI Reference

### lazyslurm

```
lazyslurm [-h] [-r SEC] [-d N] [-u USER] [-p PARTITION]
         [--no-gpu] [--no-live] [--partition-order P1,P2,...] [-H HOST]
```

| Flag | Description | Default |
|------|-------------|---------|
| `-r`, `--refresh` | Auto-refresh interval in seconds. Set to `0` or `off` to disable. | 5 |
| `-d`, `--days` | How many days back to show terminated jobs | 7 |
| `-u`, `--user` | Slurm user to monitor. When `--remote user@host` is used, defaults to the remote username. | `$USER` |
| `-p`, `--partition` | Filter jobs by partition | (all) |
| `--no-gpu` | Disable the GPU monitoring tab | off |
| `--no-live` | Disable live CPU and GPU monitoring (no SSH/srun to nodes) | off |
| `--partition-order` | Comma-separated partition display order for cluster bar | (sinfo order) |
| `-H`, `--remote` | SSH target for remote mode (e.g. `user@login.hpc.edu`) | (local) |

## Requirements

- Python 3.10+
- Slurm CLI tools: `squeue`, `sacct`, `sinfo`, `scontrol`, `sstat`, `scancel`, `sbatch`
- [Textual](https://textual.textualize.io/) (installed automatically)
- For GPU monitoring: `nvidia-smi` on compute nodes, `srun --overlap` support
- For remote mode: SSH access to the cluster login node

## License

[MIT](LICENSE)
