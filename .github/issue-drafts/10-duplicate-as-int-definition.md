---
title: "slurm.py defines _as_int twice; the first definition is dead"
labels: bug, code-health
---

## Problem

`src/lazyslurm/slurm.py` defines `_as_int` at two different points in the module:

```python
# line 898 — used by get_job_stats, just above it
def _as_int(value: str | None) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0
```

```python
# line 1428 — used by parse_sinfo_nodes, just above it
def _as_int(value: str) -> int:
    try:
        return int(float(value.strip()))
    except (ValueError, AttributeError):
        return 0
```

Python binds the name once, at module level, top to bottom — so the second
definition wins for **every** caller, including the stats code that sits next to
the first one and was written against it.

The two behave the same on the inputs that actually occur today, which is why
nothing has broken. That is what makes it worth fixing now rather than after it
does: anyone reading `get_job_stats` will reason about the definition directly
above it, which is not the code that runs.

They are also not interchangeable in general:

| Input | Line 898 | Line 1428 (the one that runs) |
|---|---|---|
| `"8"` | 8 | 8 |
| `"8.0"` | 0 | 8 |
| `None` | 0 | 0 (via `AttributeError`) |
| `"1e3"` | 0 | 1000 |

## Fix

Keep one definition near the top of the module. The float-tolerant version is the
right one to keep — Slurm reports `CPUsLoad` as `16.02` and truncating is
intended. Delete the other and confirm the callers of the deleted one still read
correctly.

Worth a quick grep for the same pattern elsewhere; `parse_mem_bytes` and
`parse_duration` are defined once in `models.py` and imported, which is the shape
to follow.
