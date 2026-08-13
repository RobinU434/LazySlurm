---
title: "Fifteen bare `except Exception` handlers hide real failures"
labels: code-health
---

## Problem

There are 15 `except Exception` sites across `src/lazyslurm/`, most of them
swallowing silently. They fall into three groups.

**Genuinely defensive — fine as they are.** Textual widget queries that may run
before mount, e.g. `DetailView.load_gpu` guarding a tab that does not exist when
`--no-gpu` is set. These are load-bearing and cheap.

**Hiding a real error.** The archive call in `get_job_detail`:

```python
try:
    await archive_batch_script(job_id)
except Exception:
    pass
```

A failure here means the batch script was not archived — the one window in which
it *could* be — and the user finds out days later when `b` reports nothing
available. The intent (never let archiving break detail loading) is right; the
silence is not. It should log to the command log.

**Papering over an interface.** `_apply_diff` and `_row_keys` in `job_table.py`
wrap `coordinate_to_cell_key` / `get_cell` / `get_row_index` in bare handlers
because the DataTable API raises on states that are hard to rule out. Each of
those hides a genuine bug in row-key handling if one ever occurs.

## What to do

Not "remove them all" — most are deliberate. Rather:

1. Narrow each to the exception actually expected (`NoMatches` for widget
   queries, `CellDoesNotExist`/`RowDoesNotExist` for the DataTable ones).
2. Where the operation failing is meaningful to the user, log it rather than
   `pass` — the archive call above is the clear case.
3. Add a short comment to the ones that stay broad, saying what they are
   defending against, so the next reader does not have to reconstruct it.

## Files

`slurm.py` (1), `app.py` (3), `widgets/job_table.py` (4),
`widgets/detail_view.py` (3), `widgets/metadata_view.py` (2),
`widgets/partition_view.py` (2), `config.py` (2, both `load()` guards that are
fine).
