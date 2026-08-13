---
title: "JobDetail shows a blank where the fallback field has the answer"
labels: bug
---

## Problem

`JobDetail`'s accessors fall back between the scontrol and sacct spellings of a
field using `dict.get`'s default argument:

```python
# src/lazyslurm/models.py
def node_list(self) -> str:
    return self.raw.get("NodeList", self.raw.get("Nodelist", "N/A"))
```

The default only applies when the key is **absent**. sacct emits empty columns
rather than omitting them, so a key that is present-but-empty wins and the
accessor returns `""` — the fallback is never consulted.

This affects eleven accessors: `node_list`, `num_cpus`, `num_nodes`, `memory`,
`time_limit`, `run_time`, `submit_time`, `start_time`, `end_time`, `state` and
`tres`.

`submit_line` is correct by accident, because it uses `or` rather than `get`:

```python
return self.raw.get("SubmitLine") or self.raw.get("Command") or "N/A"
```

## Reproduce

Select a terminated job old enough that `scontrol` no longer knows it, so the
detail comes from `_get_job_detail_sacct`. Any job whose `sacct` row has an empty
`NodeList`, `ReqTRES` or `AllocTRES` column — routine for cancelled and failed
jobs — shows a blank in the Resources tab instead of the value the other field
holds.

## Fix

Add a helper that skips empty values and use it for every alternative-name
lookup:

```python
def _first_of(self, *keys: str) -> str:
    for key in keys:
        value = self.raw.get(key)
        if value:
            return value
    return "N/A"
```

Then `node_list` becomes `self._first_of("NodeList", "Nodelist")`, and
`submit_line` can use it too rather than a hand-rolled `or` chain.

## Notes

Found while porting to Rust; the Rust implementation does this
(`src/model/job.rs::first_of`) and has regression tests for the empty-field case.
