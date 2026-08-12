---
title: "Find errors in large logs: jump-to-error, follow, highlighting"
labels: enhancement
---

## Problem

Logs here reach hundreds of MB. The panel tails the last 500 lines and `l` opens a pager,
but neither answers "where did it break?" when the traceback is 40 MB from the end.

## What to build

1. **Jump to last error** (`g` or `E`): scan backwards from EOF for
   `Traceback|Error|ERROR|CUDA out of memory|srun: error|slurmstepd:` and load a window
   around the last hit, with the matched line highlighted. Reuse the backwards
   block-reader in `slurm.tail_file()` — add a `find_last(path, pattern)` beside it.
2. **Follow mode** (`f`): keep the active log tab pinned to the end, re-tailing on each
   poll, like `tail -f`. Show `[following]` in the panel border title.
3. **Highlighting**: colour error/warning lines in the stdout/stderr tabs
   (`RichLog(highlight=True)` already there; add a regex-based line style).

## Acceptance criteria

- [ ] Jump-to-error on a >100 MB log returns in well under a second and reads only the
      tail region, not the whole file.
- [ ] No match reports "no errors found" rather than clearing the panel.
- [ ] Follow mode stops when leaving the tab or the job, and does not fight scrolling.
- [ ] Tests: backwards search across block boundaries, no-match, match on the first line.

## Notes

Keep the byte cap that `tail_file()` uses (`_TAIL_MAX_BYTES`) so a pathological log
cannot be read whole; report "searched the last N MB" when the scan stops early.
