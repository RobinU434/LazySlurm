---
title: "Resubmit a job with modified resources"
labels: enhancement
---

## Problem

The common loop after a failure is "run it again, but bigger" — more time after a
TIMEOUT, more memory after an OOM. Today `s` resubmits the job exactly as it was, and
the resource editor (`u`) only works on jobs that are already queued.

## What to build

`Shift+S` (or `s` on a TIMEOUT/OOM job) opens the existing `EditJobScreen`, prefilled
from the terminated job's allocation, then submits with overrides:

```
sbatch --chdir <workdir> --time=<new> --mem=<new> --cpus-per-task=<new> <script>
```

- Reuse `EditJobScreen`; map its fields to `sbatch` flags rather than `scontrol update`
  keys (`TimeLimit` → `--time`, `MinMemoryNode` → `--mem`, `NumCPUs` →
  `--cpus-per-task`, `Partition` → `--partition`, `NumNodes` → `--nodes`).
- Blank field = keep whatever the original submit line had.
- Pre-fill smartly: after TIMEOUT suggest 2× the time limit, after OUT_OF_MEMORY suggest
  2× the memory. Show the suggestion as the prefilled value so it can be overridden.
- Log the full `sbatch` line to the Command Log before running it.

## Acceptance criteria

- [ ] Overrides land as sbatch flags and win over the same flag in the original line.
- [ ] Blank fields change nothing.
- [ ] The archived-script fallback still applies when the original file is gone.
- [ ] Tests: flag mapping, override-wins, blank handling, suggestion for TIMEOUT/OOM.

## Notes

`slurm.resubmit_job()` and `_script_token_index()` already build the argv; extend rather
than fork them. `slurm.EDITABLE_FIELDS` is the field list to reuse.
