---
title: "Saving config destroys the commented template"
labels: bug, config
---

## Problem

`config.save()` regenerates `config.toml` from a dict:

```python
# src/lazyslurm/config.py
def save(data: dict) -> None:
    lines: list[str] = []
    for key, value in data.items():
        ...
    CONFIG_FILE.write_text("\n".join(lines) + "\n")
```

Everything not in that dict is gone: every comment, the original ordering, and
any key the loader does not recognise.

The file it overwrites is `templ/config.toml`, which is 41 lines of which ~38 are
comments documenting each setting and its valid values. That documentation is the
only in-product reference for options like `partition_colors`.

## Reproduce

```bash
lazyslurm --partition-order gpu,cpu     # persists the order, via save()
cat ~/.config/lazyslurm/config.toml     # the template is now three lines
```

`set_partition_colors` has the same effect.

## Fix

Edit the file rather than regenerating it. `tomlkit` and `toml_edit`-style
libraries preserve comments and layout; `tomlkit` is a pure-Python option that
fits the existing dependency profile:

```python
import tomlkit

def save(data: dict) -> None:
    doc = tomlkit.parse(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else tomlkit.document()
    for key, value in data.items():
        doc[key] = value
    CONFIG_FILE.write_text(tomlkit.dumps(doc))
```

That also removes the hand-rolled `_toml_value()` serialiser, which currently
handles only `str`, `bool` and `list` and would silently emit invalid TOML for
anything else (a `None` becomes a bare `None`).

If adding a dependency is unwelcome, the narrower fix is to have `save()` write
only into a file it created and otherwise refuse — but preserving the user's file
is the behaviour worth having.

## Notes

The Rust port uses `toml_edit` for exactly this reason; see `DIVERGENCES.md` §2.
