---
title: "cache_max_age_days documents a null value that TOML cannot express"
labels: bug, config, docs
---

## Problem

The template offers:

```toml
# cache_max_age_days = 30    # auto-delete cached job info older than N days (null = never)
```

TOML has no null literal. There is no value a user can write that reaches the
`None` branch — `cache_max_age_days = null` is a parse error, and omitting the
key gives the default of 30, not "never".

So the documented way to disable pruning does not exist. The loader's `None`
handling is only reachable from Python:

```python
# src/lazyslurm/__main__.py
raw_cache_age = saved.get("cache_max_age_days", 30)
cache_max_age_days = None if raw_cache_age is None else int(raw_cache_age)
```

Worse, the value a user is most likely to try instead is `0`, which is currently
taken literally: `prune_script_cache(0)` deletes every archived script whose
mtime is at or before now — i.e. the entire archive, on the next launch.

## Fix

Give "never" a representable value and make the destructive reading impossible:

```python
raw = saved.get("cache_max_age_days", 30)
cache_max_age_days = None if raw in (None, 0) else int(raw)
```

Update the template comment to `0 = never`. Optionally accept `false` too, which
some users will try.

## Notes

The Rust port treats `0` as "never" for this reason; see `DIVERGENCES.md` §1.
