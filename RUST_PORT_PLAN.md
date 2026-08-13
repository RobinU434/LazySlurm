# LazySlurm → Rust: Implementation Plan

A phased, self-contained plan for rebuilding LazySlurm as a Rust TUI that ships as a
**PyPI-installable wheel** (`pip install lazyslurm-rs` → `lazyslurm` on `$PATH`).

The reference implementation is the Python source in `src/lazyslurm/`. This plan tells
you which file to read for each piece of behaviour, what to build, and how to know
you got it right.

---

## 0. How to use this plan (agent workflow)

This document is written for an agent working in a loop. For **every** work item:

1. **Read** the referenced Python source *in full* before writing Rust. The specs
   below are a map, not a replacement — the Python contains edge cases that are
   deliberately not restated here (see §11 Fidelity rules).
2. **Port the tests first** where the plan says so. `tests/test_slurm_parsing.py`,
   `tests/test_widgets_and_models.py`, `tests/test_filters.py`,
   `tests/test_array_collapse.py`, `tests/test_efficiency.py`,
   `tests/test_pending_reason.py` and `tests/test_account_usage.py` contain the real
   fixture strings from real clusters. They are the specification.
3. **Implement** the Rust.
4. **Verify**: `cargo test`, then `cargo clippy -- -D warnings`, then the phase's
   acceptance criteria.
5. **Iterate**: if a ported test fails, re-read the Python function line by line
   before changing the test. The Python is the source of truth.

**Never** delete or weaken a ported test to make it pass.

### Progress tracking

Keep a `PORT_STATUS.md` at the repo root with one line per work item
(`P2.3 parse_sinfo — done, 6/6 tests green`). Update it as you go; it is how a
resumed session knows where it stopped.

---

## 1. Goal and scope

### Deliverable

A single static-ish Rust binary, distributed as a Python wheel so HPC users can
`pip install` it without a Rust toolchain and without a working Python TUI stack.

### Non-goals

- No Python interop at runtime. The wheel is a **delivery vehicle** for a binary,
  not a Python library. (§3 explains the one optional exception.)
- No Windows support. Linux is the target; macOS is a nice-to-have that should
  compile but is untested against a real cluster.
- No new features. Parity first. Feature work happens after parity is proven.

### Parity target

Everything in the current `README.md` feature list:

| Area | Python source | Status gate |
|---|---|---|
| Job tables (active + terminated) | `widgets/job_table.py` | P4 |
| Job detail: stdout/stderr/cpu/gpu/stats | `widgets/detail_view.py` | P5 |
| Metadata: resources/submission/pending/raw | `widgets/metadata_view.py` | P5 |
| Partition + node monitor | `widgets/partition_view.py`, `app.py` screens | P6 |
| Account usage + fairshare | `widgets/usage_view.py`, `app.py:UsageScreen` | P6 |
| Filtering / bookmarks / multi-select | `widgets/job_table.py`, `app.py` | P4 |
| Array collapse/expand | `widgets/job_table.py` | P4 |
| Cancel / force-cancel / edit / resubmit | `slurm.py`, `app.py` | P7 |
| Batch-script archive + view | `slurm.py`, `config.py` | P7 |
| Editor / pager / SSH suspend-and-shell-out | `app.py` | P7 |
| Remote mode over one SSH session (2FA) | `ssh.py` | P8 |
| Config file + live reload | `config.py`, `app.py:_reload_config` | P3, P7 |
| Help screen driven by binding data | `help.py` | P5 |

---

## 2. Target architecture

### 2.1 Crate layout

A single binary crate. A workspace adds friction for no benefit at this size.

```
lazyslurm-rs/
├── Cargo.toml
├── pyproject.toml            # maturin — see §3
├── README.md
├── src/
│   ├── main.rs               # CLI parse → Config → run TUI
│   ├── config/
│   │   ├── mod.rs            # Config struct, CLI/file/default resolution
│   │   ├── file.rs           # ~/.config/lazyslurm/config.toml load+save
│   │   ├── log_cache.rs      # log_cache.json
│   │   └── script_cache.rs   # archived sbatch scripts
│   ├── model/
│   │   ├── mod.rs            # RunningJob, CompletedJob, PartitionInfo, ...
│   │   ├── array.rs          # array range parsing, task counts
│   │   ├── efficiency.rs     # Efficiency, compute_efficiency, sizing_hint
│   │   └── format.rs         # format_bytes, format_duration, parse_* helpers
│   ├── slurm/
│   │   ├── mod.rs            # public async API
│   │   ├── transport.rs      # run_cmd: local exec vs remote session
│   │   ├── parse.rs          # ALL pure parsers (heavily unit-tested)
│   │   ├── jobs.rs           # squeue / sacct / scontrol
│   │   ├── partitions.rs     # sinfo
│   │   ├── stats.rs          # sstat / sacct stats
│   │   ├── accounting.rs     # sreport / sshare / sprio
│   │   └── actions.rs        # scancel / scontrol update / sbatch
│   ├── ssh/
│   │   ├── mod.rs            # SshSession
│   │   └── pty.rs            # pty spawn + prompt pump
│   ├── ui/
│   │   ├── mod.rs            # App state, event loop, screen stack
│   │   ├── theme.rs          # every Style constant (replaces the .tcss)
│   │   ├── layout.rs         # the fr-ratio layout helpers
│   │   ├── widgets/
│   │   │   ├── job_table.rs
│   │   │   ├── detail_view.rs
│   │   │   ├── metadata_view.rs
│   │   │   ├── partition_view.rs
│   │   │   ├── usage_view.rs
│   │   │   ├── log_pane.rs   # RichLog replacement
│   │   │   ├── input.rs      # single-line text input
│   │   │   └── tabs.rs
│   │   └── screens/
│   │       ├── main.rs
│   │       ├── partitions.rs
│   │       ├── nodes.rs
│   │       ├── usage.rs
│   │       └── modals.rs     # help, confirm, edit, ssh-prompt
│   └── help.rs               # binding data + renderer
└── tests/
    └── fixtures/             # verbatim command output from the Python tests
```

### 2.2 Dependencies

```toml
[dependencies]
ratatui       = "0.29"          # TUI framework
crossterm     = "0.28"          # terminal backend, events, raw mode, suspend
tokio         = { version = "1", features = ["rt-multi-thread", "macros", "process", "time", "sync", "io-util", "fs"] }
futures       = "0.3"           # join! / select! over the async slurm calls
clap          = { version = "4", features = ["derive"] }
serde         = { version = "1", features = ["derive"] }
serde_json    = "1"             # log_cache.json
toml          = "0.8"           # config.toml read
toml_edit     = "0.22"          # config.toml write, preserving comments
chrono        = "0.4"           # date math for sacct --starttime, sreport windows
regex         = "1"             # ssh prompt matching, a few parsers
shell-words   = "1"             # shlex.split / shlex.quote equivalents
directories   = "5"             # XDG config dir
anyhow        = "1"
thiserror     = "2"
nix           = { version = "0.29", features = ["term", "process", "fs"] }   # openpty
unicode-width = "0.2"           # correct truncation of job names
sha2          = "0.10"          # ssh control-path hash

[dev-dependencies]
rstest        = "0.23"
insta         = "1"             # snapshot tests for rendered buffers
pretty_assertions = "1"
```

Notes:
- **`toml_edit` for writes.** The Python `save()` regenerates the file and destroys
  the user's comments. Writing with `toml_edit` is a strict improvement and costs
  nothing; the shipped `templ/config.toml` is almost entirely comments.
- **`nix` over `portable-pty`.** Only `openpty` + `read`/`write` on a raw fd are
  needed; `portable-pty` pulls in a lot for that. If pty handling turns painful,
  switching is a contained change inside `ssh/pty.rs`.
- **No async-trait, no tower, no heavier runtime.** Everything is
  "spawn a process, read its stdout".

### 2.3 Concurrency model

Textual runs an asyncio loop and awaits Slurm calls inside message handlers. In
Rust:

- `tokio` multi-threaded runtime owns everything.
- One **input task** reads `crossterm::event::EventStream` and sends
  `Event::Key(..)`/`Event::Resize(..)` into an `mpsc` channel.
- One **tick task** fires `Event::Tick` every `config.refresh` seconds (skip
  entirely when `refresh == 0`).
- **Data tasks** are `tokio::spawn`ed for each poll/detail-load and send
  `Event::Data(DataMsg)` back on the same channel. `DataMsg` carries fully-parsed
  models — no rendering happens off the main task.
- The **main loop** is `while let Some(ev) = rx.recv().await { app.handle(ev); if
  app.dirty { terminal.draw(...) } }`. All mutation is single-threaded, so no
  locks on app state.

**Debouncing and cancellation** (Python uses `run_worker(exclusive=True)` and a
200 ms `set_timer`): give each detail load a monotonically increasing
`generation: u64`. On arrival, drop the message if `msg.generation !=
app.detail_generation`. For the 200 ms debounce, store `pending_selection:
Option<(String, Instant)>` and fire the load from the tick handler once it has
aged. Do not attempt to abort in-flight tasks — just ignore stale results, which
is what the Python effectively does.

---

## 3. Packaging: the PyPI wheel

This is the part that must be designed in from the start, not bolted on.

### 3.1 Approach

Use **maturin with `bindings = "bin"`**. It compiles the Rust binary and packages
it as a wheel whose `[project.scripts]`-equivalent entry lands the binary directly
in `<venv>/bin/`. No PyO3, no Python source, no import machinery.

`pyproject.toml`:

```toml
[build-system]
requires = ["maturin>=1.7,<2.0"]
build-backend = "maturin"

[project]
name = "lazyslurm-rs"
description = "A TUI for monitoring Slurm HPC jobs (Rust implementation)"
readme = "README.md"
license = { file = "LICENSE" }
requires-python = ">=3.8"
classifiers = [
    "Environment :: Console",
    "Intended Audience :: Science/Research",
    "License :: OSI Approved :: MIT License",
    "Operating System :: POSIX :: Linux",
    "Programming Language :: Rust",
    "Topic :: System :: Monitoring",
]
dynamic = ["version"]            # taken from Cargo.toml

[tool.maturin]
bindings = "bin"
strip = true
```

`requires-python = ">=3.8"` is deliberately loose: the wheel contains no Python
code, so any interpreter that can install it can run it. The binary itself has no
Python dependency at all.

### 3.2 Distribution name

The existing PyPI project is `lazyslurm-py` with command `lazyslurm`. Publish the
Rust one as **`lazyslurm-rs`**, same command name.

> **Conflict warning to document in the README:** installing both
> `lazyslurm-py` and `lazyslurm-rs` into the same environment means two packages
> own `bin/lazyslurm`, and whichever installs last wins. Mention this explicitly.

### 3.3 Wheel targets

Build **manylinux** wheels via `maturin` in the `ghcr.io/pyo3/maturin` container,
using `maturin generate-ci github` as the starting point for the workflow.

- Primary: `x86_64-unknown-linux-gnu` on `manylinux2014` (glibc 2.17) — old enough
  for essentially every HPC login node in service.
- Secondary: `aarch64-unknown-linux-gnu`.
- Optional: `x86_64-apple-darwin` + `aarch64-apple-darwin` for laptops running
  `--remote`.
- **musl:** skip. It buys little here and complicates the pty/nix build.

Always also publish the **sdist**. Anyone on an unusual platform can build it,
provided they have a Rust toolchain — say so in the README, because the failure
mode ("cargo not found" during pip install) is otherwise baffling.

### 3.4 The optional Python shim

If `python -m lazyslurm` must keep working, add a tiny pure-Python package
alongside (`bindings = "bin"` supports shipping extra Python source via
`tool.maturin.python-source`) whose `__main__.py` does
`os.execv(<binary path>, sys.argv)`. Treat this as **P9, optional**. Do not let it
influence the core design.

### 3.5 Version single-sourcing

`Cargo.toml`'s `version` is the single source of truth; `dynamic = ["version"]`
picks it up. Mirror the Python project's discipline of not letting the two drift.

---

## 4. Phases

Each phase is independently verifiable. Do not start a phase before its
predecessor's acceptance criteria pass.

---

### P0 — Skeleton and packaging (do this first)

**Why first:** proving the wheel builds and installs before there is anything to
package removes the highest-risk unknown while the fix is cheap.

**Build:**
- `cargo new`, the `Cargo.toml` + `pyproject.toml` from §2.2/§3.1.
- `main.rs` that prints the version and exits.
- CI workflow from `maturin generate-ci github`, building manylinux wheels + sdist.

**Acceptance:**
- `maturin build --release` produces a wheel.
- `pip install <wheel>` into a clean venv, then `lazyslurm --version` works.
- CI is green on a pushed branch.

---

### P1 — Models and pure formatting

**Read:** `src/lazyslurm/models.py` (all 681 lines).
**Port tests from:** `tests/test_widgets_and_models.py`, `tests/test_efficiency.py`.

**Build** `src/model/`:

| Python | Rust | Notes |
|---|---|---|
| `Config` | `struct Config` | `Option<Vec<String>>` for `partition_order`, `HashMap<String,String>` for colors, `Option<u32>` for `cache_max_age_days` |
| `RunningJob`, `CompletedJob`, `PartitionJob` | plain structs | all-`String` fields; keep Slurm's raw text |
| `PartitionInfo`, `NodeInfo` | structs + methods | `nodes_aiot()`, `cpus_aiot()`, `load()`, `mem_used()`, `gpus_total/used/free()` as methods |
| `PriorityInfo` | struct | `factors() -> Vec<(&str, i64)>` sorted descending, `ahead()` |
| `UsageRow`, `FairShare` | structs | `is_account_total()`, `share_ratio() -> Option<f64>`, `reading() -> String` |
| `JobDetail` | struct with `raw: BTreeMap<String,String>` | **`BTreeMap`, not `HashMap`** — the Raw metadata tab renders sorted |
| `JobStats` | struct | `gpu_count()`, `efficiency()` |
| `Efficiency`, `compute_efficiency`, `sizing_hint` | `efficiency.rs` | `Option<f64>` maps `None` exactly |
| `parse_mem_bytes`, `parse_duration`, `parse_req_mem` | `format.rs` → `Option<f64>` | |
| `format_bytes`, `format_duration`, `gres_count` | `format.rs` | |
| `_array_ranges`, `array_task_count`, `array_index_span` | `array.rs` | |

**Traps that will bite you:**
- `parse_duration` handles `1-04:09:36`, `06:31:12`, `00:43.900`, `43.9`, and returns
  `None` for `UNLIMITED` / `Partition_Limit` / `INVALID`. Parse as `f64`, not `u64` —
  the milliseconds form is real.
- `_array_ranges` must handle `%` (throttle, terminates the spec) and `:` (stride).
  `123_[1-4%10]` is 4 tasks; `123_[0-9:2]` is 5.
- `format_bytes` switches format at 10 units: `2.6G` but `512M` (one decimal below
  10, none at or above).
- `parse_req_mem`'s trailing `n`/`c` markers multiply by nodes/CPUs; an *unmarked*
  value is already the total and must **not** be multiplied.
- `sizing_hint` rounds memory up to whole GiB, or to 256 MB steps below 1 GiB.

**Acceptance:** every ported case from `test_efficiency.py` and the model half of
`test_widgets_and_models.py` passes.

---

### P2 — Slurm command layer

**Read:** `src/lazyslurm/slurm.py` (all 1684 lines).
**Port tests from:** `tests/test_slurm_parsing.py` (851 lines — the single most
valuable test file in the repo), plus `tests/test_pending_reason.py` and
`tests/test_account_usage.py`.

**Build** `src/slurm/`. Split strictly:

- `parse.rs` holds **pure functions** `&str -> Vec<T>`. No I/O. These get the
  exhaustive unit tests.
- `jobs.rs`/`partitions.rs`/… build argv, call `transport::run_cmd`, and hand the
  output to `parse.rs`.

**Exact command formats — copy verbatim, do not "improve":**

| Function | Command |
|---|---|
| `get_running_jobs` | `squeue -u <user> --format=%i\|%j\|%M\|%P\|%T\|%l\|%D\|%C\|%m\|%b\|%Z --noheader --sort=-i [-p <part>]` |
| `get_completed_jobs` | `sacct -u <user> --format=JobID,JobName,State,ExitCode,Start,End,Elapsed,Partition --starttime=<YYYY-MM-DDT00:00:00> --noheader --parsable2` |
| `get_job_detail` | `scontrol show job <id>` → fallback `sacct -j <id> --format=<20 fields> --noheader --parsable2` |
| `get_job_stats` | `sstat -j <id>.batch --format=<12 fields> --noheader --parsable2` **and** `sacct -j <id> --format=JobID,TotalCPU,Elapsed,ReqMem,AllocTRES,ReqTRES,AllocCPUS,NNodes,NTasks,Timelimit,MaxRSS --noheader --parsable2`, joined |
| `get_partitions` | `sinfo --noheader --summarize --format=%P\|%a\|%F\|%C\|%l\|%G` + `squeue --noheader --format=%P\|%T --states=RUNNING,PENDING` |
| `get_partition_nodes` | `sinfo -N -p <part> --noheader -O NodeHost:\|,StateLong:\|,CPUsState:\|,Memory:\|,FreeMem:\|,CPUsLoad:\|,Gres:\|,GresUsed:\|,Reason:\|` → fallback `--format=%N\|%T\|%C\|%m\|%e\|%O\|%G\|\|%E` |
| `get_partition_jobs` / `get_node_jobs` | `squeue -p <part>` (or `-w <node>`) `--noheader --format=%i\|%u\|%j\|%T\|%M\|%l\|%D\|%C\|%b\|%R --states=...` |
| `get_account_usage` | `sreport cluster AccountUtilizationByUser start=<d> end=now -t hours -P --noheader [account=<a>]` |
| `get_fairshare` | `sshare -P -o Account,User,RawShares,NormShares,RawUsage,EffectvUsage,FairShare {-u <user>\|-U}` |
| `get_job_priority` | `sprio --noheader --format=%i\|%Y\|%A\|%F\|%J\|%P\|%Q [-p <part>]` |
| `cancel_job` | `scancel [--signal=KILL] <id>` |
| `update_job` | `scontrol update jobid=<id> <Key=Value>...` |
| `resubmit_job` | `sbatch [--chdir <wd>] <tokens...>` |
| `get_batch_script` | `scontrol write batch_script <id> -` |

**Parser-by-parser traps** (each has tests — read them):

- **`job_sort_key`** — sorts on `(base_id, -task_index)` and is used with
  `reverse=True`, so array tasks read ascending inside a descending job list.
  Splits on the first of `_`, `+`, `.`. Unparseable ids sort last (`-1`).
  In Rust: return `(i64, i64)` and `sort_by(|a, b| b.key().cmp(&a.key()))`.
- **`_parse_scontrol`** — `SubmitLine=` swallows the rest of its line (it contains
  spaces); tokens *before* it on the same line still parse normally. Getting this
  wrong silently breaks resubmission.
- **`parse_sinfo`** — `--summarize` still emits one row per node *configuration*, so
  rows must be **summed per partition name**. Strip the trailing `*` from the
  default partition. Trailing fields are optional (short `%P|%a|%F` form must work).
- **`parse_sreport`** — rows are recognised **by content**, not position: skip lines
  starting with `-`, skip a first field of `cluster`/`cluster/account/user`, and
  require field 4 to parse as a float (strip thousands commas).
- **`parse_sprio`** — asks for a whole partition, then derives both the job's factor
  breakdown *and* its rank (`count(total > mine) + 1`) and `queued` from the same
  output. Parse values as `int(float(x))`.
- **`parse_sacct_stats`** — folds the job row (which has `ReqMem`/`Timelimit`/
  `AllocCPUS` but no `MaxRSS`) together with step rows (which have `MaxRSS` but
  nothing else), taking the **peak** `MaxRSS` across all steps. This is what makes
  the numbers match `seff`.
- **`parse_partition_job_counts`** — a pending job listing `gpu,gpu-long` counts
  toward **both**.
- **`explain_reason`** — exact-match table first, then prefix table (order matters:
  `AssocMaxJobs` before `AssocMax`), then `"Slurm says: <code>"`. Strips anything
  from `(` onward. Substitutes the queue-position count for `Priority` and the
  dependency string for `Dependency*`.
- **`_first_node`** — `node[001-003]` → `node001`, `gpu[01-04],gpu[10]` → `gpu01`.
- **`tail_file`** — seeks backwards in 64 KB blocks, caps at 4 MB, drops the first
  (partial) line unless it reached the file start, and prepends a `... (truncated:
  ...)` banner when it hit the byte cap without finding a newline. This exists
  because a single 200 MB progress-bar "line" is a real thing in ML logs.
- **`normalize_memory`** — `40G` → `40960`, strips trailing `n`/`c`, passes
  unparseable values through untouched so Slurm reports the error.
- **`_script_token_index`** — the script is the last bare token, skipping values of
  separate-form flags (`--array 1-4` consumes the next token; `--array=1-4` does
  not).
- **Availability latches** — `_accounting_missing` and `_sprio_missing` are set once
  and change the UI's *message*. In Rust these become `AtomicBool` or fields on a
  shared state struct, not globals.

**Transport** (`transport.rs`): one function

```rust
async fn run_cmd(args: &[&str]) -> (String, String, i32)
```

Local mode: `tokio::process::Command`. Remote mode: quote the argv into a shell
line and hand it to the SSH session (P8). Until P8 lands, remote returns an error —
that is fine and keeps P2 unblocked.

**Acceptance:** all of `test_slurm_parsing.py`, `test_pending_reason.py` and
`test_account_usage.py` ported and green. Add a `--dump` debug subcommand that
prints parsed structs for a given command, so you can point it at a real cluster.

---

### P3 — Config, caches, CLI

**Read:** `src/lazyslurm/config.py`, `src/lazyslurm/__main__.py`,
`src/lazyslurm/templ/config.toml`.

**Build:**
- `config/file.rs`: load/save `~/.config/lazyslurm/config.toml`, honouring
  `XDG_CONFIG_HOME`. Use `directories`. Read with `toml`, write with `toml_edit`.
  A malformed file must yield defaults, never a crash (the Python swallows all
  exceptions here on purpose).
- `config/log_cache.rs`: `log_cache.json`, entries
  `{stdout, stderr, command, submit_line, workdir, ts}`. Atomic write via
  temp-file + rename. `get_cached_command` prefers `submit_line` over `command`.
  `prune_log_cache` drops entries older than `cache_max_age_days`.
- `config/script_cache.rs`: `<dir>/<base_job_id>.sh`, mode `0600`, directory `0700`,
  temp+rename, **non-empty check** on read (a truncated write must not open as a
  blank buffer). `base_job_id` splits on `_`, `+`, `.` and returns `""` when what
  remains isn't all digits — that guard is what keeps arbitrary text out of cache
  filenames, so keep it.
- `main.rs`: clap derive mirroring the argparse flags exactly —
  `-r/--refresh` (accepts `off`/`none`/`null`/`0`), `-d/--days`, `-u/--user`,
  `-p/--partition`, `--no-gpu`, `--no-live`, `--partition-order` (comma list,
  **persisted to the config file when given**), `-H/--remote`.
- Resolution order **CLI > config file > hard default**, and collect an
  `overrides: Vec<String>` describing each CLI-beats-file case — the app logs
  these into the Command Log at startup.
- `--remote user@host` with no `--user` derives the user from the `user@` prefix.
- Ship `templ/config.toml` via `include_str!` and write it out on first
  `,` (edit-config).

**Acceptance:** round-trip tests for each cache; a resolution test table covering
CLI-only, file-only, both, and neither.

---

### P4 — The main screen: job tables

The first phase with pixels. Budget the most time here — this is where the
Textual→ratatui architecture change actually costs.

**Read:** `src/lazyslurm/widgets/job_table.py`, `src/lazyslurm/lazyslurm.tcss`,
`app.py:compose/on_mount/_poll_jobs/on_key`.
**Port tests from:** `tests/test_filters.py`, `tests/test_array_collapse.py`.

#### 4.1 Layout

Reproduce the `.tcss` ratios with `Constraint::Ratio`:

```
cluster-bar            Length(1)      docked top
main                   Min(0)
├── left   1fr   ┐ Ratio(1, 3)
│   ├── search-input   Length(1)  (only when visible)
│   ├── active-jobs    Ratio(1,2)
│   └── completed-jobs Ratio(1,2)
└── right  2fr   ┘ Ratio(2, 3)
    ├── detail-view    Ratio(2,3)
    ├── metadata-view  Ratio(1,3)
    └── command-log    Length(3..6)   min 3, max 6, sized to content
footer                 Length(1)      docked bottom
```

Borders: `Borders::ALL` with `BorderType::Rounded`, title centered, dim when
unfocused and accent-colored when focused — that is the entire meaning of the
`:focus-within` rules in the `.tcss`. Put both styles in `ui/theme.rs`; do not
scatter `Style::default().fg(...)` through the widgets.

#### 4.2 Job table state

`DataTable` becomes a struct you own:

```rust
pub struct JobTable<T> {
    all_jobs: Vec<T>,
    rows: Vec<Row>,             // derived: filtered + grouped + ordered
    state: TableState,          // ratatui cursor + scroll offset
    filter: String,
    terms: Vec<Term>,
    bookmarked: HashSet<String>,
    multiselected: HashSet<String>,
    expanded: HashSet<String>,  // base ids the user opened
    groups: HashMap<String, Vec<usize>>,  // base id -> indices into all_jobs
    focused: bool,
}
```

`rebuild()` mirrors `_BaseJobTable._rebuild` exactly:
1. filter by `terms` (AND across terms)
2. group arrays by `base_job_id` when `collapse_arrays` (preserving encounter order)
3. groups of size 1 render as ordinary rows — including a pending `123_[12-40]`,
   which squeue already gives you as one row
4. drop expanded-state for arrays that no longer exist (`expanded &= groups`)
5. pinned (bookmarked) groups first, others after, **stable** within each
6. expanded groups emit a header row then member rows prefixed `├ ` / `└ `
7. empty result + active filter → a dim `no jobs match` placeholder row that must
   never be selectable as a job id
8. border title gains `— <matched>/<total> match` while a filter is active

**Cursor preservation:** the Python's `_apply_diff` keeps the cursor on the same
row key across polls, and only rebuilds when keys or order change. In an
immediate-mode world you don't need the diff — but you **do** need the behaviour:
before rebuild, remember the selected row key; after rebuild, find it and restore
the index. Falling back to clamping the index is what makes a table "jump" on
refresh, which is the single most noticeable regression a port can introduce.

#### 4.3 Filter language

Port `parse_query` / `_match_term` verbatim. `test_filters.py` is the spec.

- Tokens split with shell-words; on an unbalanced quote (mid-typing) fall back to
  whitespace splitting.
- Keys and aliases: `state|st|s`, `part|partition|p`, `name|n`, `id|job`,
  `gpu|gpus|gres`. An **unknown** `key:` is treated as plain substring text — this
  guarantee ("nothing you type can break the filter") is load-bearing.
- Comparisons `>=`, `<=`, `!=`, `>`, `<`, `=` (check two-char ones first).
- `state:` is a **prefix** match (`pend` finds `PENDING`); `partition`/`name`/`id`
  are substring; bare terms search id + name + partition (+ state for the
  terminated table); `gpu:` compares `gres_count` numerically and matches nothing
  on sacct rows, which carry no GRES.

#### 4.4 Rendering details

- Truncation with `…`, widths from `max_name_width`/`max_partition_width`
  (`0` = unlimited). Use `unicode-width`, not `len()`.
- Markers prefix the **Name** cell: `◉ ` multi-selected, `★ ` bookmarked, in that
  order, before the tree indent.
- Partition colors: custom map first, else `sum(bytes) % 9` into the palette
  `[cyan, magenta, yellow, green, blue, red, bright_cyan, bright_magenta,
  bright_green]`. Keep the hash identical or colors shift between the two
  implementations.
- State colors: active table colors the **Job ID** cell; terminated table colors
  the **State** cell. Abbreviations only when `abbreviate_states`.
- Collapsed array label: `▸ 123_[0-11] ×12` (`▾` when expanded), and the tally
  (`4run 2pend`) replaces Elapsed in the active table / uses the longest elapsed in
  the terminated table.
- Zebra striping on alternate rows.

#### 4.5 Key handling

- `Up`/`Down` **wrap between the two tables** in both directions (`app.py:on_key`) —
  down past the last active row focuses the completed table at row 0, and so on.
- `Enter` on a collapsed array row toggles expansion.
- `/` toggles the filter bar; `Esc` inside it clears the filter, hides it, and
  returns focus to the active table.
- `m` bookmarks (the array base id when on a collapsed row, so the group pins as a
  unit); `Ctrl+V` enters multi-select with the cursor as anchor, `Up`/`Down`
  extends the range over the **displayed order**, `Ctrl+V` again exits.
- `q` quits.

#### 4.6 Polling

`Event::Tick` → spawn a task running `get_running_jobs`, `get_completed_jobs` and
`get_partition_availability` concurrently (`tokio::join!`), send one `DataMsg::Poll`.
On receipt: update both tables, rebuild the cluster bar via `format_cluster_summary`
(counting array *tasks*, not rows), and diff `known_running_ids` against the new set
to emit completion notifications (bell + `notify-send`, both best-effort).

**Acceptance:** run against a real cluster (or the `--dump` fixtures). Filtering,
array expand/collapse, bookmarks and multi-select behave identically to the Python
side by side.

---

### P5 — Right column: detail, metadata, help

**Read:** `widgets/detail_view.py`, `widgets/metadata_view.py`, `help.py`,
`app.py:_load_job_details`.
**Port tests from:** the widget half of `test_widgets_and_models.py`,
`tests/test_help.py`.

#### 5.1 Tabs

Write one small `tabs.rs`: a `Vec<&str>`, an active index, `[`/`]` and `(`/`)`
cycling, rendered as a single line of `Tab` labels (active bold + normal fg,
inactive dim). The `.tcss` sets `Underline { height: 0 }`, i.e. no underline bar —
match that.

#### 5.2 Detail view

Tabs: `stdout`, `stderr`, `cpu`, `gpu` (omitted when `no_gpu`/`no_live`), `stats`.

`RichLog` → a `LogPane` widget: owns `Vec<Line>`, a scroll offset, wraps to width,
supports PageUp/PageDown/Home/End. It never needs to be editable or searchable —
`l` shells out to the pager for that.

The **stats** tab is generated text; port `_efficiency_section` and `load_stats`
literally, including:
- `▆▆▆▁▁▁▁▁` eighth-block gauges, 8 wide
- coloring thresholds: `>= 1.0` red-bold ("at the limit"), `>= 0.60` green,
  `>= 0.25` yellow, below red
- `<1%` rather than rounding a fraction of a percent up to `1%`
- `<0.1` for tiny CPU-core-equivalents
- the `← over-requested` / `← at the limit, risks OOM` annotations
- `/node` suffix on the memory request for multi-node jobs
- the GPU line's "utilisation is not recorded by Slurm" note
- the sizing hint line
- sparklines from the sampled history (`▁▂▃▄▅▆▇█`, normalised to the max, 60 samples)
- trailing `Source: sstat|sacct|combined`

The Python renders these with Rich markup (`[bold]`, `[dim]`, `[green]`). **Do not
port a markup parser.** Build `ratatui::text::Line`s from styled `Span`s directly.
It is more code but removes a whole class of escaping bugs (`help.py` has a
`_escape` function purely because `[ / ]` is a keybinding that looks like markup).

#### 5.3 Metadata view

Tabs `Resources`, `Submission`, `Pending`, `Raw`. **`Pending` only exists while the
selected job is pending** — it is inserted into and removed from the tab list, and
`(`/`)` must skip it when absent. Port `_pending_report` including the
`priority_bar` (`█░`, 20 wide), the `#rank of N pending in <partition>` line, the
factor table with per-factor percentages, and the two distinct
"unavailable" messages (sprio returned nothing vs sprio doesn't exist).

`Raw` renders `detail.raw` sorted by key — hence `BTreeMap` in P1.

#### 5.4 Help screen

Port `help.py` as **data** (`Key { display, text, keys }`, `Panel { context, title,
subtitle, keys, notes }`), not prose. Then port `tests/test_help.py`'s cross-check:
every key listed in a panel's bindings is documented, and nothing documented has
ceased to exist. That test is why the Python help stopped drifting; the Rust port
inherits the problem and should inherit the guard.

Context selection follows the focused panel/screen exactly as
`app.py:_help_context` does — including the filter bar mapping to the JOBS context.

#### 5.5 Detail loading

Port `_load_job_details`: fetch `get_job_detail`, then concurrently
`read_log_file(stdout)`, `read_log_file(stderr)`, `get_job_stats`, and — **only for
pending jobs** — `get_job_priority`. Live CPU/GPU are deliberately *not* fetched on
selection; they load lazily when their tab is open (§P6.3).

**Acceptance:** stats and pending panels render identically to the Python for the
same job. Help test green.

---

### P6 — Full-screen panels: partitions, nodes, usage

**Read:** `app.py:PartitionScreen/NodeScreen/UsageScreen`, `widgets/partition_view.py`,
`widgets/usage_view.py`.

#### 6.1 Screen stack

```rust
enum Screen { Main, Partitions(PartitionState), Nodes(NodeState), Usage(UsageState) }
struct App { screens: Vec<Screen>, modal: Option<Modal>, ... }
```

Keys route to the modal if present, else the top screen, else the global table.
`Esc`/`q` pops. This replaces Textual's `push_screen`/`pop_screen` and its
`push_screen_wait`; the latter (used for the SSH prompt) becomes a `oneshot`
channel the modal resolves — see P8.

#### 6.2 Tables

`PartitionTable`, `NodeTable`, `PartitionJobTable`, `UsageTable` are each simpler
than the job table: no grouping, no filtering, but the **same cursor-preservation
rule** (keep the cursor on the same partition/node name across refreshes).

Port the renderers precisely:
- `load_bar`: 10-wide `█░`, green `< 0.6`, yellow `< 0.9`, red above, with a
  `nnn%` suffix; 6-wide in the node table.
- Down partitions render `[down]` dim with a struck-through name.
- Node memory cell `  12/64G`, red when `>= 90%` used; drained/down nodes show `—`
  for load, since their counters mean nothing.
- GPU cell `used/total`, green when any free, red when none, `—` when the node has
  no GRES.
- `PartitionJobTable` prefixes **your** jobs with `▸ ` in bold cyan.
- `share_bar` (16-wide) and `format_hours` (thin-space thousands: `68 364`).

Both screens carry their own summary bar and their own refresh interval, and both
show *all users'* jobs — the header says so, keep it.

#### 6.3 Live CPU/GPU

Port `_refresh_live_monitors`: only fetch when the corresponding tab is actually
active. `get_node_processes` shells `ps` over SSH to the compute node;
`get_gpu_status` prefers `srun --overlap --jobid=<id>` (which lands inside the job's
cgroup and so sees only the allocated GPUs) and falls back to SSH + `nvidia-smi`
with an explicit "showing all GPUs" warning. Keep both strategies and the warning.

Note the two distinct SSH option sets in `slurm.py`: `_SSH_OPTS` (local mode,
multiplexed via `~/.ssh/cm-lazyslurm`) and `_NODE_SSH_OPTS` (the login-node→compute
hop in remote mode, `BatchMode=yes` so it fails fast instead of prompting where
nobody can answer).

#### 6.4 Usage screen

Opens **immediately with a placeholder** and fills in when `sreport`/`sshare` land —
`sreport` can take seconds on a busy slurmdbd. `w` cycles
`month → 30d → year`. When both queries come back empty, distinguish "this cluster
has no accounting" from "no data for you in this window" using the accounting latch.

**Acceptance:** all four screens usable against a real cluster; `p`, `Enter`, `U`,
`w`, `r`, `Esc` all behave as documented in `help.py`.

---

### P7 — Actions and shell-outs

**Read:** `app.py` action handlers, `slurm.py` action functions.
**Port tests from:** `tests/test_edit_screen.py`, `tests/test_live_resubmit.py`.

- **Cancel** (`c`) with a confirm modal; **force-cancel** (`Shift+C`) with **no**
  confirmation. On a collapsed array both target the base id, so `scancel 123`
  takes every task. Multi-select applies to all selected ids.
- **Edit** (`u`) — modal styled like a text editor (line numbers, `^S write`,
  `esc quit`), fields from `EDITABLE_FIELDS`
  (`TimeLimit`, `Partition`, `NumNodes`, `NumCPUs`, `MinMemoryNode`). Prefilled for
  a single job, blank for a multi-edit where every non-empty field applies to all.
  **Only pending jobs** — filter the selection and log how many were skipped.
  Up/Down/Tab move between fields.
- **Resubmit** (`s`) — terminated jobs only, confirm modal, prefers `SubmitLine`
  over `Command`. If the original script is gone, fall back to the archived copy —
  and refuse in remote mode, because the archive is local while `sbatch` runs on the
  login node. Keep that refusal explicit.
- **Batch script** (`b`) — `archive_batch_script` then open **read-only**
  (`vim -R`, `nano -v`; unknown editors open writable *with a logged warning*).
  Archiving happens opportunistically on every `scontrol`-sourced detail load,
  because after `MinJobAge` the script is gone for good.
- **Pager** (`l`) / **editor** (`e`, `Shift+E`) — suspend the TUI, run the command,
  resume. In remote mode the pager runs **on the cluster** over the existing control
  socket (never copy a multi-GB log down); the editor scp's the file to a temp file
  over that same socket and deletes it afterwards. Pager flags per binary:
  `less -R +G`, `most +`, `more +G`, `bat --paging=always --style=plain`.
- **SSH to node** (`o`) — suspend, `ssh` (in remote mode via a `ProxyCommand` that
  rides the live master socket, **not** `-J`, which would re-trigger 2FA), resume.
- **Edit config** (`,`) — write the template if absent, open in `$editor`, then
  **live-reload**: rebuild `Config`, re-apply display settings, force a table
  rebuild, and log each changed field. CLI-only values (`remote`, `user`) survive
  the reload.

**Suspending the terminal in ratatui:** leave raw mode, leave the alternate screen,
show the cursor, run the child to completion, then re-enter and force a full
redraw. Wrap this in one helper (`ui::suspend(|| { ... })`) and use it everywhere —
a missed `LeaveAlternateScreen` corrupts the user's terminal, which is the worst
failure mode this app has.

---

### P8 — Remote mode (SSH session)

**Read:** `src/lazyslurm/ssh.py` (all 428 lines) — the module docstring explains the
design and is worth reading first.
**Port tests from:** `tests/test_ssh_session.py` (431 lines; it overrides the channel
argv to run a local `/bin/sh`, which ports cleanly).

The three-step design must be preserved exactly:

1. **Master.** `ssh -N -M -o ControlPath=<hash>.sock -o ControlPersist=no
   -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10
   -o ServerAliveInterval=30 -o ServerAliveCountMax=3 <host>`, spawned **on a pty**.
   The pty is the whole point: ssh writes prompts to `/dev/tty`, so with plain pipes
   the 2FA question never reaches you. With no prompt callback available, add
   `BatchMode=yes` to fail fast instead of hanging; with one, `NumberOfPasswordPrompts=3`.
2. **Auth pump.** Read the pty in a loop; on each chunk, strip `\r` and match the
   prompt regex against the tail. Forward the matched prompt to the UI, write the
   answer + `\n` back to the fd, and **clear the buffer** so the same prompt isn't
   re-matched. Poll `ssh -O check` each iteration — the master coming up is the
   success condition, not EOF. Overall timeout 120 s.
3. **Channel.** `ssh -o ControlPath=... -o ControlMaster=no -o BatchMode=yes <host>
   /bin/sh -s` — multiplexed over the master, so no second authentication. It stays
   open for the app's lifetime.

`run(cmd)` writes into that shell and frames the output with a unique marker:

```sh
<command>
__lzs_rc=$?
printf '%s%s\n' '__LAZYSLURM_<n>__' "$__lzs_rc"
printf '%s\n' '__LAZYSLURM_<n>__' 1>&2
```

then reads stdout and stderr until each sees the marker, taking the exit status
from the stdout marker's suffix. Serialize with a `tokio::sync::Mutex` so commands
queue rather than interleave. On timeout (20 s default) the shell is mid-command and
out of sync — **kill it**; the next `run()` opens a fresh one over the same
connection, re-authenticating only if the master also died.

**Rust specifics:**
- `nix::pty::openpty`, then `tokio::process::Command` with
  `pre_exec`/`setsid` for a new session and the slave fd on all three stdio
  handles. Close the slave in the parent.
- Async reads from the pty master: `tokio::io::unix::AsyncFd`, or a
  `spawn_blocking` read loop feeding a channel. The blocking approach mirrors the
  Python's `run_in_executor` most closely — prefer it if `AsyncFd` fights you.
- Prompt patterns (regex, case-insensitive, multiline, anchored at end): password,
  passphrase, passcode, verification code, one-time password, `\botp\b`,
  token_response, `\bpin\b`, duo, `(yes/no[/[fingerprint]])?`, and a generic
  `^\s*enter .*:\s*$` PAM fallback. `(yes/no` is the **only** non-secret prompt.
- On failure, scan the buffer backwards for a line matching the failure regex
  (permission denied / authentication failed / too many authentication failures /
  no route to host / connection refused|closed|timed out / could not resolve) and
  surface **that** line, not a generic error.
- Control path: `~/.ssh/cm-lazyslurm/<sha256(host)[..16]>.sock`, dir mode `0700`.
  A hash, not the host name, because unix socket paths are length-limited.
- `close()` sends `ssh -O exit` before killing anything.

The UI side: the prompt callback is an `async fn(String, bool) -> Option<String>`
that pushes the modal and awaits a `oneshot::Receiver`. Because the SSH session runs
on a task and the modal lives on the main loop, this crosses tasks — send a
`Event::SshPrompt { text, secret, reply: oneshot::Sender<Option<String>> }` on the
main channel and let the main loop own the modal.

`r` on the main screen retries a failed/dropped session — that is the user's only
way back after cancelling or mistyping a code. Keep it.

**Acceptance:** `test_ssh_session.py` ported and green; a manual connect against a
real 2FA cluster completes, survives ~50 commands, and reconnects after the network
drops.

---

### P9 — Polish and release

- README rewrite: installation (`pip install lazyslurm-rs`), the
  `lazyslurm-py`/`lazyslurm-rs` conflict warning, the sdist-needs-Rust note.
- `--version`, `--help` output parity.
- Optional: the `python -m lazyslurm` shim (§3.4).
- Optional: shell completions via `clap_complete`.
- Tag, build wheels in CI, `maturin upload`.

---

## 5. Testing strategy

The Python has ~3.7k lines of tests. They do **not** port uniformly.

### 5.1 Ports 1:1 — do these

`test_slurm_parsing.py`, `test_efficiency.py`, `test_pending_reason.py`,
`test_account_usage.py`, and the model half of `test_widgets_and_models.py` are pure
functions over fixture strings. Move the fixture strings verbatim into
`tests/fixtures/` and drive them with `rstest` cases. This is roughly 60% of the
test value for 20% of the effort — do it as you write each parser, not afterwards.

### 5.2 Needs rearchitecting — plan for it

`test_filters.py`, `test_array_collapse.py`, `test_edit_screen.py`,
`test_remote_ui.py` and `test_help.py` drive Textual's `Pilot` (press keys, query
live widgets). **ratatui has no equivalent.** The mitigation is structural, and it
must be designed in from P4 rather than retrofitted:

> Keep every state transition in a plain struct with **no rendering and no
> terminal**. `JobTable::rebuild()`, `App::handle_key()`, `parse_query()` and
> friends must be callable and assertable without a `Terminal`.

Then:
- **State tests** (the bulk): construct the struct, feed `KeyEvent`s, assert on
  `rows`, `selected`, `expanded`, `multiselected`. This covers everything
  `test_filters.py` and `test_array_collapse.py` currently check.
- **Render smoke tests** (a handful): `ratatui::backend::TestBackend` + `insta`
  snapshots of the rendered buffer, for the main screen and each full-screen panel.
  These catch layout regressions, not logic.

### 5.3 Manual verification

Keep a `MANUAL_TESTS.md` checklist for what no test can cover: 2FA login, suspend
into `less`/`vim` and return cleanly, `notify-send` on completion, terminal resize,
and behaviour on an 80-column terminal.

---

## 6. Behaviour that is easy to lose

A checklist to run before declaring parity. Each item is a deliberate decision in
the Python that a naive port drops silently.

- [ ] Array tasks sort ascending *within* a descending job list (`job_sort_key`).
- [ ] A pending `123_[12-40]` row counts as 29 jobs in the cluster bar, not 1.
- [ ] Table cursor stays on the same job across a refresh.
- [ ] Expanded arrays stay expanded across a refresh.
- [ ] `Up`/`Down` wrap between the two job tables in both directions.
- [ ] `SubmitLine` survives `_parse_scontrol` with its spaces intact.
- [ ] Batch scripts are archived opportunistically while the job is still live.
- [ ] `scontrol write batch_script` exits 0 even on failure — only non-empty stdout
      counts as success.
- [ ] `tail_file` never reads a whole 200 MB log.
- [ ] The efficiency memory ratio is against the request **per node**.
- [ ] `MaxRSS` is the peak across sacct step rows, not `.batch` alone.
- [ ] Remote mode never opens a second SSH connection (2FA would re-trigger).
- [ ] The remote pager runs on the cluster; the remote editor scp's and cleans up.
- [ ] Login-node warning fires on both local hostname and remote host.
- [ ] Config live-reload preserves CLI-only values.
- [ ] The `no jobs match` placeholder is never selectable as a job.
- [ ] Unknown `key:` filter terms degrade to plain substring search.
- [ ] Force-cancel has no confirmation; ordinary cancel always does.
- [ ] Only pending jobs are editable; non-pending selections are skipped and logged.

---

## 7. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| pty + async in Rust fights you | P8 slips | Use `spawn_blocking` reads (mirrors the Python); keep pty code isolated in `ssh/pty.rs` behind a trait so it can be swapped for `portable-pty` |
| Terminal left corrupted after suspend | Worst UX failure | One `ui::suspend()` helper, used everywhere; test it manually early in P7 |
| UI logic entangled with rendering | Untestable, P4–P6 slow to a crawl | Enforce §5.2's rule from the first line of P4 |
| manylinux build breaks on `nix` | No wheels | P0 proves the build before any pty code exists; keep the glibc target at 2.17 |
| Silent parser divergence | Wrong numbers, no error | Port the fixture tests *with* each parser, never after |
| Scope creep into new features | Never reaches parity | Feature freeze until P9 |

---

## 8. Effort estimate

For one developer comfortable in Rust, assuming the Python stays the reference:

| Phase | Estimate |
|---|---|
| P0 skeleton + packaging | 0.5 day |
| P1 models | 1–2 days |
| P2 slurm layer | 3–5 days |
| P3 config + CLI | 1–2 days |
| P4 job tables | 4–7 days |
| P5 detail/metadata/help | 3–4 days |
| P6 partition/node/usage | 3–4 days |
| P7 actions + shell-outs | 2–3 days |
| P8 remote SSH | 3–5 days |
| P9 polish + release | 1–2 days |

**Total: ~4–7 weeks part-time.** A usable local-only subset (P0–P4) lands in
roughly 1.5–2 weeks and is already worth running.

Expect **9–13k lines of Rust** against 5.7k lines of Python — the growth is almost
entirely in `ui/` (the 288-line stylesheet and Textual's widget behaviour become
explicit code) and, to a lesser degree, `ssh/`.

---

## 9. Milestone definition of done

**Parity release (0.1.0)** ships when:

1. Every box in §6 is ticked.
2. `cargo test` green; `cargo clippy -- -D warnings` clean.
3. `MANUAL_TESTS.md` fully walked on a real cluster, local **and** `--remote`.
4. `pip install lazyslurm-rs` from TestPyPI works on a clean manylinux container
   and on an actual HPC login node.
5. README documents installation, the name conflict, and the sdist toolchain
   requirement.

---

## 10. Reference: Python source map

| File | Lines | Port target | Phase |
|---|---|---|---|
| `models.py` | 681 | `src/model/` | P1 |
| `slurm.py` | 1684 | `src/slurm/` | P2 |
| `config.py` | 275 | `src/config/` | P3 |
| `__main__.py` | 195 | `src/main.rs` | P3 |
| `widgets/job_table.py` | 643 | `src/ui/widgets/job_table.rs` | P4 |
| `lazyslurm.tcss` | 288 | `src/ui/theme.rs`, `src/ui/layout.rs` | P4 |
| `widgets/detail_view.py` | 270 | `src/ui/widgets/detail_view.rs` | P5 |
| `widgets/metadata_view.py` | 190 | `src/ui/widgets/metadata_view.rs` | P5 |
| `help.py` | 218 | `src/help.rs` | P5 |
| `widgets/partition_view.py` | 252 | `src/ui/widgets/partition_view.rs` | P6 |
| `widgets/usage_view.py` | 73 | `src/ui/widgets/usage_view.rs` | P6 |
| `app.py` | 1781 | `src/ui/` (screens, modals, event loop) | P4–P7 |
| `ssh.py` | 428 | `src/ssh/` | P8 |

---

## 11. Fidelity rules

1. **The Python is the specification.** Where this plan and the source disagree,
   the source wins — report the discrepancy, don't silently pick one.
2. **Do not "improve" Slurm command strings, formats, or parsing heuristics.** Every
   odd-looking one is a workaround for a real cluster. The comments say which.
3. **Do not change user-visible strings** during the port. Identical output is what
   makes side-by-side comparison a usable verification method. Improve wording after
   parity, as its own change.
4. **Preserve the comments' intent.** The Python's comments explain *why*, which is
   the expensive knowledge here. Carry the reasoning into the Rust, in your own
   words — don't paste, don't drop.
5. **When a behaviour looks like a bug**, write it down in a `DIVERGENCES.md` and
   port it faithfully anyway. Fix it in both implementations afterwards, deliberately.
