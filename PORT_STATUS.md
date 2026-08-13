# Rust port — status

Tracks progress against [RUST_PORT_PLAN.md](RUST_PORT_PLAN.md). One line per work
item; update as you go, so a resumed session knows where it stopped.

**Branch:** `rust-dev` · **Tests:** 582 passing · **Last updated:** 2026-08-13

`lazyslurm` now starts, draws the main screen and quits. Verified end to end in
a pty as well as by full-screen `TestBackend` draws.

## Layout

| Path | What it is |
|---|---|
| `src/` | The Rust implementation — the real source from here on |
| `reference/python/` | The Python implementation, kept for lookup only |
| `assets/config.toml` | The commented config template, shipped in the binary |
| `Cargo.toml` | Crate manifest; single source of the version |
| `pyproject.toml` | maturin packaging, so the binary ships as a wheel |

`reference/python/` is **reference material**, not a build target. It still runs
(`cd reference/python && uv pip install -e .`) which is useful for side-by-side
comparison against a real cluster.

## Phases

| Phase | Scope | Status |
|---|---|---|
| P0 | Skeleton, Cargo + maturin packaging, CI | **done** |
| P1 | Models, formatting, efficiency | **done** |
| P2 | Slurm command layer: parsers, transport, queries, actions | **done** |
| P3 | Config file, on-disk caches, CLI resolution | **done** |
| P4 | Job tables, filtering, arrays, main screen | **done** |
| P5 | Detail / metadata / help panels | **done** |
| P6 | Partition, node and usage screens | **done** |
| P7 | Actions wired to the UI, editor/pager shell-outs | **done** |
| P8 | Remote mode over one SSH session | **done** |
| P9 | Polish and release | not started |

## Toolchain

`rust-toolchain.toml` pins stable with `rustfmt` and `clippy`. Local checks:

```sh
cargo fmt --all && cargo clippy --all-targets -- -D warnings && cargo test
```

CI runs the same three, plus wheel and sdist builds.

## Done in detail

### P0 — packaging proven first
- `maturin` with `bindings = "bin"`: the wheel carries the compiled binary, no
  PyO3 and no Python source.
- Verified locally on all three install paths: `pip install <wheel>`,
  `uv pip install <wheel>`, and `uv tool install .` (builds from source).
- CI builds manylinux2014 wheels for x86_64 and aarch64, an sdist, and
  smoke-tests the x86_64 wheel in a clean venv.

> A **local** `maturin build` tags the wheel with this machine's glibc
> (`manylinux_2_39` here), which most login nodes are too old to accept. Only the
> CI container's manylinux2014 wheels are fit to publish — never upload a locally
> built one.

### P1 — `src/model/`
`format.rs`, `job_id.rs`, `job.rs`, `partition.rs`, `accounting.rs`,
`efficiency.rs`. All I/O-free.

### P2 — `src/slurm/`
- `parse.rs` — every output parser, with the Python test fixtures ported verbatim.
- `reason.rs` — pending-reason codes into sentences, start-estimate formatting.
- `transport.rs` — `CommandRunner` trait, `LocalRunner`, recording test stub.
- `query.rs` — the `Slurm` facade; availability latches are fields, not globals.
- `action.rs` — cancel, update, resubmit, batch-script fetch and archive.
- `fs.rs` — backwards-seeking log tail, remote-aware reads.
- `cache.rs` — `DetailCache` and `ScriptStore` traits: what the Slurm layer needs
  remembered, without saying where it goes.

### P3 — `src/config/`, `src/cli.rs`, `src/startup.rs`
- `paths.rs` — XDG resolution, `~` expansion, atomic writes with permissions.
- `file.rs` — `config.toml`; reads never fail, writes preserve comments.
- `log_cache.rs` — per-job log paths and submit commands, merge-on-write so two
  sessions cannot clobber each other.
- `script_cache.rs` — the sbatch-script archive, owner-only, keyed by base job id.
- `cli.rs` — clap arguments; `startup.rs` — the CLI→file→default resolution and
  the trait impls that join `config` to `slurm`.

**Carve-out closed:** `job_detail` now records what `scontrol` reports and
consults the cache before guessing log paths, which is what keeps a job's logs
reachable after `MinJobAge`.

### P4 — `src/ui/`

**State** (no terminal or rendering type anywhere in it, per §5.2 of the plan):
- `filter.rs` — the query language, ported from `test_filters.py`.
- `job_table.rs` — `JobTable<T>` over both job types: filtering, array grouping
  and expansion, bookmark pinning, multi-select, the no-match placeholder,
  cursor preservation. Ported from `test_array_collapse.py`.

**Rendering and the loop:**
- `theme.rs` — every style, replacing the 288-line stylesheet. The partition
  hash matches the Python's byte sum, so both agree on colours.
- `text.rs` — truncation by display column, not character count.
- `layout.rs` — the stylesheet's fr ratios as `Constraint`s.
- `render.rs` — job tables, cluster bar, command log, footer, filter bar.
- `event.rs` — keys, ticks and finished queries on one channel.
- `terminal.rs` — RAII ownership, panic hook, and the suspend helper P7 needs.
- `app.rs` — state and key handling; `handle_key` returns a `Command` rather
  than acting, so every keystroke is testable without a terminal.

Three things worth knowing before touching this code:

- The cursor anchor is **held as state**, not read off the rows during a
  rebuild. A rebuild normally follows a change to the job list, at which point
  the existing rows index into a list that no longer matches them.
- `Row::Job` carries an *index* into the job list, so a renderer must read jobs
  through `JobTable::job()` and must not cache rows across a poll.
- Columns are **sized to their content**. Fixed widths made ratatui drop whole
  columns off a 38-column panel rather than shrink them, and the job tables are
  routinely that narrow.

Keys so far: `q` quit, `/` filter (`Enter` accepts, `Esc` abandons), `r`
refresh, `m` bookmark, `Enter` expand an array, `↑`/`↓`/`j`/`k` with wrapping
between the two tables, `g`/`G` for the ends.

### P5 — the right-hand column
- `tabs.rs` — a tab strip whose visible set can change, remembering the active
  tab by name.
- `log_pane.rs` — replaces `RichLog`; follows the newest line until the user
  scrolls up.
- `detail.rs` — stdout/stderr/cpu/gpu/stats, with the efficiency report ported
  literally.
- `metadata.rs` — Resources, Submission, Pending, Raw.
- `help.rs` — **bindings and help from one table**, so they cannot drift. Add a
  key by adding a `Binding`; it is documented by construction.

Detail loading is debounced 200 ms and generation-stamped. A superseded load
checks the generation before asking Slurm anything, so arrowing through a list
costs one set of commands rather than one per row.

### P6 — full-screen panels
- `slurm/live.rs` — `node_processes` and `gpu_status`, the only two queries that
  reach a compute node rather than Slurm.
- `ui/simple_table.rs` — the ungrouped tables (partitions, nodes, other users'
  jobs, usage rows), sharing the cursor-follows-its-row rule.
- `ui/screens.rs` — the three panels, their summary bars and their bars.
- `ui/app.rs` — a screen stack. Keys route to the top screen, which carries its
  own help context, so `Up` means "next partition" there and "next job" on the
  main view without either knowing about the other.

Two rules worth keeping when extending this:

- **Replies are dropped when they no longer match the cursor.** A slow `sreport`
  or a stale partition job list must not overwrite what is on screen.
- **Live tabs fetch only while showing.** Each is an SSH round trip to a compute
  node; paying for it behind a hidden tab is waste.

### P7 — actions
- `ui/modal.rs` — confirm-cancel, confirm-resubmit, the job editor. A modal owns
  its keys and returns what the user decided; it never acts, which is what makes
  the destructive paths testable without anything that could reach a cluster.
- Keys: `c`, `Shift+C`, `Ctrl+V`, `u`, `s`, `b`, `o`, `e`, `Shift+E`, `l`, `,`.
- Shell-outs go through `terminal::suspended`, written in P4 for this.

**Remote-mode quoting is done properly rather than ported.** The pager runs on
the cluster with each argument quoted for the remote shell and the whole command
quoted again for the local one; `ssh` hops through the live session's control
socket rather than `-J`. Those are Python issues #39 and #40 — do not copy that
code back.

### P8 — remote mode
- `ssh/pty.rs` — spawning on a pty, which is what makes 2FA work at all. A test
  asserts the property directly: the child sees a terminal.
- `ssh/session.rs` — master, auth pump, multiplexed channel, marker-framed
  `run()`, reconnect, close.
- `ssh/runner.rs` — `RemoteRunner`. **This is the whole of what remote mode
  changes.** Everything above the transport was written once.

The prompt crosses tasks: the session sends the question on the event channel
with a `oneshot` for the reply, and the main loop owns the modal. `r` retries a
refused or dropped session.

Two test-isolation bugs were fixed here, both worth knowing about:

- Tests were calling `std::env::set_var("HOME", …)`, which is process-wide and
  raced with the session's control-path lookup. `expand_tilde` now has an
  injectable-home inner function so no test needs to.
- `PtyProcess::kill` kills the process *group*. We put the child in its own, so
  killing only the leader left children holding the pty open and the reader
  blocked long after `close()`.

## Divergences

Seven, all recorded in [DIVERGENCES.md](DIVERGENCES.md) with reasoning. One (#4)
is a genuine Python bug the port surfaced: `JobDetail`'s accessors fall back on
key *presence* rather than a non-empty value, so an empty `sacct` column can show
a blank where the fallback field has the answer. Worth fixing in the Python too.

## Still deferred

- **`g`/`G`** are bound on the job tables only. The full-screen panels do not
  advertise them and do not handle them; add to their binding tables if wanted.

## Next steps

P9 — polish and release:

1. Rewrite the README for the Rust implementation: `pip install lazyslurm-rs`,
   the `lazyslurm-py` / `lazyslurm-rs` name-conflict warning, and the note that
   an sdist build needs a Rust toolchain.
2. Walk `MANUAL_TESTS.md` on a real cluster, local **and** `--remote`. Nothing
   below has been run against real Slurm yet — every check so far is against
   recorded output or a local shell.
3. Optional: the `python -m lazyslurm` shim, and shell completions via
   `clap_complete`.
4. Tag, build wheels in CI, `maturin upload`.

Before releasing, walk §6 of the plan — the "behaviour that is easy to lose"
checklist. Most items now have tests, but the list is the acceptance gate.

### The old P8 notes, kept for reference Read `reference/python/src/lazyslurm/ssh.py`
in full first; its module docstring explains the design, and every part of it is
load-bearing.

1. `ssh/pty.rs` — spawn `ssh -N -M` **on a pty**. That is the whole trick: ssh
   writes prompts to `/dev/tty`, so with plain pipes the 2FA question never
   reaches us. Use `nix::pty::openpty`, and read it from a blocking task
   (mirroring the Python's `run_in_executor`) rather than fighting `AsyncFd`.
2. `ssh/mod.rs` — the auth pump (prompt regexes, answer, clear the buffer so the
   same prompt is not re-matched), the multiplexed shell channel, and `run()`
   with its marker framing and 20s timeout.
3. A `RemoteRunner` implementing `CommandRunner`. Nothing above the transport
   changes — that was the point of the trait.
4. Wire `Slurm::session_control_path`, which currently returns `None`. The
   editor, pager and `o` all need it to ride the authenticated connection.
5. The SSH prompt modal, crossing tasks: send the prompt on the event channel
   with a `oneshot::Sender` for the reply, and let the main loop own the modal.

`r` must retry a failed or dropped session — it is the user's only way back
after cancelling or mistyping a code.

Port `tests/test_ssh_session.py` (431 lines); it overrides the channel argv to
run a local `/bin/sh`, and that seam ports cleanly.
