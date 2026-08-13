# Manual test checklist

Everything here needs a real cluster, a real terminal, or a real person — which
is why none of it is in `cargo test`. Walk it before tagging a release.

The automated suite covers parsing, state transitions, key dispatch, generated
reports and layout. What it cannot cover is whether the commands actually work
against Slurm, and whether the terminal survives.

**Record the result.** Copy this file, tick as you go, and note anything odd —
including things that worked but looked wrong.

---

## 1. Local mode, on a login node

| | Check | Expected |
|---|---|---|
| ☐ | `lazyslurm` starts | Both job tables populate within a refresh interval |
| ☐ | The cluster bar | Your name, running and pending counts, partition availability |
| ☐ | Counts match `squeue -u $USER \| wc -l` | Bear in mind the bar counts array **tasks**, so a pending `123_[1-9]` row is 9 |
| ☐ | Terminated table matches `sacct` for the same window | Sub-steps (`123.batch`) absent, running jobs absent |
| ☐ | Selecting a job fills the right-hand panels | Within ~200 ms of the cursor settling |
| ☐ | Arrowing quickly through 20 jobs | Only the job you land on is loaded — the debounce is doing its job |
| ☐ | A job with no logs yet | "(no log file path available)" or "(file not found: …)", not a hang |

## 2. Job tables

| | Check | Expected |
|---|---|---|
| ☐ | `↑`/`↓` past the end of one table | Focus moves to the other, entering from the side you were travelling towards |
| ☐ | Wait through several refreshes with the cursor mid-list | The cursor stays on the same **job**, not the same row number |
| ☐ | A job array | One `▸ 123_[0-11] ×12` row with a state tally |
| ☐ | `Enter` on it, then wait for a refresh | Stays expanded |
| ☐ | `m` on a collapsed array | The whole group pins to the top |
| ☐ | `/`, type `state:pend`, `Enter` | Bar closes, filter stays, title shows `— n/m match`, list is navigable |
| ☐ | `/`, type something, `Escape` | Filter abandoned |
| ☐ | A filter matching nothing | "no jobs match" placeholder, and `c` on it does nothing |
| ☐ | A job name with non-ASCII characters | Columns stay aligned |

## 3. Detail and metadata panels

| | Check | Expected |
|---|---|---|
| ☐ | stdout on a job writing output | Follows the newest lines |
| ☐ | Scroll up, wait for a refresh | Stays where you put it |
| ☐ | Scroll back to the bottom | Resumes following |
| ☐ | A log of several hundred MB | Opens promptly — only the tail is read |
| ☐ | A log that is one enormous line | Truncation banner, not a freeze |
| ☐ | `stats` on a finished job | Efficiency block; numbers agree with `seff <jobid>` |
| ☐ | `stats` on a multi-node job | Memory shows `/node` |
| ☐ | `stats` on a job with GPUs | GPU line, with the "not recorded by Slurm" note |
| ☐ | A **pending** job | Metadata grows a Pending tab; `(`/`)` cycles through it |
| ☐ | The same job once running | Pending tab disappears and the cycle skips it |
| ☐ | Pending tab on a blocked job | Reason in plain language, and the raw code beneath |
| ☐ | Priority breakdown | `#n of m pending in <partition>`, factors summing sensibly |
| ☐ | On a cluster without `sprio` | Says the cluster does not run it — not an empty panel |
| ☐ | `cpu` tab on a running job | Process listing from the node, refreshing while open |
| ☐ | `gpu` tab on a GPU job | Only the job's GPUs, with `CUDA_VISIBLE_DEVICES` in the header |
| ☐ | Switch away from `cpu` | Fetching stops (check with `ps` on the node, or trust the log) |

## 4. Actions — **use a scratch job**

| | Check | Expected |
|---|---|---|
| ☐ | `c` on a job, then `n` | Nothing happens |
| ☐ | `c`, then `y` | Job cancelled; the tables refresh |
| ☐ | `Shift+C` | Cancels immediately, no confirmation |
| ☐ | `c` on a collapsed array | Confirms once, and `scancel <base>` takes every task |
| ☐ | `Ctrl+V`, `↓` a few times, `c` | Confirmation lists them; all are cancelled |
| ☐ | `u` on a **running** job | Refuses, and says only pending jobs can be edited |
| ☐ | `u` on a pending job, change the runtime, `Ctrl+S` | `scontrol update` runs; the change shows on the next refresh |
| ☐ | `u`, change nothing, `Ctrl+S` | Nothing is sent |
| ☐ | `s` on a terminated job | Confirms with the original submit line; resubmits |
| ☐ | `b` on a recent job | Script opens read-only in your editor |
| ☐ | `b` on a job older than `MinJobAge`, first seen while running | Still opens — from the archive |
| ☐ | `b` on an old job never seen while running | Says it is unavailable and why |

## 5. Shell-outs — **the terminal must survive every one**

After each: does the TUI redraw correctly, is the cursor visible, does `Ctrl+C`
still work in your shell afterwards?

| | Check | Expected |
|---|---|---|
| ☐ | `e` — stdout in your editor | Opens; quitting returns to a correct screen |
| ☐ | `Shift+E` — stderr | Same |
| ☐ | `l` — pager on a huge log | Opens at the end; `/` searches, `F` follows |
| ☐ | `l` with `pager = "bat"` configured | Correct flags |
| ☐ | `o` — SSH to the compute node | Shell on the node; `exit` returns |
| ☐ | `,` — config in your editor | Opens the template on first use; edits apply on close |
| ☐ | Change `refresh` via `,` | The command log reports the change |
| ☐ | An editor that does not exist in the config | Says so; does not suspend into nothing |
| ☐ | Kill the terminal while suspended | Recoverable shell (`reset` if not) |

## 6. Full-screen panels

| | Check | Expected |
|---|---|---|
| ☐ | `p` — partition monitor | Every partition, load bars, `A/I/O/T` counters |
| ☐ | A down partition | Struck through, `[down]` instead of a bar |
| ☐ | `Tab` into the job list, scroll, wait for a refresh | Cursor stays put |
| ☐ | `Enter` on a partition | Its nodes; drained nodes show `—` for load and their reason |
| ☐ | `Escape` from nodes | Back to partitions, not all the way out |
| ☐ | `Shift+U` — usage | Loads (may take seconds); your row marked `▸` |
| ☐ | `w` | Window cycles month → 30 days → year, reloading each time |
| ☐ | Fair-share block | Factor and the sentence explaining it |
| ☐ | On a cluster without accounting | Says accounting is not enabled |

## 7. Remote mode — **the least-tested path**

Run from a laptop against a real login node.

| | Check | Expected |
|---|---|---|
| ☐ | `lazyslurm --remote user@host` with keys only | Connects; tables populate |
| ☐ | With a password | Dialog appears with the server's own prompt text, masked |
| ☐ | With 2FA | Both prompts appear in order and are accepted |
| ☐ | Escape at the prompt | Reports cancelled; `r` retries |
| ☐ | A wrong code | Reports the server's own error, not a generic one |
| ☐ | Once connected, use it for several minutes | No further prompts — one connection throughout |
| ☐ | `ps` on the laptop | **One** ssh master, not one per command |
| ☐ | Drop the network, then `r` | Reconnects, re-authenticating only if the master died |
| ☐ | `l` on a huge log | Pager runs **on the cluster** — no long copy |
| ☐ | `e` on a log | File is fetched and opens; temp file removed after |
| ☐ | A log path containing a space | Still opens |
| ☐ | `o` to a compute node | Hops through the existing connection — no second 2FA prompt |
| ☐ | `cpu`/`gpu` tabs | Work; the hop happens on the login node |
| ☐ | Quit | The ssh master is gone (`ps`), no stray processes |

## 8. Terminal and environment

| | Check | Expected |
|---|---|---|
| ☐ | Resize the window while running | Relayouts cleanly |
| ☐ | An 80×24 terminal | Usable; nothing overlaps |
| ☐ | A very small terminal (40×12) | Does not panic |
| ☐ | `TERM=xterm-256color` and `TERM=screen` | Both render |
| ☐ | Inside `tmux` | Colours and keys work; `Ctrl+V` reaches the app |
| ☐ | A light-background terminal | Readable |
| ☐ | `q` | Terminal restored: cursor visible, echo on, no alternate screen |
| ☐ | Force a panic (if you can) | Message visible, terminal still restored |
| ☐ | A job finishing while you watch | Desktop notification and a bell, if `notify-send` exists |
| ☐ | On a login node | The warning about resource usage appears |

## 9. Packaging

| | Check | Expected |
|---|---|---|
| ☐ | `pip install` the CI wheel in a clean venv on a login node | `lazyslurm` on PATH and runs |
| ☐ | The oldest login node you support | glibc new enough (manylinux2014 = 2.17) |
| ☐ | `uv tool install lazyslurm-rs` | Works |
| ☐ | `pip install --no-binary` without Rust | Fails with a clear toolchain error |
| ☐ | `lazyslurm --version` | Matches the tag |

---

## Sign-off

| Field | |
|---|---|
| Version | |
| Cluster(s) | |
| Slurm version | |
| Tested by / date | |
| Outstanding issues | |
