---
title: "Structured filters in the search bar"
labels: enhancement, ui
---

## Problem

`/` filters on a substring of id, name and partition. There is no way to ask for "all my
pending GPU jobs" or "everything that failed".

## What to build

Recognise `key:value` terms in the search input, combined with AND, alongside plain
substring terms:

| Term | Matches |
|------|---------|
| `state:pending`, `state:fail` | job state, prefix match, case-insensitive |
| `part:gpu` | partition |
| `name:train` | job name |
| `id:4815` | job id |
| `gpu:>0` | jobs requesting GPUs |

- Parse in a pure helper (`parse_query(text) -> list[Term]`) so it is testable, then
  apply in `_BaseJobTable._filter_match()`.
- Unknown keys fall back to substring matching, so nothing a user types can "break" it.
- Show the active filter and match count in the panel border title.

## Acceptance criteria

- [ ] `state:pend part:gpu` narrows to pending GPU jobs in both tables.
- [ ] Bare words keep today's behaviour; mixed `train state:run` works.
- [ ] Empty result shows "no jobs match" rather than an empty table.
- [ ] Tests: query parsing incl. malformed input, matching per key, AND semantics.
