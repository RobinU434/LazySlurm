---
title: "parse_sinfo drops a partition's GRES when one spec is a substring of another"
labels: bug
---

## Problem

`parse_sinfo` aggregates the rows `sinfo --summarize` emits per node
configuration, and concatenates their distinct GRES strings:

```python
# src/lazyslurm/slurm.py
elif gres and gres not in part.gres:
    part.gres = f"{part.gres},{gres}" if part.gres else gres
```

`gres not in part.gres` is a **substring** test on the accumulated string, not a
membership test over the specs already collected. A spec that is a prefix of one
already present is silently dropped.

## Reproduce

A partition with two node configurations, one with 8 GPUs and one with 80:

```
gpu|up|4/0/0/4|...|gpu:a100:80
gpu|up|2/0/0/2|...|gpu:a100:8
```

Expected `gpu:a100:80,gpu:a100:8`; actual `gpu:a100:80`, because
`"gpu:a100:8" in "gpu:a100:80"` is `True`.

The same happens for `gpu:1` against an accumulated `gpu:16`, which is the more
likely real-world shape.

The node counts are unaffected — only the GRES column of the partition monitor
under-reports.

## Fix

Track the specs as a list and test membership properly:

```python
if gres and gres not in part.gres.split(","):
    part.gres = f"{part.gres},{gres}" if part.gres else gres
```

Or accumulate into a `list[str]` on `PartitionInfo` and join for display, which
avoids re-parsing a string that was built from a list.

## Test

`tests/test_slurm_parsing.py::test_parse_sinfo_aggregates_rows_per_partition`
already covers the working case (`gpu:a100:8` + `gpu:a100:9`); add a case where
one spec is a prefix of the other.
