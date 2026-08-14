"""Tests for the persistent SSH session.

The command side is exercised against a real local `/bin/sh` channel (same code
path, minus the ssh hop), so the marker framing, exit codes, stream separation
and recovery behavior are all covered without a cluster. The prompt-detection
and error-reporting helpers are tested directly.
"""

from __future__ import annotations

import asyncio
import threading
import pty
import contextlib
import os
import sys

import pytest

from lazyslurm import slurm
from lazyslurm.models import Config
from lazyslurm.ssh import SSHSession, _control_path, _PROMPT_RE, _NON_SECRET_RE


def _local_session(**kwargs) -> SSHSession:
    """A session whose 'ssh channel' is a local shell — no network involved."""
    return SSHSession("test@localhost", channel_command=["/bin/sh", "-s"], **kwargs)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Prompt detection (this is what makes 2FA work)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "user@login's password: ",
        "Password: ",
        "Enter passphrase for key '/home/u/.ssh/id_ed25519': ",
        "Verification code: ",
        "Duo two-factor login for user\n\nPasscode or option (1-3): ",
        "One-time password (OATH-TOTP): ",
        "OTP: ",
        "Token_Response: ",
        "Enter PIN: ",
        "Are you sure you want to continue connecting (yes/no/[fingerprint])? ",
    ],
)
def test_prompt_patterns_match_real_ssh_prompts(text):
    assert _PROMPT_RE.search(text.replace("\r", ""))


@pytest.mark.parametrize(
    "text",
    [
        "Last login: Tue Aug 12 09:00:00 2026 from 10.0.0.1",
        "Welcome to the cluster!",
        "debug1: Authenticating to login:22 as 'user'",
    ],
)
def test_prompt_patterns_ignore_ordinary_output(text):
    assert not _PROMPT_RE.search(text)


def test_host_key_prompt_is_not_treated_as_secret():
    assert _NON_SECRET_RE.search("(yes/no/[fingerprint])?")
    assert not _NON_SECRET_RE.search("Verification code:")


def test_control_path_is_stable_and_per_host():
    a = _control_path("user@login.hpc.edu")
    assert a == _control_path("user@login.hpc.edu")
    assert a != _control_path("user@other.hpc.edu")
    assert a.endswith(".sock")


def test_auth_error_picks_the_meaningful_line():
    session = _local_session()
    assert session._auth_error(
        "debug1: connecting\r\nPermission denied (publickey,keyboard-interactive).\r\n"
    ) == "Permission denied (publickey,keyboard-interactive)."
    assert "unreachable-host" in session._auth_error("") or session._auth_error("")


# ---------------------------------------------------------------------------
# Running commands over one persistent channel
# ---------------------------------------------------------------------------


def test_commands_share_one_channel_and_keep_shell_state():
    async def scenario():
        session = _local_session()
        ok, _ = await session.connect()
        assert ok
        pid_before = session._channel.pid
        try:
            out, err, rc = await session.run("echo hello")
            assert (out, err, rc) == ("hello\n", "", 0)

            # State set by one command is visible to the next: proof they run
            # in the same shell, not in separate ssh invocations.
            await session.run("MARKER=persisted")
            out, _, _ = await session.run("echo $MARKER")
            assert out == "persisted\n"
            assert session._channel.pid == pid_before  # never respawned
        finally:
            await session.close()

    _run(scenario())


def test_stdout_and_stderr_stay_separate_and_rc_is_reported():
    async def scenario():
        session = _local_session()
        await session.connect()
        try:
            out, err, rc = await session.run("echo to-out; echo to-err 1>&2; (exit 3)")
            assert out == "to-out\n"
            assert err == "to-err\n"
            assert rc == 3
        finally:
            await session.close()

    _run(scenario())


def test_output_containing_marker_like_text_is_not_confused():
    async def scenario():
        session = _local_session()
        await session.connect()
        try:
            out, _, rc = await session.run("echo '__LAZYSLURM_99__ in my data'")
            assert out == "__LAZYSLURM_99__ in my data\n"
            assert rc == 0
            # The counter moved on, so a later command still frames correctly.
            out, _, rc = await session.run("echo after")
            assert (out, rc) == ("after\n", 0)
        finally:
            await session.close()

    _run(scenario())


def test_multiline_output_is_returned_whole():
    async def scenario():
        session = _local_session()
        await session.connect()
        try:
            out, _, rc = await session.run("printf 'a\\nb\\nc\\n'")
            assert out == "a\nb\nc\n"
            assert rc == 0
        finally:
            await session.close()

    _run(scenario())


def test_commands_are_serialized_not_interleaved():
    async def scenario():
        session = _local_session()
        await session.connect()
        try:
            results = await asyncio.gather(*[
                session.run(f"echo job{i}") for i in range(8)
            ])
            assert [out for out, _, _ in results] == [f"job{i}\n" for i in range(8)]
        finally:
            await session.close()

    _run(scenario())


def test_timeout_recycles_the_channel_and_later_commands_work():
    async def scenario():
        session = _local_session(command_timeout=0.5)
        await session.connect()
        try:
            out, err, rc = await session.run("sleep 5")
            assert rc == 1
            assert "Timed out" in err
            assert not session.connected  # dropped, because it is out of sync
            # Next call transparently reopens the channel.
            out, _, rc = await session.run("echo recovered")
            assert (out, rc) == ("recovered\n", 0)
        finally:
            await session.close()

    _run(scenario())


def test_channel_death_is_recovered_on_the_next_command():
    async def scenario():
        session = _local_session()
        await session.connect()
        try:
            await session.run("echo warm")
            session._channel.kill()
            await session._channel.wait()
            out, _, rc = await session.run("echo back")
            assert (out, rc) == ("back\n", 0)
        finally:
            await session.close()

    _run(scenario())


def test_run_after_close_fails_without_reconnecting():
    async def scenario():
        session = _local_session()
        await session.connect()
        await session.close()
        out, err, rc = await session.run("echo nope")
        assert (out, rc) == ("", 1)
        assert "closed" in err

    _run(scenario())


# ---------------------------------------------------------------------------
# slurm.py transport integration
# ---------------------------------------------------------------------------


def test_run_cmd_goes_through_the_session_in_remote_mode(monkeypatch):
    async def scenario():
        monkeypatch.setattr(slurm, "_config", Config(remote="user@login"))
        session = _local_session()
        await session.connect()
        monkeypatch.setattr(slurm, "_sessions", {"host": session})
        monkeypatch.setattr(slurm, "_attached", "host")
        try:
            # Arguments are shell-quoted into one command line for the channel.
            out, _, rc = await slurm._run_cmd("echo", "a b", "c'd")
            assert out == "a b c'd\n"
            assert rc == 0
        finally:
            await session.close()

    _run(scenario())


def test_run_cmd_without_a_session_reports_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(slurm, "_config", Config(remote="user@login"))
    monkeypatch.setattr(slurm, "_sessions", {})
    monkeypatch.setattr(slurm, "_attached", "")
    out, err, rc = _run(slurm._run_cmd("squeue"))
    assert (out, rc) == ("", 1)
    assert "not connected" in err


class _RecordingSession:
    """Stands in for SSHSession, capturing the command line it is handed."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, command, timeout=None):
        self.commands.append(command)
        return "ok\n", "", 0


def test_node_hop_runs_from_the_login_node(monkeypatch):
    """The compute-node hop must happen inside the session, not locally.

    A local ProxyJump would open a second connection to the login node and
    trigger two-factor authentication all over again.
    """
    recorder = _RecordingSession()
    monkeypatch.setattr(slurm, "_config", Config(remote="user@login"))
    monkeypatch.setattr(slurm, "_sessions", {"host": recorder})
    monkeypatch.setattr(slurm, "_attached", "host")

    out, rc = _run(slurm._ssh_cmd("gpu001", "nvidia-smi -L"))
    assert (out, rc) == ("ok\n", 0)
    assert len(recorder.commands) == 1
    sent = recorder.commands[0]
    # One command, run on the login node, which then hops to the compute node.
    assert sent.startswith("ssh ")
    assert "-J" not in sent  # no local ProxyJump -> no second authentication
    assert "gpu001" in sent
    assert "'nvidia-smi -L'" in sent  # inner command quoted as one argument


def test_local_mode_still_ssh_directly_to_the_node(monkeypatch):
    monkeypatch.setattr(slurm, "_config", Config())  # no remote
    monkeypatch.setattr(slurm, "_sessions", {})
    monkeypatch.setattr(slurm, "_attached", "")
    captured = {}

    async def _fake_exec(*args, **kwargs):
        captured["argv"] = args

        class _Proc:
            returncode = 0

            async def communicate(self):
                return b"out", b""

        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    out, rc = _run(slurm._ssh_cmd("gpu001", "hostname"))
    assert (out, rc) == ("out", 0)
    assert captured["argv"][0] == "ssh"
    assert captured["argv"][-2:] == ("gpu001", "hostname")


def test_connect_remote_is_a_noop_in_local_mode():
    ok, msg = _run(slurm.connect_remote(None, Config()))
    assert ok
    assert msg == "Local mode"
    assert slurm.get_session() is None


# ---------------------------------------------------------------------------
# The interactive auth pump (password + 2FA verification code)
# ---------------------------------------------------------------------------

# Stands in for ssh: prompts on its pty exactly like a 2FA login does, and
# "becomes reachable" (touches the ready file) only once both answers are right.
_FAKE_SSH = r"""
import os, sys, time
ready = sys.argv[1]
print("debug1: Authenticating to login:22", flush=True)
sys.stdout.write("jdoe@login's password: "); sys.stdout.flush()
password = sys.stdin.readline().strip()
sys.stdout.write("\nDuo two-factor login for jdoe\n\nPasscode or option (1-3): ")
sys.stdout.flush()
code = sys.stdin.readline().strip()
if password == "hunter2" and code == "123456":
    open(ready, "w").close()
    print("\nAuthenticated.", flush=True)
    time.sleep(30)
else:
    print("\nPermission denied (keyboard-interactive).", flush=True)
    sys.exit(255)
"""


class _FakeAuthSession(SSHSession):
    """SSHSession whose master is a local script that prompts like a 2FA login."""

    def __init__(self, script: str, ready: str, **kwargs) -> None:
        super().__init__(
            "jdoe@login", channel_command=["/bin/sh", "-s"], **kwargs
        )
        self._use_master = True  # exercise the auth path with the fake master
        self._script = script
        self._ready = ready

    def _master_argv(self):
        return [sys.executable, "-u", self._script, self._ready]

    async def _master_alive(self):
        return os.path.exists(self._ready)


@pytest.fixture
def fake_ssh(tmp_path):
    script = tmp_path / "fake_ssh.py"
    script.write_text(_FAKE_SSH)
    return str(script), str(tmp_path / "ready")


def test_two_factor_prompts_are_forwarded_and_answered(fake_ssh):
    """Password *and* verification code reach the UI callback, in order."""
    script, ready = fake_ssh
    seen: list[tuple[str, bool]] = []

    async def prompt(text, secret):
        seen.append((text, secret))
        return "hunter2" if "password" in text.lower() else "123456"

    async def scenario():
        session = _FakeAuthSession(script, ready, prompt_cb=prompt, connect_timeout=20)
        try:
            ok, msg = await session.connect()
            assert ok, msg
            # And the session works for commands afterwards.
            out, _, rc = await session.run("echo authenticated")
            assert (out, rc) == ("authenticated\n", 0)
        finally:
            await session.close()

    _run(scenario())
    assert len(seen) == 2
    assert "password" in seen[0][0].lower()
    assert seen[0][1] is True                      # masked
    assert "passcode" in seen[1][0].lower()        # the 2FA prompt
    assert seen[1][1] is True                      # masked


def test_wrong_code_reports_the_servers_own_error(fake_ssh):
    script, ready = fake_ssh

    async def prompt(text, secret):
        return "hunter2" if "password" in text.lower() else "000000"

    async def scenario():
        session = _FakeAuthSession(script, ready, prompt_cb=prompt, connect_timeout=20)
        ok, msg = await session.connect()
        await session.close()
        return ok, msg

    ok, msg = _run(scenario())
    assert not ok
    assert "Permission denied" in msg


def test_cancelling_a_prompt_aborts_the_connection(fake_ssh):
    script, ready = fake_ssh

    async def prompt(text, secret):
        return None  # user pressed Escape

    async def scenario():
        session = _FakeAuthSession(script, ready, prompt_cb=prompt, connect_timeout=20)
        ok, msg = await session.connect()
        await session.close()
        return ok, msg

    ok, msg = _run(scenario())
    assert not ok
    assert "cancelled" in msg.lower()


def test_no_callback_means_batchmode_so_it_cannot_hang():
    """Without a UI to prompt, the master must fail fast rather than block."""
    session = SSHSession("user@login", prompt_cb=None)
    assert "BatchMode=yes" in session._master_argv()

    interactive = SSHSession("user@login", prompt_cb=lambda *_: None)
    assert "BatchMode=yes" not in interactive._master_argv()


# ---------------------------------------------------------------------------
# The auth pump must not leak a thread per second of silence (#52)
# ---------------------------------------------------------------------------


def _pump_session(monkeypatch, answers=None):
    """A session with a short poll interval and `ssh -O check` stubbed out."""
    import lazyslurm.ssh as ssh_mod

    monkeypatch.setattr(ssh_mod, "_AUTH_POLL_SECONDS", 0.02)
    asked: list[tuple[str, bool]] = []
    queue = list(answers or ["123456"])

    async def prompt_cb(prompt, secret):
        asked.append((prompt, secret))
        return queue.pop(0) if queue else None

    session = SSHSession("test@localhost", prompt_cb=prompt_cb)

    async def _never_alive():
        return False

    monkeypatch.setattr(session, "_master_alive", _never_alive)
    return session, asked


def _drive(session, body):
    """Run the pump against a real pty; `body(child_fd)` plays ssh's side.

    The child side is closed *before* the event loop shuts down. A reader
    thread blocked in os.read() cannot be cancelled, and asyncio.run() waits
    for the default executor on its way out — so leaving the pty open makes a
    leak deadlock the test instead of failing it.

    Returns (whatever body returned, threads still alive at its peak).
    """
    parent_fd, child_fd = pty.openpty()
    peak = {}

    async def scenario():
        base = threading.active_count()
        task = asyncio.create_task(session._drive_master_auth(parent_fd))
        try:
            return await body(child_fd)
        finally:
            peak["extra"] = threading.active_count() - base
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            os.close(child_fd)          # EOF: unblocks every pending read
            await asyncio.sleep(0.05)

    try:
        out = asyncio.run(scenario())
    finally:
        with contextlib.suppress(OSError):
            os.close(parent_fd)
    return out, peak["extra"]


def test_silence_does_not_leak_one_thread_per_poll(monkeypatch):
    """asyncio.wait_for cannot cancel a thread already blocked in os.read.

    Starting a fresh read each poll left every previous one blocked on the same
    fd, so a slow 2FA login burned a thread per second out of the shared default
    executor — the one slurm.py also uses for log reads and file checks.
    """
    session, _ = _pump_session(monkeypatch)

    async def stay_quiet(_child_fd):
        await asyncio.sleep(0.5)        # ~25 polls, with ssh saying nothing
        return None

    _, extra = _drive(session, stay_quiet)
    assert extra <= 1, f"{extra} threads blocked on the pty after 0.5s of silence"


def test_a_prompt_after_silence_is_still_answered(monkeypatch):
    """The prompt has to reach the pump, not a read nobody is awaiting."""
    session, asked = _pump_session(monkeypatch, answers=["424242"])

    async def quiet_then_prompt(child_fd):
        await asyncio.sleep(0.3)
        os.write(child_fd, b"Verification code: ")
        for _ in range(200):
            if asked:
                break
            await asyncio.sleep(0.01)
        # Bounded: if the prompt was swallowed there is nothing to read back,
        # and this has to fail rather than block.
        try:
            return await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    None, os.read, child_fd, 64
                ),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            return b""

    answer, _ = _drive(session, quiet_then_prompt)
    assert asked, "the prompt written after a quiet period was never seen"
    assert asked[0][0].startswith("Verification code:")
    assert asked[0][1] is True                      # a code is a secret
    assert b"424242" in answer                      # answered back into the pty


def test_prompt_arriving_immediately_still_works(monkeypatch):
    """The no-silence path must be unaffected by the change."""
    session, asked = _pump_session(monkeypatch, answers=["hunter2"])

    async def prompt_at_once(child_fd):
        os.write(child_fd, b"user@login's password: ")
        for _ in range(200):
            if asked:
                break
            await asyncio.sleep(0.01)
        return None

    _drive(session, prompt_at_once)
    assert asked and asked[0][0].endswith("password:")
    assert asked[0][1] is True


# ---------------------------------------------------------------------------
# One login per connection, even while the refresh timer is firing (#56)
# ---------------------------------------------------------------------------


def _slow_login_session(delay=0.3):
    """A session whose master takes `delay` seconds to authenticate.

    Stands in for a 2FA login: ssh has asked for a code and the user is
    looking at their phone. Counts how many times a login is started.
    """
    session = SSHSession("me@login.hpc")
    session._use_master = True
    done = asyncio.Event()
    logins: list[int] = []

    async def start_master():
        logins.append(1)
        await asyncio.sleep(delay)
        done.set()
        return True, "SSH master ready"

    async def master_alive():
        return done.is_set()

    async def start_channel():
        return True, "SSH channel open"

    async def exchange(command):
        return "ok", "", 0

    session._start_master = start_master
    session._master_alive = master_alive
    session._start_channel = start_channel
    session._exchange = exchange
    return session, logins, done


def test_polls_during_a_slow_login_do_not_ask_for_a_second_code(monkeypatch):
    """The refresh timer must not start a rival ssh master.

    connect() assigns the session before authenticating, so a poll arriving
    mid-login found "not connected", called _reconnect(), saw no live master
    and started another one — a second one-time-password prompt per tick.
    """
    session, logins, done = _slow_login_session()
    monkeypatch.setattr(type(session), "connected", property(lambda s: done.is_set()))

    async def scenario():
        connect = asyncio.create_task(session.connect())

        async def poll(n):
            await asyncio.sleep(0.05 * n)      # refresh ticks during the login
            await session.run("squeue -u me")

        await asyncio.gather(connect, *(poll(n) for n in range(1, 5)))

    _run(scenario())
    assert len(logins) == 1, f"{len(logins)} logins for one connect"


def test_a_poll_during_the_login_still_runs_afterwards(monkeypatch):
    """Waiting for the login is the point; the command must not be dropped."""
    session, _, done = _slow_login_session()
    monkeypatch.setattr(type(session), "connected", property(lambda s: done.is_set()))

    async def scenario():
        connect = asyncio.create_task(session.connect())
        await asyncio.sleep(0.05)
        out, _, rc = await session.run("squeue -u me")
        await connect
        return out, rc

    out, rc = _run(scenario())
    assert (out, rc) == ("ok", 0)


def test_a_genuinely_dead_master_is_still_restarted(monkeypatch):
    """The lock must not stop a real reconnect after the connection drops."""
    session, logins, done = _slow_login_session(delay=0)

    async def scenario():
        await session.connect()
        assert len(logins) == 1
        # The master dies and the channel with it.
        done.clear()
        monkeypatch.setattr(type(session), "connected", property(lambda s: False))
        await session.run("squeue -u me")

    _run(scenario())
    assert len(logins) == 2, "a dropped connection has to re-authenticate"
