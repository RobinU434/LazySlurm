---
title: "Job efficiency report (built-in seff)"
labels: enhancement
---

## Problem

Over-requesting resources is the main reason jobs sit in the queue, and the data to spot
it is already fetched — but the stats tab shows absolute numbers, never "you asked for
five times what you used".

## What to build

An **Efficiency** block in the stats tab for terminated (and running) jobs:

```
CPU     1.4 / 8 cores    18%  ▂▂▁▁▁▁▁▁  ← over-requested
Memory  8.2G / 40G       20%  ▂▂▁▁▁▁▁▁  ← over-requested
GPU     2 / 2            —
Walltime 6:37 / 24:00    28%
```

- CPU efficiency = `TotalCPU / (NCPUS × Elapsed)`; memory = `MaxRSS / ReqMem`;
  walltime = `Elapsed / Timelimit`. All from `sacct` fields already in `JobStats`
  (add `NCPUS`, `Timelimit`, `ReqMem` if missing).
- Colour: green 60-100%, yellow 25-60%, red <25%; flag ≥100% memory as at-risk of OOM.
- One-line hint when a job is badly sized: "next time try `--mem=12G --cpus-per-task=2`".

## Acceptance criteria

- [ ] Percentages match `seff <jobid>` for the same job (spot-check a few).
- [ ] Multi-node and array tasks compute per-task, not summed wrongly.
- [ ] Missing fields (job too old for sacct) degrade to "unavailable".
- [ ] Tests: efficiency maths incl. zero/absent denominators, colour thresholds.

## Notes

`slurm.get_job_stats()` and `models.JobStats` are the place. `parse_mem_bytes()` in
`widgets/detail_view.py` already normalises Slurm's memory strings.
