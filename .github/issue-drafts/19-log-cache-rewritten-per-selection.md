---
title: "The log cache is read and rewritten in full on every job selection"
labels: performance
---

## Problem

`cache_job_paths` is called from `get_job_detail`, i.e. every time the user moves
the cursor to a different job. Each call reads the whole cache file, mutates one
entry and writes the whole file back:

```python
# src/lazyslurm/config.py
def cache_job_paths(job_id, ...):
    cache = _load_log_cache()      # full JSON parse
    entry = cache.get(job_id, {})
    ...
    _save_log_cache(cache)         # full JSON serialise + fsync-ish rename
```

The cache holds one entry per job seen in the last 30 days. On a busy account
that is thousands of entries, parsed and re-serialised on every arrow-key press
that lands on a new job — on a shared filesystem, where the write is the
expensive part.

The selection debounce (200 ms) limits the rate but not the cost.

## Why it is written this way

The read-modify-write is deliberate: two LazySlurm sessions can share the cache,
and re-reading before each write means neither loses the other's entries. That
property is worth keeping.

## Options

1. **Keep it in memory, write on a timer.** Load once at startup, mutate in
   memory, flush every N seconds and on exit. Merge on flush by re-reading first,
   preserving the multi-session property. Loses at most N seconds of cache on a
   hard kill, which costs a guessed log path — not correctness.

2. **Write only when something changed.** Most selections re-cache values
   identical to what is already stored. Comparing before writing would eliminate
   the majority of writes for a few lines:

   ```python
   if entry == cache.get(job_id):
       return
   ```

3. **Append-only log, compacted on prune.** More work; only worth it if the cache
   grows far beyond current sizes.

Option 2 is the cheap fix and probably enough; option 1 is the thorough one.

## Related

`prune_log_cache` already only writes when it actually dropped something, which
is the pattern to follow.
