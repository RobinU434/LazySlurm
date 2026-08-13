# Rust port — status

Tracks progress against [RUST_PORT_PLAN.md](RUST_PORT_PLAN.md). One line per work
item; update as you go, so a resumed session knows where it stopped.

**Branch:** `rust-dev` · **Tests:** 304 passing · **Last updated:** 2026-08-13

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
| P4 | Job tables, filtering, arrays, main screen | next |
| P5 | Detail / metadata / help panels | not started |
| P6 | Partition, node and usage screens | not started |
| P7 | Actions wired to the UI, editor/pager shell-outs | not started |
| P8 | Remote mode over one SSH session | not started |
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

## Divergences

Four, all recorded in [DIVERGENCES.md](DIVERGENCES.md) with reasoning. One (#4)
is a genuine Python bug the port surfaced: `JobDetail`'s accessors fall back on
key *presence* rather than a non-empty value, so an empty `sacct` column can show
a blank where the fallback field has the answer. Worth fixing in the Python too.

## Still deferred

- **`format_cluster_summary`** is not ported. It builds Rich markup, so it
  belongs with the UI in P4 as styled spans rather than a marked-up string.
- **Opportunistic script archiving on detail load.** `archive_batch_script` and
  the store are both in place; hooking it into the detail-load path is P7, where
  the UI decides when to spend the extra call.

## Next steps

1. Start P4. Read §5.2 of the plan **before** writing UI code: state transitions
   must be testable without a terminal, or the UI tests cannot be ported at all.
2. First slice: `ratatui` + `crossterm`, the two-column layout, and the active
   job table with cursor preservation across polls.
3. Then filtering and array collapse, ported against `test_filters.py` and
   `test_array_collapse.py`.
