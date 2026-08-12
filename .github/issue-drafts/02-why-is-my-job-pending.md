---
title: "Explain why a job is pending"
labels: enhancement
---

## Problem

The most common question at a queue — *why is this not running, and when will it?* — is
answerable from Slurm but not shown. Today only the raw reason code appears
(`(QOSMaxGRESPerUser)`, `(Resources)`, `(Priority)`).

## What to build

A **Pending** section in `MetadataView` (or a new tab), shown when the selected job is
PENDING:

- **Estimated start**: `squeue -j <id> --start --format=%S` → "starts ~14:20 (in 2h10m)".
  Slurm reports `N/A` when it cannot estimate — say so rather than showing nothing.
- **Priority breakdown**: `sprio -j <id> -o "%i|%Y|%A|%F|%J|%P|%Q"` → age, fairshare,
  job size, partition, QOS components, plus the total and the job's rank among the
  partition's pending jobs.
- **Reason, in words**: map Slurm's codes to a sentence —
  `Resources` → "waiting for free nodes", `QOSMaxGRESPerUser` → "you are at your QOS GPU
  limit", `Dependency` → "waiting for job N", `Priority` → "N jobs ahead of it".

## Acceptance criteria

- [ ] Pending jobs show estimated start, priority components and a plain-language reason.
- [ ] Running/terminated jobs do not show the section.
- [ ] `sprio`/`--start` unavailable or `N/A` degrades to a clear message, never a crash.
- [ ] One extra Slurm call per selection at most; nothing added to the poll loop.
- [ ] Tests: parsers for `sprio` and `--start`, reason-code mapping, degradation paths.

## Notes

Add `get_job_priority()` / `get_start_estimate()` to `slurm.py` next to `get_job_stats()`,
with pure parsers so they can be tested without a cluster. Not every cluster has
accounting enabled for `sprio`.
