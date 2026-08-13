"""A single, long-lived SSH session that every remote command runs through.

Remote mode used to spawn one `ssh host <cmd>` per Slurm call. That re-runs
authentication for every command unless the keys are passwordless, which makes
it unusable on clusters with two-factor authentication — and even with
multiplexing it forks an ssh client per command.

Here the app instead opens **one** connection at startup:

1. `SSHSession.connect()` starts an SSH *master* (`-M -N`) attached to a pty.
   Because it owns a pty, ssh writes its password / verification-code prompts
   there instead of the terminal, so they can be forwarded to a callback (the
   TUI shows a modal) and the answer typed back. This is where 2FA happens —
   once, at startup.
2. Once the master is up, a *shell channel* is opened over it
   (`ssh <host> /bin/sh -s`, multiplexed onto the master's connection, so no
   second authentication). It stays open for the life of the app.
3. `SSHSession.run()` writes a command into that shell's stdin and reads the
   output back, framed by unique markers. No new process, no new connection,
   no re-authentication per command.

The channel is serialized by a lock: commands queue rather than interleave. If
it dies (network drop, remote logout) the next `run()` transparently restarts
it, re-authenticating only if the master is gone too.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import pty
import re
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path

# A prompt is anything ssh/PAM asks for interactively. Matched against the tail
# of what the master has written to its pty, so it must anchor at the end.
_PROMPT_PATTERNS = [
    r"password[^\n]*:\s*$",
    r"passphrase[^\n]*:\s*$",
    r"passcode[^\n]*:\s*$",
    r"verification code[^\n]*:\s*$",
    r"one[- ]time password[^\n]*:\s*$",
    r"\botp\b[^\n]*:\s*$",
    r"token[_ ]?response[^\n]*:\s*$",
    r"\bpin\b[^\n]*:\s*$",
    r"duo[^\n]*:\s*$",
    r"\(yes/no(?:/\[fingerprint\])?\)\?\s*$",
    # Generic PAM fallback: "Enter <something>:" / "<something> for user:"
    r"^\s*enter [^\n]*:\s*$",
]
_PROMPT_RE = re.compile("|".join(_PROMPT_PATTERNS), re.IGNORECASE | re.MULTILINE)

# Host-key confirmation is the one prompt that is not a secret.
_NON_SECRET_RE = re.compile(r"\(yes/no", re.IGNORECASE)

_FAILURE_RE = re.compile(
    r"permission denied|authentication failed|too many authentication failures"
    r"|no route to host|connection (refused|closed|timed out)|could not resolve",
    re.IGNORECASE,
)

# Callback signature: (prompt_text, is_secret) -> answer, or None to abort.
PromptCallback = Callable[[str, bool], Awaitable[str | None]]

_CONTROL_DIR = Path.home() / ".ssh" / "cm-lazyslurm"

_SSH_BASE_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
]


def _control_path(host: str) -> str:
    """A short, stable control-socket path for `host`.

    Not ssh's own %C token: the path is needed before the connection exists.
    Unix sockets are length-limited, hence the hash rather than the host name.
    """
    digest = hashlib.sha256(host.encode()).hexdigest()[:16]
    return str(_CONTROL_DIR / f"{digest}.sock")


# How long the auth pump waits for the master to say something before looping
# back to re-check whether it is up. Short in tests, so a slow login is fast.
_AUTH_POLL_SECONDS = 1.0


class SSHSession:
    """One persistent SSH connection, shared by every remote command."""

    def __init__(
        self,
        host: str,
        prompt_cb: PromptCallback | None = None,
        command_timeout: float = 20.0,
        connect_timeout: float = 120.0,
        channel_command: list[str] | None = None,
    ) -> None:
        self.host = host
        self.prompt_cb = prompt_cb
        self.command_timeout = command_timeout
        self.connect_timeout = connect_timeout
        # Overrides the shell channel's argv. Only used by tests, which run a
        # local /bin/sh instead of reaching for a cluster; those also skip the
        # master unless they explicitly set _use_master.
        self._channel_command = channel_command
        self._use_master = channel_command is None
        self.control_path = _control_path(host)

        self._master: asyncio.subprocess.Process | None = None
        self._master_pty: int | None = None
        self._channel: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._seq = 0
        self._closed = False
        self.last_error: str = ""

    # ------------------------------------------------------------------
    # Connection setup
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._channel is not None and self._channel.returncode is None

    async def connect(self) -> tuple[bool, str]:
        """Authenticate once and open the shared shell channel."""
        self._closed = False
        if self._use_master:
            ok, msg = await self._start_master()
            if not ok:
                return False, msg
        ok, msg = await self._start_channel()
        if not ok:
            return False, msg
        return True, f"Connected to {self.host}"

    async def _master_alive(self) -> bool:
        """Ask ssh whether the multiplexing master is up (`ssh -O check`).

        False when ssh itself is missing: the caller reports "cannot connect",
        which is true and useful, rather than raising out of the poll loop.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", f"ControlPath={self.control_path}",
                "-O", "check", self.host,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return False
        return await proc.wait() == 0

    async def _start_master(self) -> tuple[bool, str]:
        """Spawn `ssh -M -N` on a pty and answer its prompts via the callback.

        The pty is what makes 2FA work: ssh opens /dev/tty for prompts, so with
        plain pipes the verification-code question would never reach us.
        """
        if await self._master_alive():
            return True, "Reusing existing SSH master"

        try:
            _CONTROL_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as e:
            return False, f"Cannot create {_CONTROL_DIR}: {e}"

        argv = self._master_argv()

        parent_fd, child_fd = pty.openpty()
        try:
            self._master = await asyncio.create_subprocess_exec(
                *argv,
                stdin=child_fd, stdout=child_fd, stderr=child_fd,
                start_new_session=True,
            )
        except OSError as e:
            os.close(parent_fd)
            os.close(child_fd)
            return False, f"Failed to start ssh: {e}"
        finally:
            os.close(child_fd)

        self._master_pty = parent_fd
        try:
            return await asyncio.wait_for(
                self._drive_master_auth(parent_fd), timeout=self.connect_timeout
            )
        except asyncio.TimeoutError:
            await self._kill_master()
            return False, f"Timed out connecting to {self.host}"

    def _master_argv(self) -> list[str]:
        """Argv for the background master connection (overridden in tests)."""
        argv = [
            "ssh", "-N", "-M",
            "-o", f"ControlPath={self.control_path}",
            "-o", "ControlPersist=no",
            *_SSH_BASE_OPTS,
        ]
        if self.prompt_cb is None:
            # Nobody can answer a prompt: fail fast instead of hanging.
            argv += ["-o", "BatchMode=yes"]
        else:
            argv += ["-o", "NumberOfPasswordPrompts=3"]
        argv.append(self.host)
        return argv

    async def _drive_master_auth(self, fd: int) -> tuple[bool, str]:
        """Pump the master's pty: forward prompts out, write answers back.

        One read stays pending across polls. `asyncio.wait_for` cancels the
        *future* on timeout but cannot cancel a thread already blocked inside
        `os.read`, so starting a fresh read each poll stacked up one blocked
        thread per second of silence — and ssh's eventual write would land in
        whichever reader the kernel picked, quite possibly one nobody was
        awaiting any more, losing the 2FA prompt and hanging the login.

        Silence is the normal case here: a Duo push waits on a phone.
        """
        buffer = ""
        loop = asyncio.get_running_loop()
        pending: asyncio.Future[str] | None = None
        while True:
            if self._master is not None and self._master.returncode is not None:
                # ssh exited: either it failed, or (rarely) the master is up
                # anyway. Check before reporting an error.
                if await self._master_alive():
                    return True, "SSH master ready"
                return False, self._auth_error(buffer)

            if await self._master_alive():
                return True, "SSH master ready"

            if pending is None:
                pending = asyncio.ensure_future(
                    loop.run_in_executor(None, _read_fd, fd)
                )
            done, _ = await asyncio.wait({pending}, timeout=_AUTH_POLL_SECONDS)
            if not done:
                # Nothing said yet — loop back and re-check the master, but
                # leave the same read pending rather than opening another.
                continue
            chunk = pending.result()
            pending = None
            if not chunk:
                if await self._master_alive():
                    return True, "SSH master ready"
                return False, self._auth_error(buffer)

            buffer += chunk
            match = _PROMPT_RE.search(buffer.replace("\r", ""))
            if not match:
                continue

            prompt = match.group(0).strip()
            secret = _NON_SECRET_RE.search(prompt) is None
            if self.prompt_cb is None:
                await self._kill_master()
                return False, f"{self.host} asked for '{prompt}' but no input is available"
            answer = await self.prompt_cb(prompt, secret)
            if answer is None:
                await self._kill_master()
                return False, "Authentication cancelled"
            os.write(fd, (answer + "\n").encode())
            buffer = ""  # consumed: don't re-match this prompt

    def _auth_error(self, buffer: str) -> str:
        """Turn the master's pty output into one useful error line."""
        text = buffer.replace("\r", "").strip()
        for line in reversed(text.splitlines()):
            if _FAILURE_RE.search(line):
                return line.strip()
        last = text.splitlines()[-1].strip() if text else ""
        return last or f"Could not connect to {self.host}"

    async def _start_channel(self) -> tuple[bool, str]:
        """Open the shell that every command is then written into."""
        argv = self._channel_command or [
            "ssh",
            "-o", f"ControlPath={self.control_path}",
            "-o", "ControlMaster=no",
            "-o", "BatchMode=yes",  # the master already authenticated
            *_SSH_BASE_OPTS,
            self.host,
            "/bin/sh -s",
        ]
        try:
            self._channel = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return False, f"Failed to open SSH channel: {e}"
        return True, "SSH channel open"

    # ------------------------------------------------------------------
    # Running commands
    # ------------------------------------------------------------------

    async def run(
        self, command: str, timeout: float | None = None
    ) -> tuple[str, str, int]:
        """Run `command` in the shared shell. Returns (stdout, stderr, rc)."""
        if self._closed:
            return "", "SSH session closed", 1
        async with self._lock:
            if not self.connected:
                ok, msg = await self._reconnect()
                if not ok:
                    self.last_error = msg
                    return "", msg, 1
            try:
                return await asyncio.wait_for(
                    self._exchange(command), timeout=timeout or self.command_timeout
                )
            except asyncio.TimeoutError:
                # The shell is now mid-command and out of sync — drop it. The
                # next run() opens a fresh one over the same connection.
                await self._kill_channel()
                return "", f"Timed out running: {command}", 1
            except (BrokenPipeError, ConnectionResetError) as e:
                await self._kill_channel()
                return "", f"SSH channel lost: {e}", 1

    async def _exchange(self, command: str) -> tuple[str, str, int]:
        """Write one command and read back its framed stdout/stderr/status."""
        assert self._channel is not None
        assert self._channel.stdin is not None
        self._seq += 1
        marker = f"__LAZYSLURM_{self._seq}__"

        # `command` runs, then both streams are stamped with the marker. The
        # stdout stamp carries the exit status; the stderr stamp just tells the
        # reader where this command's stderr ends.
        payload = (
            f"{command}\n"
            f"__lzs_rc=$?\n"
            f"printf '%s%s\\n' '{marker}' \"$__lzs_rc\"\n"
            f"printf '%s\\n' '{marker}' 1>&2\n"
        )
        self._channel.stdin.write(payload.encode())
        await self._channel.stdin.drain()

        out, err = await asyncio.gather(
            _read_until(self._channel.stdout, marker),
            _read_until(self._channel.stderr, marker),
        )
        stdout, status = out
        stderr, _ = err
        try:
            rc = int(status.strip())
        except (TypeError, ValueError):
            rc = 1
        return stdout, stderr, rc

    async def _reconnect(self) -> tuple[bool, str]:
        """Bring the channel back, re-authenticating only if the master died."""
        await self._kill_channel()
        if self._use_master and not await self._master_alive():
            ok, msg = await self._start_master()
            if not ok:
                return False, msg
        return await self._start_channel()

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def _kill_channel(self) -> None:
        proc, self._channel = self._channel, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
            await proc.wait()
        except (ProcessLookupError, OSError):
            pass

    async def _kill_master(self) -> None:
        if self._master_pty is not None:
            try:
                os.close(self._master_pty)
            except OSError:
                pass
            self._master_pty = None
        proc, self._master = self._master, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
            await proc.wait()
        except (ProcessLookupError, OSError):
            pass

    async def close(self) -> None:
        """Close the channel and tear the connection down."""
        self._closed = True
        await self._kill_channel()
        if self._use_master:
            # Ask ssh to close the master cleanly, then make sure it is gone.
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ssh", "-o", f"ControlPath={self.control_path}",
                    "-O", "exit", self.host,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            except OSError:
                pass
        await self._kill_master()


def _read_fd(fd: int) -> str:
    """Blocking read of one chunk from a pty (run in an executor)."""
    try:
        return os.read(fd, 4096).decode(errors="replace")
    except OSError:
        return ""


async def _read_until(
    stream: asyncio.StreamReader | None, marker: str
) -> tuple[str, str]:
    """Read lines until the marker line. Returns (text before, marker suffix).

    The suffix is whatever the marker line carries after the marker itself —
    the exit status on stdout, nothing on stderr.
    """
    if stream is None:
        return "", ""
    chunks: list[str] = []
    while True:
        line = await stream.readline()
        if not line:  # EOF: the channel died mid-command
            return "".join(chunks), ""
        text = line.decode(errors="replace")
        index = text.find(marker)
        if index != -1:
            chunks.append(text[:index])
            return "".join(chunks), text[index + len(marker):]
        chunks.append(text)


def quote_argv(args: tuple[str, ...] | list[str]) -> str:
    """Join an argv list into one shell command line for the remote shell."""
    return " ".join(shlex.quote(a) for a in args)
