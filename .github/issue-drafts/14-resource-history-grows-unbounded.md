---
title: "Sparkline history grows unbounded across a long session"
labels: bug, performance
---

## Problem

`_resource_history` accumulates one entry per job the user has ever selected, and
nothing ever removes them:

```python
# src/lazyslurm/app.py
self._resource_history: dict[str, dict[str, list[float]]] = {}
...
hist = self._resource_history.setdefault(job_id, {"memory": [], "cpu": []})
```

Each entry is capped at `_MAX_HISTORY` (60) samples per series, so a single job
is bounded — but the number of *jobs* is not. A user who leaves LazySlurm open
for a week on a busy account, arrowing through job lists, accumulates an entry
for every job they ever highlighted, including ones that finished days ago.

The samples are also useless once a job ends: the panel only plots history for a
job that is currently running.

## Fix

Evict alongside the poll loop, where the set of live jobs is already known. In
`_poll_jobs`, after `current_ids` is computed:

```python
self._resource_history = {
    job_id: history
    for job_id, history in self._resource_history.items()
    if job_id in current_ids
}
```

A job that finishes loses its history, which matches what the panel does with it.

If keeping history briefly after completion turns out to be wanted, cap the dict
with an LRU instead — but the simple eviction is the right default.
