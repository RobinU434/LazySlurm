---
title: "The CPU sparkline in the stats tab actually plots memory"
labels: bug, ui
---

## Problem

The stats tab renders two sparklines, labelled `Memory:` and `CPU:`. Both are fed
memory readings.

```python
# src/lazyslurm/app.py, _collect_resource_sample
mem = parse_mem_bytes(stats.max_rss)
...
# Parse CPU: try total_cpu as seconds-like value, or just count samples
# For sparklines, use max_rss as primary metric; CPU is harder to normalize
# Use ave_rss as a proxy for CPU activity (non-zero means active)
cpu_val = parse_mem_bytes(stats.ave_rss)
if cpu_val is not None:
    hist["cpu"].append(cpu_val)
```

So the "CPU" series is `AveRSS` — average resident memory — while the "Memory"
series is `MaxRSS`. The comment is honest that this was a stand-in, but the label
the user sees says CPU, and two memory series plotted next to each other look
like corroborating evidence when they are the same measurement twice.

## What to do

Either plot CPU or do not claim to.

**Option A — plot real CPU.** `TotalCPU` is cumulative, so the per-sample delta
divided by the elapsed delta gives core-equivalents busy over that interval,
which is the same quantity the Efficiency block already reports as `cpu_used`:

```
cpu_now = (TotalCPU[n] - TotalCPU[n-1]) / (Elapsed[n] - Elapsed[n-1])
```

Normalise against `AllocCPUS` for a 0–1 series. Both fields are already fetched.

**Option B — relabel.** Rename the series to `Avg RSS:` and `Max RSS:`, which is
what they are, and drop the CPU claim.

Option A is the useful one: a flat CPU sparkline against a rising memory one is
exactly the picture that tells a user their job is stalled.

## Files

- `src/lazyslurm/app.py` — `_collect_resource_sample`
- `src/lazyslurm/widgets/detail_view.py` — `load_stats`, the Resource History block
