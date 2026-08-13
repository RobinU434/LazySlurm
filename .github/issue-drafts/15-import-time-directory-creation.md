---
title: "Importing lazyslurm.slurm creates a directory in ~/.ssh"
labels: bug, code-health
---

## Problem

`src/lazyslurm/slurm.py` creates a directory at module scope:

```python
_SSH_CONTROL_DIR = os.path.join(os.path.expanduser("~"), ".ssh", "cm-lazyslurm")
try:
    os.makedirs(_SSH_CONTROL_DIR, mode=0o700, exist_ok=True)
except OSError:
    _SSH_CONTROL_DIR = ""
```

So `import lazyslurm.slurm` — for `--help`, for a unit test, for a docs build —
writes to the user's home directory. Side effects at import time are surprising
in general, and this one touches `~/.ssh`, which is a directory users and
security tooling watch closely.

It also runs on machines that will never use the feature: the directory is only
needed for local-mode SSH to compute nodes, which `--no-live` disables outright.

Note `ssh.py` already gets this right — its `_CONTROL_DIR` is a module constant
and `mkdir` happens inside `_start_master()`, at the point of use.

## Fix

Make the constant a plain path and create the directory on first use, inside
`_ssh_cmd()`:

```python
_SSH_CONTROL_DIR = Path.home() / ".ssh" / "cm-lazyslurm"

def _control_opts() -> list[str]:
    try:
        _SSH_CONTROL_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        return []
    return ["-o", "ControlMaster=auto", "-o", "ControlPersist=60s",
            "-o", f"ControlPath={_SSH_CONTROL_DIR / '%C'}"]
```

The existing fallback behaviour — drop the multiplexing options when the
directory cannot be made — is preserved.

## Related

While in there: `_SSH_OPTS` is built once at import from the same conditional, so
the two need to move together.
