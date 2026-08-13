<p align="center">
  <img src="https://raw.githubusercontent.com/RobinU434/LazySlurm/main/img/Logo-LazySlurm.png" alt="LazySlurm" width="260">
</p>

<p align="center">
  A terminal UI for monitoring Slurm HPC jobs — like <code>htop</code> for your cluster.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/built%20with-Rust-000000.svg?logo=rust" alt="Built with Rust">
  <a href="https://slurm.schedmd.com/"><img src="https://img.shields.io/badge/Slurm-HPC-informational.svg" alt="Slurm"></a>
  <img src="https://img.shields.io/badge/platform-Linux-lightgrey.svg" alt="Platform: Linux">
  <img src="https://img.shields.io/badge/status-alpha-orange.svg" alt="Status: alpha">
</p>

LazySlurm gives you a live overview of your running and past jobs, lets you read
stdout/stderr logs, inspect resource usage, monitor CPU and GPU activity on
compute nodes, cancel, edit or resubmit jobs, and watch how busy each partition
is — all from a single terminal. It runs against the local cluster or, with
`--remote`, over a single background SSH connection that handles two-factor
authentication once at startup.

> **This branch is the Rust implementation.** It is a port of the original
> Python/Textual version, which is kept under `reference/python/` for
> comparison. See [Status](#status) for what that means in practice.

<p align="center">
  <img src="https://raw.githubusercontent.com/RobinU434/LazySlurm/main/img/main.png" alt="LazySlurm main view" width="100%">
</p>

## Installation

LazySlurm ships as a Python wheel containing a compiled binary. There is no
Python code in it and no Python dependency at runtime — the wheel is purely a
convenient way to get a binary onto a login node, where `pip` is usually the one
package manager everybody already has.

```bash
pip install lazyslurm-rs

# or, with uv
uv tool install lazyslurm-rs
```

The command is `lazyslurm`.

> **If you also have the Python version installed**, note that `lazyslurm-py` and
> `lazyslurm-rs` both provide a `lazyslurm` executable. Installing both into one
> environment means whichever was installed last wins. Pick one per environment.

### From source

```bash
git clone https://github.com/RobinU434/LazySlurm.git
cd LazySlurm
cargo install --path .
```

Or build the wheel yourself:

```bash
pip install maturin
maturin build --release
pip install target/wheels/lazyslurm_rs-*.whl
```

> Installing the **sdist** (`pip install --no-binary lazyslurm-rs lazyslurm-rs`)
> compiles from source and therefore needs a Rust toolchain. Without one the
> install fails with `cargo: not found`. Prefer the wheel.

## Quick start

```bash
# On a cluster login node
lazyslurm

# From your laptop, monitoring a remote cluster
lazyslurm --remote user@login.hpc.edu

# Refresh every 3 seconds, 14 days of history
lazyslurm --refresh 3 --days 14

# No auto-refresh; press r instead
lazyslurm --refresh off
```

## Layout

```
┌─ user  12 running  3 pending   gpu:18/2/1/21  cpu:4/0/0/4 ──────────────┐
│ ┌── Active Jobs ─────────┐ ┌── Job Details ───────────────────────────┐ │
│ │ Job ID  Name  Elapsed  │ │ stdout  stderr  cpu  gpu  stats          │ │
│ │ 4815162 train 1:12:04  │ │                                          │ │
│ └────────────────────────┘ │                                          │ │
│ ┌── Terminated Jobs ─────┐ └──────────────────────────────────────────┘ │
│ │ Job ID  Name  State    │ ┌── Job Metadata ──────────────────────────┐ │
│ │ 4815100 prep  COMPLETED│ │ Resources  Submission  Pending  Raw      │ │
│ └────────────────────────┘ └──────────────────────────────────────────┘ │
│                            ┌── Command Log ───────────────────────────┐ │
│                            │ 14:22:01 scancel 4815162                 │ │
│                            └──────────────────────────────────────────┘ │
└─ q quit  ? help  / filter  r refresh  Tab panel ────────────────────────┘
```

## Key bindings

Press `?` for the keys of **the panel you are in**. The help is generated from
the same table the keys are dispatched from, so it cannot drift out of date.

### Everywhere

| Key | Action |
|-----|--------|
| `?` | Help for the current panel |
| `/` | Filter the job tables. `Enter` accepts and returns to the list, `Escape` abandons |
| `r` | Refresh now. In remote mode, retries a dropped or refused SSH session |
| `Tab` / `Shift+Tab` | Move between the four panels |
| `,` | Edit `~/.config/lazyslurm/config.toml`, then reload it live |
| `q`, `Ctrl+C` | Quit |

### Job tables

| Key | Action |
|-----|--------|
| `↑` `↓` / `k` `j` | Move through the list — wraps between Active and Terminated |
| `g` / `G` | First / last row |
| `Enter` | Expand or collapse a job array (`▸` row) |
| `m` | Bookmark — `★` rows pin to the top |
| `c` | Cancel the selected job(s), with a confirmation |
| `Shift+C` | **Force cancel** — SIGKILL, no confirmation |
| `Ctrl+V` | Multi-select; `↑`/`↓` extends the range |
| `u` | Edit a **pending** job: runtime, partition, nodes, CPUs, memory |
| `s` | Resubmit a **terminated** job from its original submit line |
| `b` | View the job's sbatch script, read-only |
| `o` | SSH to the job's compute node (suspends the TUI) |
| `p` | Partition monitor |
| `Shift+U` | Account usage and fair share |

### Job Details / Job Metadata

| Key | Action |
|-----|--------|
| `[` / `]` | Previous / next tab in Job Details |
| `(` / `)` | Previous / next tab in Job Metadata |
| `↑` `↓` / `k` `j` | Scroll |
| `l` | Open the active log in your pager — the way to read a huge log |
| `e` / `Shift+E` | Open stdout / stderr in your editor |

### Partition monitor, node view, account usage

| Key | Action |
|-----|--------|
| `↑` `↓` | Move; the lower pane follows |
| `Enter` | (partitions) show that partition's nodes |
| `Tab` | Switch between the two panes |
| `w` | (usage) cycle the window: this month → last 30 days → this year |
| `Escape` | Back |

## Features

### Filtering

`/` opens the filter bar. Plain words match the job id, name and partition;
`key:value` terms are ANDed together:

```
train state:pend part:gpu gpu:>=2
```

| Key | Aliases | Matches |
|-----|---------|---------|
| `state:` | `st:` `s:` | State, by prefix — `pend` finds PENDING |
| `part:` | `partition:` `p:` | Partition, substring |
| `name:` | `n:` | Job name, substring |
| `id:` | `job:` | Job id, substring |
| `gpu:` | `gpus:` `gres:` | GPU count, with `>` `>=` `<` `<=` `=` `!=` |

An unrecognised key is searched as plain text, so nothing you type can break the
filter. The table title shows `— 2/40 match` while one is active.

### Job arrays

A job array folds into one row with its index range and a state tally:

```
▸ 4815201_[0-11] ×12   sweep-lr   2run 10pend   gpu
```

`Enter` expands it. Actions on a collapsed row apply to the whole array —
`scancel 4815201` takes every task. An array you expanded stays expanded across
refreshes, and the cursor stays on the same job rather than the same row number.

### Efficiency report

The **stats** tab opens with what the job used against what it reserved — the
same definitions `seff` uses:

```
Efficiency
  CPU             2.1 / 8 cores      26%  ▆▆▁▁▁▁▁▁
  Memory         6.2G / 64G           9%  ▆▁▁▁▁▁▁▁  ← over-requested
  Walltime      1:04:12 / 8:00:00    13%  ▆▁▁▁▁▁▁▁
  next time try --mem=8G --cpus-per-task=3 --time=01:36:00
```

Memory is measured against the request **per node**, so a multi-node job is not
credited with memory it never touched on one node.

### Why is my job pending?

For a waiting job, the Metadata panel grows a **Pending** tab: Slurm's reason
code in plain language, its backfill start estimate, and where it sits in the
queue with the priority factors that put it there.

### Partition and node monitors

`p` shows every partition with its node and CPU `A/I/O/T` counters and a load
bar, plus every user's jobs on the highlighted one. `Enter` drills into the
individual nodes — state, load, memory, GPUs used, and the drain reason when
Slurm has one.

### Account usage

`Shift+U` shows where the allocation went and what it costs you: CPU-hours per
user in your account, and the fair-share factor that actually drives queue
order.

### Live node monitoring

The **cpu** and **gpu** tabs read from the compute node itself while the tab is
open. `gpu` runs `nvidia-smi` inside the job's cgroup via `srun --overlap`, so it
shows the GPUs the job actually holds; if that fails it falls back to SSH and
says so rather than implying every GPU on the node is yours.

### Remote mode

`--remote user@login.hpc.edu` runs every Slurm command over **one** SSH
connection, opened once at startup:

- Authentication happens once. A password or verification-code prompt is
  forwarded to a dialog in the TUI, so two-factor logins work.
- Every later command reuses that connection — no re-authentication, no new
  process per command.
- The pager runs *on the cluster* rather than copying a multi-gigabyte log down,
  and `o` hops to a compute node through the connection that is already open.

If the connection drops or you mistype a code, `r` retries it.

## Configuration

`~/.config/lazyslurm/config.toml`, honouring `XDG_CONFIG_HOME`. Press `,` to
create and edit it; changes apply as soon as the editor closes.

```toml
refresh = 5.0              # auto-refresh interval in seconds (0 = off)
days = 7                   # how far back the terminated table reaches
user = ""                  # Slurm user to monitor (empty = $USER)
partition = ""             # restrict every view to one partition
no_gpu = false             # hide the GPU tab
no_live = false            # hide both live monitoring tabs
editor = "vim"             # for logs, scripts and this file
pager = "less"             # for browsing whole logs with 'l'

max_name_width = 16        # 0 = unlimited
max_partition_width = 16
abbreviate_states = false  # COMP, FAIL, OOM, ...
collapse_arrays = true     # fold an array into one expandable row

cache_max_age_days = 30    # 0 = never prune
script_cache_dir = ""      # default: <config dir>/scripts

partition_order = ["gpu", "cpu", "fat"]

[partition_colors]
gpu = "green"
cpu = "cyan"
```

Command-line arguments override the file; the command log says which settings
were overridden and names any key it did not recognise, so a typo is visible
rather than silently doing nothing.

## CLI reference

```
lazyslurm [OPTIONS]

  -r, --refresh <SEC>      Auto-refresh interval. 0 or 'off' disables it
  -d, --days <N>           How many days back to show terminated jobs
  -u, --user <USER>        Slurm user to monitor (default: current user)
  -p, --partition <NAME>   Filter jobs by partition
      --no-gpu             Disable the live GPU tab
      --no-live            Disable live CPU/GPU tabs (no SSH to nodes)
      --partition-order    Comma-separated display order, e.g. gpu,cpu,fat
  -H, --remote <HOST>      SSH target, e.g. user@login.hpc.edu
      --completions <SHELL> Print a completion script (bash, zsh, fish, …)
  -h, --help               Print help
  -V, --version            Print version
```

### Shell completions

```bash
lazyslurm --completions bash > ~/.local/share/bash-completion/completions/lazyslurm
lazyslurm --completions zsh  > ~/.zfunc/_lazyslurm
lazyslurm --completions fish > ~/.config/fish/completions/lazyslurm.fish
```

## Caches

LazySlurm keeps two small caches under its config directory, because Slurm
forgets a job shortly after it ends:

- **`log_cache.json`** — the stdout/stderr paths and submit command of every job
  seen, recorded while `scontrol` still knows them. This is what lets you open
  the logs of a job that finished last week.
- **`scripts/`** — the text of each job's sbatch script, archived while the job
  is live. After `MinJobAge` (often five minutes) Slurm cannot produce it at all,
  so this copy is the only one. Owner-readable only, since batch scripts
  routinely hold tokens and private paths.

Both are pruned by `cache_max_age_days`.

## Requirements

- Linux, and the Slurm CLI tools: `squeue`, `sacct`, `scontrol`. `sstat`,
  `sinfo`, `sprio`, `sreport` and `sshare` enable the corresponding panels and
  are detected at runtime — a cluster without accounting is told so rather than
  shown an empty table.
- For remote mode: an `ssh` client that supports connection multiplexing
  (OpenSSH 5.6+).
- No Python runtime, and no Rust toolchain unless you build from the sdist.

## Status

Every feature of the Python version is implemented. What that does **not** yet
mean:

- The Rust port has been tested against recorded cluster output, a local shell
  standing in for the SSH channel, and a scripted two-factor login — but not yet
  against a live Slurm cluster. Treat this as alpha until it has been.
- A handful of behaviours deliberately differ from the Python, each because the
  Python has a bug or a documented option that cannot be expressed. They are
  listed with their reasoning in [DIVERGENCES.md](DIVERGENCES.md).

Porting notes and the phase-by-phase plan are in
[RUST_PORT_PLAN.md](RUST_PORT_PLAN.md) and [PORT_STATUS.md](PORT_STATUS.md). If
you have a cluster and want to help finish it,
[AGENT_HANDOFF.md](AGENT_HANDOFF.md) says exactly what is left.

## Development

```bash
cargo test
cargo clippy --all-targets -- -D warnings
cargo fmt --all -- --check
```

The interesting half of the codebase needs no terminal: filtering, array
grouping, cursor movement, key dispatch and every generated report are plain
state and are asserted directly. Rendering is checked with `ratatui`'s
`TestBackend`.

`reference/python/` holds the original implementation. It still runs
(`cd reference/python && uv pip install -e .`), which is useful for comparing
the two side by side against a real cluster.

## License

MIT — see [LICENSE](LICENSE).
