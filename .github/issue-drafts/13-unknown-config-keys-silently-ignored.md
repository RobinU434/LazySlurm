---
title: "A misspelled config key is silently ignored"
labels: enhancement, config, ux
---

## Problem

`config.load()` returns whatever TOML it parsed, and each consumer picks out the
keys it knows:

```python
editor = saved.get("editor", "vim")
```

A key that is spelled wrong is simply never read. The user gets no error, no
warning, and a setting that does nothing — which is the hardest kind of config
problem to diagnose, because the file looks right.

```toml
reffresh = 2.0      # no effect, no complaint
editr = "nvim"      # no effect, no complaint
```

The same applies to a key that was valid in an older version and has since been
renamed.

## What to build

Compare the parsed keys against the known set and report the difference in the
command log at startup, next to the existing `config override` lines:

```
14:22:01 config file
  >>> ignoring unknown setting: reffresh
```

The set of known keys already exists implicitly in `__main__.py`'s `_CONFIG_KEYS`
plus the config-file-only settings just below it; those two lists could be merged
into one table that both the reader and this check consume.

Do **not** reject the file over an unknown key: one typo should not cost the user
every other setting they configured. Report and carry on.

Nested tables (`[partition_colors]`) should be checked by table name only.

## Notes

The Rust port does this (`src/startup.rs`); see `DIVERGENCES.md` §3, which also
records why rejecting the file was the wrong first attempt.
