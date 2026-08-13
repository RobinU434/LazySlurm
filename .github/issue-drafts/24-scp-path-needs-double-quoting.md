---
title: "Opening a remote log fails when its path contains a space"
labels: bug, remote
---

## Problem

`_open_in_editor` copies the log down with `scp`, quoting the remote path once:

```python
# src/lazyslurm/app.py
rc = os.system(
    f"scp -q {self._ssh_control_opt()} "
    f"{self.config.remote}:{shlex.quote(path)} {shlex.quote(tmp.name)}"
)
```

A remote `scp` path is interpreted by **two** shells: the local one running the
command, and the remote one that `scp` invokes to expand the path.
`shlex.quote` satisfies only the first. The remote shell then re-splits on
whitespace.

## Reproduce

A job whose output lands in a directory with a space:

```
/work/my runs/slurm-4815.out
```

- `shlex.quote` produces `'/work/my runs/slurm-4815.out'`
- the local shell strips the quotes; `scp` receives `host:/work/my runs/slurm-4815.out`
- the remote shell splits that into `/work/my` and `runs/slurm-4815.out`

`scp` fails, `rc != 0`, and `e` reports "failed to fetch remote file" with no
indication that the path was the problem.

## Fix

Quote for both shells:

```python
remote_path = shlex.quote(shlex.quote(path))
rc = os.system(
    f"scp -q {self._ssh_control_opt()} "
    f"{shlex.quote(self.config.remote)}:{remote_path} {shlex.quote(tmp.name)}"
)
```

Note `self.config.remote` is also unquoted today; it comes from the command line
so it is lower risk, but there is no reason to leave it.

`scp -O` (legacy protocol) or `sftp` avoid the remote-shell expansion entirely
and would be a more thorough fix if the dependency is acceptable.

## Related

`_page_file`, forty lines away in the same file, already handles this correctly:

```python
cmd = (
    f"ssh -t {self._ssh_control_opt()} {shlex.quote(self.config.remote)} "
    + shlex.quote(" ".join(shlex.quote(a) for a in [pager, *flags, path]))
)
```

Inner quoting per argument, then the whole command quoted again for the remote
shell. The `scp` call is the odd one out.

See also the companion issue about `read_log_file`'s unquoted path.
