---
title: "Column truncation counts characters, not terminal columns"
labels: bug, ui
---

## Problem

`_truncate` measures width with `len()`:

```python
# src/lazyslurm/widgets/job_table.py
def _truncate(text: str, max_width: int) -> str:
    if max_width <= 0 or len(text) <= max_width:
        return text
    return text[:max_width - 1] + "…"
```

`len()` counts code points. A terminal lays out **display columns**, and the two
differ for a good deal of valid text:

| Text | `len()` | Columns |
|---|---|---|
| `実験-sweep` | 8 | 12 |
| `éxperiment` (combining acute) | 11 | 10 |
| `🚀-run` | 5 | 6 |

So a job name containing CJK, emoji, or combining marks is either cut too late —
overflowing its column and pushing everything to its right out of alignment — or
cut too early, wasting columns.

Slurm imposes no restriction on job names; `sbatch -J 実験-sweep job.sh` is
accepted, and non-ASCII names are not unusual on shared clusters.

## Where it bites

`_truncate` is called for the name and partition columns of both job tables, and
from `partition_view.py` for node reasons, GRES strings and job names in the
partition and node monitors. The GRES and reason columns are ASCII in practice;
the name columns are the exposure.

## Fix

Measure with `wcwidth`, which is already an indirect dependency (Rich vendors
`cell_len` for exactly this):

```python
from rich.cells import cell_len, set_cell_size

def _truncate(text: str, max_width: int) -> str:
    if max_width <= 0 or cell_len(text) <= max_width:
        return text
    return set_cell_size(text, max_width - 1) + "…"
```

`rich.cells.set_cell_size` already handles the "do not split a wide character in
half" case, which a naive slice does not.

## Test

```python
def test_truncate_counts_columns_not_characters():
    assert _truncate("実験実験", 5) == "実験…"      # 4 columns + the ellipsis
    assert cell_len(_truncate("実験実験", 4)) <= 4  # never overflows
```

## Notes

Found while porting to Rust, which uses `unicode-width` for this
(`src/ui/text.rs`) and has the above cases as tests.
