# Handoff: finishing the Rust port on a real cluster

You have something the previous agent did not: **access to a Slurm cluster.**
That is the only thing standing between this branch and a release.

Everything is implemented. Nothing has met real Slurm. Every test so far runs
against recorded command output, a local `/bin/sh` standing in for the SSH
channel, or a scripted two-factor login. The parsers are exercised by fixtures
lifted from the Python's own test suite, but no `squeue` has actually been
invoked by this code.

Your job, in order:

1. [Turn cluster access into permanent tests](#1-capture-real-output-as-fixtures) — the highest-value thing you can do.
2. [Verify the interface](#2-verify-the-interface) against [MANUAL_TESTS.md](MANUAL_TESTS.md).
3. [Fix what you find](#3-fixing-what-you-find).
4. [Release](#4-releasing).

---

## Orientation

**Branch:** `rust-dev`. 22 commits, pushed. `main` is still the Python version.

| Path | What it is |
|---|---|
| `src/` | The Rust implementation |
| `reference/python/` | The original Python, kept for comparison — **it still runs** |
| `RUST_PORT_PLAN.md` | The plan this was built from; §6 and §11 still matter |
| `PORT_STATUS.md` | What is done, and the decisions behind it |
| `DIVERGENCES.md` | Where the Rust deliberately differs from the Python, and why |
| `MANUAL_TESTS.md` | The checklist you are here to walk |
| `scripts/drive_tui.py` | Drives the TUI in a pty — see [below](#driving-the-tui) |

```sh
cargo test
cargo clippy --all-targets -- -D warnings
cargo fmt --all -- --check
```

586 tests, all green, and they must stay that way. `rust-toolchain.toml` pins
the toolchain; if `cargo fmt` is missing you have a distro Rust without rustup.

### The reference implementation is your oracle

`reference/python/` runs on the same cluster:

```sh
cd reference/python && uv pip install -e . && lazyslurm
```

Wherever the two disagree, **one of them is wrong and you have to work out
which**. Do not assume it is the Rust. Eighteen bugs in the Python have already
been filed from this port (issues #22–#34, #37–#40, #43); another disagreement
being a nineteenth is entirely plausible. `DIVERGENCES.md` lists the seven places
they differ **on purpose** — check it before reporting a difference.

---

## 1. Capture real output as fixtures

Cluster access is temporary; a fixture is forever. Before touching the
interface, turn your cluster into regression tests.

Every parser in `src/slurm/parse.rs` is a pure `&str -> data` function, and each
already has tests built from fixtures. Add yours alongside them.

### Capture

Run exactly the command the code runs. The formats are in
`src/slurm/query.rs` as consts — copy them rather than retyping:

```sh
mkdir -p /tmp/fixtures

squeue -u "$USER" --format='%i|%j|%M|%P|%T|%l|%D|%C|%m|%b|%Z' --noheader --sort=-i \
  > /tmp/fixtures/squeue.txt

sacct -u "$USER" --format=JobID,JobName,State,ExitCode,Start,End,Elapsed,Partition \
  --starttime="$(date -d '7 days ago' +%Y-%m-%dT00:00:00)" --noheader --parsable2 \
  > /tmp/fixtures/sacct.txt

sinfo --noheader --summarize --format='%P|%a|%F|%C|%l|%G' > /tmp/fixtures/sinfo.txt

sinfo -N -p "$(sinfo -h -o %P | head -1 | tr -d '*')" --noheader \
  -O 'NodeHost:|,StateLong:|,CPUsState:|,Memory:|,FreeMem:|,CPUsLoad:|,Gres:|,GresUsed:|,Reason:|' \
  > /tmp/fixtures/sinfo_nodes.txt

scontrol show job "$SOME_JOB_ID"   > /tmp/fixtures/scontrol.txt
sacct -j "$SOME_JOB_ID" --format=JobID,TotalCPU,Elapsed,ReqMem,AllocTRES,ReqTRES,AllocCPUS,NNodes,NTasks,Timelimit,MaxRSS \
  --noheader --parsable2       > /tmp/fixtures/sacct_stats.txt
sprio --noheader --format='%i|%Y|%A|%F|%J|%P|%Q'  > /tmp/fixtures/sprio.txt
sshare -P -o Account,User,RawShares,NormShares,RawUsage,EffectvUsage,FairShare -U \
  > /tmp/fixtures/sshare.txt
sreport cluster AccountUtilizationByUser start="$(date -d '1 month ago' +%Y-%m-%d)" \
  end=now -t hours -P --noheader > /tmp/fixtures/sreport.txt
```

**Anonymise before committing.** Real output carries usernames, account names,
node names and job names, some of which are project names people would rather
not publish. Rewrite them; keep the *shapes* — field counts, empty columns,
odd states, the `*` on the default partition, a drained node's reason.

### Turn each into a test

Put the interesting ones in `tests/fixtures/` and add cases to the existing
`mod tests` in `src/slurm/parse.rs`:

```rust
#[test]
fn parses_our_clusters_squeue_output() {
    let jobs = squeue_jobs(include_str!("../../tests/fixtures/squeue-real.txt"));
    assert_eq!(jobs.len(), 4);
    // Assert on the thing that was surprising about your cluster.
    assert_eq!(jobs[0].gres, "gres/gpu:2");
}
```

**Prioritise whatever your cluster does that the fixtures do not.** Older Slurm,
a partition with mixed hardware, heterogeneous jobs (`123+0`), an array with a
throttle (`[1-4%2]`), a node with several GRES types, a job whose name has a
space or non-ASCII in it. Those are the rows most likely to break something, and
the ones nobody could invent in advance.

If a parser gets it wrong: **fix the parser, keep the fixture.** That is a bug
the port would otherwise have shipped.

---

## 2. Verify the interface

[MANUAL_TESTS.md](MANUAL_TESTS.md) is the checklist. Work through it in order;
sections 1–6 are local mode, section 7 is remote.

### Safety — read this before section 4

**Sections 4 and 7 press keys that cancel, edit and resubmit real jobs.**

- **Submit your own scratch jobs and act only on those.** Never `c`, `Shift+C`,
  `u` or `s` a job you did not submit.
  ```sh
  for i in 1 2 3; do sbatch --wrap 'sleep 600' -J lazyslurm-test-$i; done
  sbatch --array=1-4 --wrap 'sleep 600' -J lazyslurm-array-test
  ```
- Filter to them (`/name:lazyslurm-test`) before touching anything, so the
  cursor cannot be on somebody else's job.
- `Shift+C` has **no confirmation**. That is deliberate, and it makes a
  misdirected keystroke expensive.
- Clean up: `scancel -n lazyslurm-test-1 …`, or `scancel -u "$USER" --name=…`.
- On a shared login node, remember the app SSHes to compute nodes for the live
  tabs. `--no-live` turns that off if you would rather it did not.

### Driving the TUI

The program needs a real terminal — with a redirected stdout it has no size and
draws nothing. `scripts/drive_tui.py` allocates a pty, sizes it, sends keys on a
schedule and prints what was drawn:

```sh
# Open the partition monitor, then quit; strip escapes for grepping.
scripts/drive_tui.py --plain --keys 'p' -- ./target/debug/lazyslurm --refresh 0

# Filter, accept it, look at the stats tab.
scripts/drive_tui.py --plain --keys '/train\r]]]]' -- ./target/debug/lazyslurm

# Slower, for a cluster where a poll takes a while.
scripts/drive_tui.py --plain --settle 4 --key-delay 1.5 --keys 'p\r' -- ./target/debug/lazyslurm
```

`q` is appended automatically. `\r` is Enter, `\e` Escape, `\t` Tab.

What you get back is one long string containing every frame. That is enough for
`grep`, and enough to assert that a panel appeared and a value is in it. It is
**not** enough to judge layout, colour or whether a redraw flickered — do that
by eye, at least once, at 80×24 and at your normal size.

Two traps:

- Frames accumulate. `grep` finding something proves it appeared at *some*
  point, not that it is on screen at the end.
- Timing is real. If a poll takes three seconds and you send a key after one,
  you are testing the empty state. Raise `--settle`.

### What to compare against

For anything numeric, check the app against Slurm directly rather than against
your expectations:

| Panel | Compare with |
|---|---|
| Cluster bar counts | `squeue -u $USER -h -t R \| wc -l` — but the bar counts array **tasks**, so a pending `123_[1-9]` row is 9, not 1 |
| Terminated table | `sacct -u $USER --starttime=… -X` (`-X` matches the sub-step filtering) |
| Efficiency block | `seff <jobid>` — the definitions are meant to agree |
| Partition monitor | `sinfo --summarize` |
| Node view | `sinfo -N -p <partition> -O NodeHost,StateLong,CPUsState,Gres,GresUsed` |
| Account usage | `sreport cluster AccountUtilizationByUser start=… end=now -t hours` |
| Fair share | `sshare -U` |

`seff` is the sharpest of these: if CPU or memory efficiency disagrees with it,
something in `src/model/efficiency.rs` or `src/slurm/parse.rs::sacct_stats` is
wrong, and it matters — that panel is the reason people open the stats tab.

### Remote mode

Section 7 is the least-tested path in the codebase. The auth pump has only ever
met a shell script pretending to be ssh.

Run it from a machine that is *not* the cluster:

```sh
./target/debug/lazyslurm --remote user@login.hpc.edu
```

Check, with `ps` on the local machine, that there is **one** `ssh` master and
that it does not multiply as you use the app. That is the entire point of the
design, and the thing that makes 2FA bearable.

If your cluster uses two-factor authentication, that is the single most valuable
test on the list. Nobody has run it.

---

## 3. Fixing what you find

### Where things live

| Symptom | Look in |
|---|---|
| A field parsed wrong, or a row dropped | `src/slurm/parse.rs` |
| A command rejected by Slurm | the format consts at the top of `src/slurm/query.rs` |
| Efficiency numbers disagree with `seff` | `src/model/efficiency.rs`, `parse.rs::sacct_stats` |
| A panel shows the wrong thing | `src/ui/detail.rs`, `metadata.rs`, `screens.rs` |
| A key does nothing / the wrong thing | `src/ui/help.rs` — the binding table **is** the help |
| Cursor jumps, list reorders oddly | `src/ui/job_table.rs`, `src/ui/simple_table.rs` |
| Remote command mangled | `src/ssh/session.rs`, `src/slurm/transport.rs::quote_argv` |
| Terminal left broken | `src/ui/terminal.rs` |

### The rules that still apply

From §11 of the plan, and they have earned their place:

1. **The Python is the specification.** Where it and the Rust disagree, work out
   which is right before changing either.
2. **Do not "improve" Slurm command strings or parsing heuristics.** Every
   odd-looking one is a workaround for a real cluster; the comments say which.
   If your cluster needs a different one, add a fallback — do not replace.
3. **Every fix gets a test**, from the real output that exposed it.
4. **If you diverge from the Python deliberately, record it** in
   `DIVERGENCES.md` with the reasoning, and file an issue against the Python.
5. **When something looks like a Python bug, it probably is.** Eighteen have
   been found this way. `.github/issue-drafts/` holds the format;
   `create-issues.sh` files them.

### If a divergence turns out to be wrong

`DIVERGENCES.md` has seven entries where the Rust deliberately differs. Three
are fixes for filed Python bugs (#23, #28, and the filter-accept key). If a
cluster shows one of those decisions was wrong, revert it and say so in the file
— they are decisions, not commitments.

---

## 4. Releasing

Only after `MANUAL_TESTS.md` is walked and its sign-off block is filled in.

1. Work through §6 of `RUST_PORT_PLAN.md`, the "behaviour that is easy to lose"
   checklist. Seventeen of nineteen items have tests; the two that do not —
   opportunistic script archiving, and remote mode never opening a second
   connection — are exactly the two that need a cluster. Confirm them by hand.
2. Set the version in `Cargo.toml` (`pyproject.toml` takes it from there).
3. Configure PyPI **trusted publishing** for this repository, or add a
   `PYPI_API_TOKEN` secret and change the publish step in
   `.github/workflows/release.yml` to use it.
4. Tag:
   ```sh
   git tag v0.1.0 && git push origin v0.1.0
   ```

**Never upload a locally built wheel.** It carries the glibc of whatever built
it — a local build here tags `manylinux_2_39`, which will not install on most
login nodes. Only the CI container's `manylinux2014` (glibc 2.17) wheels are fit
to publish. The workflow already does this correctly; the danger is doing it by
hand.

Consider a pre-release (`v0.1.0-rc1`) first, installed on a real login node from
PyPI, before the version people will actually get.

---

## What "done" looks like

- [ ] Real cluster output committed as fixtures, with tests over it
- [ ] `MANUAL_TESTS.md` walked, local **and** remote, sign-off filled in
- [ ] Two-factor login exercised against a real cluster, if yours has one
- [ ] Everything found either fixed with a test, or filed with a reason
- [ ] §6 of the plan confirmed
- [ ] `cargo test`, `clippy -D warnings`, `fmt --check` all clean
- [ ] Wheel installed from CI on the oldest login node you support
- [ ] The README's Status section rewritten — it currently says alpha *because*
      none of the above had happened. When it has, say so.
