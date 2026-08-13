---
title: "_guess_log_path stats the same candidate twice"
labels: good first issue, performance
---

## Problem

The candidate list in `_guess_log_path` contains the same path twice:

```python
# src/lazyslurm/slurm.py
candidates.extend([
    os.path.join(work_dir, f"{job_name}-{job_id}.{ext_out}"),      # <- here
    os.path.join(work_dir, f"{job_name}_{job_id}.{ext_out}"),
    os.path.join(work_dir, f"{job_name}.{ext_out}"),
    # sbatch --output/--error with %j pattern
    os.path.join(work_dir, f"{job_name}-%j.{ext_out}".replace("%j", job_id)),  # <- same
])
```

`f"{job_name}-%j.{ext_out}".replace("%j", job_id)` produces
`{job_name}-{job_id}.{ext_out}`, which is already the first entry.

## Why it matters

Each candidate costs a `stat` — and in remote mode a full `test -f` round trip
over SSH. The function is called twice per job detail load (stdout and stderr),
so this is two wasted round trips per selection of a job that has aged out of
`scontrol`.

It is also misleading: the comment implies `%j` handling that the code does not
actually add.

## Fix

Delete the duplicate. If the intent was to cover more `sbatch` output patterns,
the ones actually missing are `%x` (job name) and `%A_%a` (array master/task),
which would be a real addition:

```python
f"{job_name}_{array_master}_{array_task}.{ext_out}"   # %A_%a
```

Deduplicating the list generally (`dict.fromkeys(candidates)`) is worth doing
either way, since `ext_out == suffix` when `suffix == "out"` makes the first two
entries identical too.
