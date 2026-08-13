# Rust port — status

Tracks progress against [RUST_PORT_PLAN.md](RUST_PORT_PLAN.md). One line per work
item; update as you go, so a resumed session knows where it stopped.

**Branch:** `rust-dev` · **Tests:** 247 passing · **Last updated:** 2026-08-13

## Layout

| Path | What it is |
|---|---|
| `src/` | The Rust implementation — the real source from here on |
| `reference/python/` | The Python implementation, kept for lookup only |
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
| P2 | Slurm command layer: parsers, transport, queries, actions | **done** (2 carve-outs below) |
| P3 | Config file, on-disk caches, CLI resolution | **partial** — CLI done, file + caches pending |
| P4 | Job tables, filtering, arrays, main screen | not started |
| P5 | Detail / metadata / help panels | not started |
| P6 | Partition, node and usage screens | not started |
| P7 | Actions wired to the UI, editor/pager shell-outs | not started |
| P8 | Remote mode over one SSH session | not started |
| P9 | Polish and release | not started |

## Done in detail

### P0 — packaging proven first
- `maturin` with `bindings = "bin"`: the wheel carries the compiled binary, no
  PyO3 and no Python source.
- Verified locally on all three install paths:
  - `pip install <wheel>` → `lazyslurm` on PATH ✓
  - `uv pip install <wheel>` ✓
  - `uv tool install .` (builds from source via the maturin backend) ✓
- CI (`.github/workflows/rust.yml`) builds manylinux2014 wheels for x86_64 and
  aarch64, an sdist, and smoke-tests the x86_64 wheel in a clean venv.

> A **local** `maturin build` tags the wheel with this machine's glibc
> (`manylinux_2_39` here), which most login nodes are too old to accept. Only the
> CI container's manylinux2014 wheels are fit to publish — never upload a locally
> built one.

### P1 — `src/model/`
`format.rs`, `job_id.rs`, `job.rs`, `partition.rs`, `accounting.rs`,
`efficiency.rs`. All I/O-free. Ported edge cases include array specs with `%`
throttles and `:` strides, `parse_req_mem`'s per-node/per-cpu markers, the
per-node memory denominator, and `None`-vs-zero for unrecorded efficiency.

### P2 — `src/slurm/`
- `parse.rs` — every output parser, with the Python test fixtures ported verbatim.
- `reason.rs` — pending-reason codes into sentences, start-estimate formatting.
- `transport.rs` — `CommandRunner` trait, `LocalRunner`, and a recording stub
  used by the tests. Remote mode plugs in here at P8 with no caller changes.
- `query.rs` — the `Slurm` facade. Availability latches (`sprio`/accounting
  missing) are fields on it rather than globals.
- `action.rs` — cancel, update, resubmit, batch-script fetch.
- `fs.rs` — backwards-seeking log tail, remote-aware file reads.

## Carve-outs — deliberate, not forgotten

1. **`job_detail` does not yet consult or populate the log-path cache.** The
   Python caches `StdOut`/`StdErr`/`Command` from scontrol so they survive after
   the job leaves slurmctld, and opportunistically archives the batch script.
   Wire this in with P3's caches. Guessing from filename patterns already works.
2. **`format_cluster_summary` is not ported.** It builds Rich markup, so it
   belongs with the UI in P4 as styled spans rather than a marked-up string.

## Local toolchain note

This machine has a distro Rust with no `rustup`, so `cargo fmt` and
`cargo clippy` are unavailable locally — CI runs both and gates on them. If you
work here, either install rustup or rely on CI for lint and formatting.

## Next steps

1. Finish P3: `config.toml` load/save with `toml_edit` (preserving the user's
   comments, unlike the Python), `log_cache.json`, the script archive, and the
   CLI→file→default resolution chain.
2. Close carve-out 1 once the caches exist.
3. Start P4. Read §5.2 of the plan **before** writing UI code: state transitions
   must be testable without a terminal, or the UI tests cannot be ported at all.
