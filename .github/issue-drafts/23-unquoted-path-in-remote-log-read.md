---
title: "Remote log reading breaks on a path containing an apostrophe"
labels: bug, remote
---

## Problem

`read_log_file` builds the remote command by interpolating the path twice — once
quoted, once not:

```python
# src/lazyslurm/slurm.py
cmd = f"tail -n {tail_lines} {shlex.quote(path)} 2>/dev/null || echo '(file not found: {path})'"
```

The first `path` is safe. The second sits **inside single quotes with no
quoting of its own**, so a path containing `'` closes the quote early and the
rest of the path is handed to the remote shell as commands.

## Reproduce

A job whose working directory contains an apostrophe — a directory named after a
person is the usual way this happens:

```
/work/bens'runs/slurm-4815.out
```

The command becomes:

```sh
tail -n 500 '/work/bens'"'"'runs/slurm-4815.out' 2>/dev/null || echo '(file not found: /work/bens'runs/slurm-4815.out)'
                                                                                        ^ quote closes here
```

The `echo` argument ends early, `runs/slurm-4815.out)` is parsed as a further
word, and the shell reports an error instead of the log. In remote mode the log
tab shows nothing and never explains why.

## Severity

The path comes from the user's own job (`scontrol StdOut`, which comes from
their own `sbatch --output`), so this is self-inflicted rather than a way to
attack another user. It is still a command-injection shape, and it is a plain
correctness bug for paths that are entirely legitimate.

## Fix

Quote it, or better, keep the message out of the remote shell entirely:

```python
cmd = f"tail -n {tail_lines} {shlex.quote(path)}"
stdout, _, rc = await _run_remote(cmd)
return stdout if stdout.strip() else f"(file not found: {path})"
```

That is also clearer: the "file not found" text is a local UI string and has no
business being generated on the cluster.

## Related

`_page_file` in `app.py` gets the equivalent case right — it quotes each
argument and then quotes the whole command line again for the remote shell.
Worth grepping for other single-quoted f-string interpolations while fixing this.
