---
title: "Collapse array jobs into a single row"
labels: enhancement, ui
---

## Problem

Every task of a job array gets its own row, so a 40-task array buries everything else.
`2736118_0 … 2736118_11` are twelve rows that all carry the same name, partition and
resources; only the state differs.

## What to build

One row per array, expandable:

```
▸ 2736118_[0-11]  sweep-lr   12 tasks: 3 run · 9 pend   gpu
  ├ 2736118_0     sweep-lr   RUNNING  1:12:04
  └ 2736118_1     sweep-lr   PENDING  0:00
```

- Group rows in `_BaseJobTable._rebuild()` (`src/lazyslurm/widgets/job_table.py`) by
  `config.base_job_id()`. Groups of one render exactly as today.
- Collapsed row shows the id range, the shared name/partition, and a state tally.
  Elapsed can show the max of running tasks.
- `Enter` (or `→`/`←`) toggles expansion; remember expanded ids across polls so a
  refresh does not collapse what the user opened.
- Actions on a collapsed row apply to the whole array: `c` cancels the base id
  (`scancel 2736118` cancels all tasks), `u` edits all pending tasks, `m` bookmarks the
  group. Actions on an expanded child keep today's per-task behaviour.
- Config switch `collapse_arrays = true` (default on) in `config.toml` + `models.Config`.

## Acceptance criteria

- [ ] An array of N tasks occupies one row until expanded.
- [ ] The tally is correct across mixed states (running / pending / completing).
- [ ] Expansion state survives a poll and a filter change.
- [ ] `c` on a collapsed row cancels the whole array with one `scancel`.
- [ ] Non-array jobs are unchanged.
- [ ] Tests: grouping, tally, expand/collapse, cancel target.

## Notes

`config.base_job_id()` already maps `123_11`, `123_[1-40]`, `123.batch` → `123`, and
`slurm.job_sort_key()` already keeps arrays together and ordered — build on both.
Pending arrays arrive from squeue as a single row (`123_[12-40]`) while running tasks
arrive individually; the grouping must merge those two shapes.
