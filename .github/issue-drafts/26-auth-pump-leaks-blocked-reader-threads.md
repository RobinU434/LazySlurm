---
title: "The SSH auth pump leaks a blocked thread per second and can lose a 2FA prompt"
labels: bug, remote
---

## Problem

`_drive_master_auth` reads the master's pty like this, once per loop iteration:

```python
# src/lazyslurm/ssh.py
try:
    chunk = await asyncio.wait_for(
        loop.run_in_executor(None, _read_fd, fd), timeout=1.0
    )
except asyncio.TimeoutError:
    continue  # nothing said yet — loop back and re-check the master
```

`asyncio.wait_for` cancels the *future* on timeout. It cannot cancel the
*thread*: `concurrent.futures.Future.cancel()` returns `False` once the callable
has started, and `_read_fd` is already blocked inside
`os.read(fd, 4096)`. The thread stays there until the fd produces data or closes.

So every second that ssh stays quiet leaks another executor thread, all of them
blocked reading the same descriptor.

## Two consequences

**1. A prompt can be swallowed, and the login then hangs.**

When ssh finally writes, the data goes to exactly one of the blocked readers —
POSIX does not say which. If it is one whose future was already cancelled,
`_read_fd` returns the text to a future nobody is awaiting and the chunk is
gone. The pump never matches the prompt, keeps looping, and the connection hangs
until `connect_timeout` (120 s) before reporting something unhelpful.

**2. The default executor fills up, and unrelated things stop working.**

`asyncio`'s default executor is `min(32, cpu_count + 4)` threads, and every
leaked reader occupies one permanently. `slurm.py` uses the *same* default
executor:

```python
return await asyncio.to_thread(os.path.isfile, path)     # _file_exists
return await asyncio.to_thread(tail_file, path, tail_lines)  # read_log_file
```

A slow login therefore degrades — and eventually blocks — log reading for the
rest of the session.

## When it happens

Only while ssh is silent for more than a second, which is exactly what a
two-factor login does: Duo push and telephony wait several seconds for the
user's phone, and slow PAM or a slow network do the same. In other words, it
bites hardest on the clusters this feature exists for, and not at all on the
passwordless ones most likely to be used while developing it.

Ten seconds of Duo silence is ten leaked threads and ten chances for the
passcode prompt to land on a dead one.

## Fix

**Minimal:** keep one read pending across iterations instead of starting a new
one each time.

```python
pending = None
while True:
    ...
    if pending is None:
        pending = asyncio.ensure_future(loop.run_in_executor(None, _read_fd, fd))
    done, _ = await asyncio.wait({pending}, timeout=1.0)
    if not done:
        continue                 # still nothing; the same read stays pending
    chunk = pending.result()
    pending = None
```

One thread, no leak, and no chunk can be delivered to a future nobody is
reading.

**Proper:** a pty master can be watched by the event loop directly, which
removes the executor from the picture altogether.

```python
os.set_blocking(fd, False)
queue: asyncio.Queue[str] = asyncio.Queue()
loop.add_reader(fd, lambda: queue.put_nowait(_read_fd(fd)))
...
chunk = await asyncio.wait_for(queue.get(), timeout=1.0)   # now safe to cancel
```

A queue read is safe to cancel because the data stays in the queue.

Remember `loop.remove_reader(fd)` in `_kill_master` either way.

## Related

While in there: `_master_alive()` spawns an `ssh -O check` process on **every**
iteration of this loop, so a 30-second login is ~30 subprocesses. Not a bug, but
the same restructuring makes it easy to check less often.

## Notes

Found while porting to Rust, which avoids it by construction: one reader thread
pumps the pty into a channel for the lifetime of the process, and the pump times
out on the *channel*, where an expired wait cannot consume anything
(`src/ssh/pty.rs`). Recorded in `DIVERGENCES.md`.
