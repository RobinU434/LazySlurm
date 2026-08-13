## Job arrays are one row

A 40-task array used to fill the table with forty near-identical lines. It now folds
into one:

```
▸ 4815201_[0-39] ×40    train-sweep    gpu    32run 8pend
```

<img src="https://raw.githubusercontent.com/RobinU434/LazySlurm/main/img/job-array.png" alt="A collapsed job array" width="100%">

The row carries the id range, the shared name and a tally of what the tasks are doing.
`Enter` expands and collapses it, and the expansion survives refreshes and filter
changes — a poll never folds a group you opened.

Actions understand the group: `c` cancels the whole array with one `scancel`, `u` edits
every pending task in it, `m` bookmarks it. The cluster bar counts array *tasks* rather
than squeue rows, so a pending `123_[3-11]` contributes nine pending jobs, matching what
the table shows.

Turn it off with `collapse_arrays = false` if you prefer one row per task.

## Resubmit with more of what ran out

The loop after a failure is "run it again, but bigger" — more time after a TIMEOUT, more
memory after an OOM kill. `u` cannot help there: Slurm fixes a job's allocation once it
starts, so the property editor only works on jobs still queued.

**`Shift+S`** opens that same editor for a *terminated* job, prefilled with what the job
actually had, and submits the changed fields as sbatch flags:

```
sbatch --chdir /work --time=4:00:00 --mem=16G job.sh
```

After a **TIMEOUT** the runtime is prefilled with double the old limit, and after
**OUT_OF_MEMORY** the memory with double the old request. They are suggestions — clear or
overwrite them.

An override *replaces* the same option in the original submit line rather than being
appended after it, so the `sbatch` line written to the Command Log is exactly what ran. A
field left blank keeps whatever the original had, and arguments after the script name
belong to the script and are never touched.

## Choose how `o` reaches a compute node

Pressing `o` opens a shell on the job's node. There are two ways to do that, and they
land you somewhere meaningfully different:

```toml
interactive_shell = "ssh"    # "ssh" (default) | "srun"
```

**`ssh`** puts you on the machine, *outside* the job's cgroup: `nvidia-smi` shows every
GPU on the node rather than the one you allocated, and nothing you start is capped by the
job's limits. It adds no job step, so it cannot skew the efficiency report.

**`srun`** puts you *inside* the allocation — correct `CUDA_VISIBLE_DEVICES`, correct
limits, the environment the job sees. The cost is a job step in `sacct`, and an idle
debugging shell drags the job's reported CPU efficiency down.

Use `ssh` to poke at the machine, `srun` to debug inside your allocation. `o` uses the
configured one and **`Shift+O`** uses the other, for a single shell. You may *have* to use
`srun` on clusters where `pam_slurm_adm` refuses SSH without an allocation.

## Remote mode: one login, and it stays up

Three things went wrong on clusters with two-factor authentication, and all three are
fixed:

- **The code was asked for repeatedly.** The refresh timer started at mount rather than
  after the login, so every tick that landed while you were still typing found the session
  "not connected", tried to reconnect, saw no live master — the first was still waiting on
  your phone — and started another `ssh -M`, which asked for another code.
- **A slow login leaked a thread per second**, out of the same pool log reading uses, and
  could **lose the prompt entirely**: the pump started a fresh pty read each second, and
  ssh's eventual write could land in a reader nobody was awaiting. The login then hung to
  the two-minute timeout.
- **Paths with a space or an apostrophe broke**: opening a remote log in your editor
  quoted the `scp` path for one shell where it passes through two, and reading a log built
  its "file not found" message on the cluster with the path inside single quotes.

## It tells you when it cannot work

Starting `lazyslurm` off a cluster used to crash on the first poll with
`FileNotFoundError: 'squeue'`. It now says what is wrong and what to do instead:

```
lazyslurm: no Slurm commands found on this machine (squeue, sacct).

LazySlurm reads jobs from the Slurm CLI, so it has to run somewhere those exist:
  - on a cluster login node, or
  - on your own machine against a cluster, with --remote:

      lazyslurm --remote user@login.hpc.edu
```

A command that goes missing while it runs — a partial install where only `sstat` or
`sprio` is absent — now leaves one empty panel and a line in the Command Log, rather than
taking the app down.

## Browsing what you filtered

`Enter` in the filter bar now **accepts** the filter: the bar closes, the query stays in
force, and the cursor lands back on the matching rows so you can act on them. `Escape`
still abandons it. Previously every way out of the bar cleared the query, so the rows a
filter found could not be reached from the keyboard.

`Tab` cycles the three panels you act in — job tables → Job Details → Job Metadata — and
comes back to the tables.

The partition and node screens no longer jump to the top every few seconds: their job
lists are polled on a timer, and the cursor now stays on the job you were reading. It also
follows that job when rows above it disappear.

## Fixes worth knowing about

- **A `|` in a job name shifted every column** of its row — including `state`, which drives
  colours and filters, and the working directory, which resubmit passes as `--chdir`.
- **A down node was shown as 100% memory used.** `sinfo` reports `FreeMem` as `N/A` for a
  node that has not reported, which was read as zero free. Unknown now renders `—`, as the
  load column in the same row already did.
- **The stats tab could advise `--time=00:00:00`** for a job shorter than 40 seconds —
  which Slurm reads as *unlimited*, the opposite of the advice.
- **The "CPU" sparkline plotted memory.** Both series were memory readings, which looked
  like two measurements agreeing. It now plots real CPU against a fixed 0–100% scale.
- **Job names with CJK, emoji or combining marks broke column alignment**, because
  truncation counted code points where a terminal lays out columns.
- **Saving config destroyed the commented template** — `--partition-order` rewrote
  `config.toml` from a dict, deleting the documentation for every setting.
- **`cache_max_age_days = 0` deleted the whole script archive.** The template documented
  `null = never`, which TOML cannot express; `0` is now what "never" means.
- **A misspelled config key was silently ignored.** Unknown settings are now listed in the
  Command Log at startup and after an in-app reload. The file is never rejected over a typo.

## Upgrading

```bash
pip install --upgrade lazyslurm-py
# or
uv tool upgrade lazyslurm-py
```

Nothing to migrate. `tomlkit` is a new dependency, installed automatically — it is what
lets LazySlurm edit `config.toml` in place instead of regenerating it.
