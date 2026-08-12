---
title: "Account usage panel (sreport)"
labels: enhancement
---

## Problem

Nothing in the tool shows how much of the allocation has been consumed — you find out
from a separate `sreport` invocation, or not at all until jobs start being deprioritised.

## What to build

A screen (suggest `U`) summarising the user's and the account's usage:

- `sreport cluster AccountUtilizationByUser start=<month-start> end=now -t hours -P`
  → per-user hours within your account, and your share of it.
- `sshare -U -P -o Account,User,RawShares,NormShares,RawUsage,EffectvUsage,FairShare`
  → fairshare position, which is what actually determines queue priority.
- Show: hours used this month, top consumers in the account, your fairshare factor with
  a plain reading ("below your share — your jobs get boosted priority").
- Time window selectable: this month / last 30 days / this year.

## Acceptance criteria

- [ ] Renders on clusters with accounting enabled; a clear message where it is not.
- [ ] `-P` (parsable) output parsed without assuming column widths.
- [ ] Fetched on open and on `r`, never in the poll loop.
- [ ] Tests: parsers for `sreport` and `sshare` output, absent-accounting path.

## Notes

`sreport` can be slow (seconds); load asynchronously with a "loading…" placeholder so
the screen opens immediately.
