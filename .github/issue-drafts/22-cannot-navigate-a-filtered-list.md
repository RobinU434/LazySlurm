---
title: "No keyboard way to browse a filtered job list"
labels: bug, ui, ux
---

## Problem

Filtering works, but there is no key that returns focus to the job tables while
**keeping** the filter. Every exit from the filter bar clears it:

- `Escape` — `on_key` clears the value, hides the bar, applies `""` and focuses
  the active table.
- `/` again — `action_toggle_search` does the same.
- `Enter` — no handler; nothing happens, focus stays in the input.

`Tab` does leave the input with the filter intact, but
`action_focus_next_right` only ever toggles between the detail and metadata
panels:

```python
def action_focus_next_right(self) -> None:
    if self._right_focus == "detail":
        self._right_focus = "metadata"
        metadata.query_one("TabbedContent").focus()
    else:
        self._right_focus = "detail"
        detail.query_one("TabbedContent").focus()
```

There is no case that focuses a job table, so once focus is on the right-hand
panels the only way back is the mouse.

While the input has focus, `Up`/`Down` do nothing either: `on_key`'s table
navigation is guarded on `active.has_focus` / `completed.has_focus`, and neither
is true.

## Why it matters

Filtering exists to narrow a long list down to the jobs you want to act on —
then cancel, edit or inspect them. At the moment you can narrow the list and
look at it, but the moment you try to move the cursor onto one of those rows the
filter is gone and the list is long again.

## Fix

Make `Enter` in the filter bar close it and keep the query:

```python
def on_input_submitted(self, event: Input.Submitted) -> None:
    if event.input.id == "search-input":
        event.input.display = False
        self._search_visible = False
        self.query_one("#active-jobs", ActiveJobTable).focus()
        # deliberately no _apply_filter("") — the filter stays
```

`Escape` keeps its current meaning, which is the useful pairing: `Enter` to
accept, `Escape` to abandon. The border title already shows `— 2/4 match` while
a filter is active, so it stays visible that one is in force.

Worth considering separately: `action_focus_next_right` never returning to the
left column is surprising in its own right.

## Notes

Found while porting to Rust, where `Enter` accepts and `Escape` abandons
(`src/ui/app.rs`), with both paths tested.
