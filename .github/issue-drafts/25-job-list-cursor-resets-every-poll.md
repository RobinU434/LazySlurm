---
title: "The job list on the partition and node screens jumps to the top every refresh"
labels: bug, ui
---

## Problem

`PartitionTable` and `NodeTable` both take care to keep the cursor on the same
row across a refresh:

```python
# src/lazyslurm/widgets/partition_view.py
def update_partitions(self, partitions: list[PartitionInfo]) -> None:
    """Rebuild the table, keeping the cursor on the same partition."""
    selected = self.get_selected_partition()
    self.clear()
    ...
    if selected:
        for index, part in enumerate(partitions):
            if part.name == selected:
                self.move_cursor(row=index)
                break
```

`PartitionJobTable`, seventy lines further down the same file, does not:

```python
def update_jobs(self, jobs: list[PartitionJob]) -> None:
    self._jobs = jobs
    self.clear()
    for job in jobs:
        self.add_row(*self._row_for(job), key=job.job_id)
```

`clear()` resets the cursor to row 0.

## Reproduce

1. Press `p` for the partition monitor.
2. `Tab` into the lower pane and scroll down the job list.
3. Wait for the refresh interval — five seconds by default.

The cursor jumps back to the first row. On a busy partition with hundreds of
jobs the pane cannot be browsed at all: every scroll is undone before you finish
reading.

The path is `PartitionScreen.on_mount` → `set_interval(refresh, _refresh_partitions)`
→ `_refresh_jobs` → `update_jobs`. `NodeScreen` has the same shape, so the node
view's job list behaves the same way.

`UsageTable.update_rows` has the identical pattern. It is much less noticeable
because usage only reloads on `r` and `w`, never on a timer — but it is the same
bug and worth fixing in the same pass.

## Fix

Give both the treatment `PartitionTable` already has. The row key is the job id
(or the user, for usage), so it is already there to search on:

```python
def update_jobs(self, jobs: list[PartitionJob]) -> None:
    self._jobs = jobs
    selected = self._selected_key()
    self.clear()
    for job in jobs:
        self.add_row(*self._row_for(job), key=job.job_id)
    if selected:
        try:
            self.move_cursor(row=self.get_row_index(selected))
        except Exception:
            pass
```

Better still, factor the preserve-restore out: four tables now want it, and the
main job tables have a fifth implementation of it inside `_apply_diff`.

## Notes

Found while porting to Rust, where all four ungrouped tables share one
`SimpleTable` that keys the cursor by row rather than index
(`src/ui/simple_table.rs`), with a test for exactly this case.
