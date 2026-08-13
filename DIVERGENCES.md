# Deliberate divergences from the Python

The port is faithful by default — see §11 of [RUST_PORT_PLAN.md](RUST_PORT_PLAN.md).
Everywhere it is not, the reason is recorded here.

Anything listed as "to fix in both" is a real bug found while porting; the Rust
side already behaves correctly and the Python should be brought in line.

---

## 1. `cache_max_age_days = 0` means "never prune"

**Python:** `cache_max_age_days = None` disables pruning, per the template's
`# (null = never)` comment.

**Rust:** `0` disables pruning; `None` is unreachable from the file.

**Why:** TOML has no null literal, so the documented "never" is not expressible
in the Python either — the comment describes something the user cannot actually
write. The remaining candidate value, `0`, would mean "delete everything older
than zero days", i.e. wipe the whole script archive on first launch. Making it
mean "never" both reaches the documented intent and removes a footgun.

**Affects:** `src/config/file.rs`.

---

## 2. Config writes preserve the file

**Python:** `config.save()` regenerates the file from a dict, discarding every
comment and any key it does not know about.

**Rust:** writes with `toml_edit`, so comments, ordering and unknown keys
survive.

**Why:** the shipped template is almost entirely comments documenting each
setting. Losing them the first time `--partition-order` is passed is a plain
regression, and format-preserving editing costs nothing.

**Affects:** `src/config/file.rs::save_partition_order`.

---

## 3. Unknown config keys are reported

**Python:** silently ignores any key it does not recognise.

**Rust:** applies every key it does recognise, then names the leftovers in the
command log — `config file: ignoring unknown setting reffresh`.

**Why:** a misspelled setting is the most likely config mistake and the hardest
to notice, because the symptom is "nothing happened". Note the Rust deliberately
does **not** reject the file: an earlier draft did, which meant one typo cost the
user every other setting — worse than the Python. Reporting without discarding is
the improvement; rejecting was not.

**Affects:** `src/config/file.rs`, `src/startup.rs`.

---

## 4. Empty `sacct` fields fall through to the next alternative

**Status: to fix in both.** This one is a Python bug the port inherited and then
caught.

`JobDetail.submit_line` is `raw.get("SubmitLine") or raw.get("Command")`. In
Python an empty string is falsy, so an empty `SubmitLine` correctly falls through
to `Command`. The first Rust version stopped at the first key that merely
*existed*, returning `""`.

That is fixed (`src/model/job.rs::first_of` skips empty values), and the Python
is right by accident of `or` semantics — but only for `submit_line`. The same
pattern in `JobDetail`'s other accessors uses `raw.get(a, raw.get(b, "N/A"))`,
which is **presence**-based and therefore returns `""` for an empty-but-present
`sacct` column. `Nodelist`, `ReqTRES` and `AllocTRES` are all routinely empty in
sacct output, so the Python's Resources tab can show a blank where the fallback
field has the answer.

**Action:** the Rust behaves correctly throughout. The Python's `JobDetail`
accessors should be changed from `raw.get(a, raw.get(b, "N/A"))` to a helper that
skips empty values.
