#!/usr/bin/env python3
"""Drive the TUI in a pty and capture what it drew.

LazySlurm needs a terminal: with a redirected stdout it has no size and renders
nothing. This allocates a real pty, gives it a size, sends keystrokes on a
schedule and prints everything the program wrote.

    scripts/drive_tui.py --keys 'p' -- ./target/debug/lazyslurm --refresh 0
    scripts/drive_tui.py --keys '/train\\r' --plain -- lazyslurm

Keys are sent one at a time, `--key-delay` apart, starting `--settle` seconds in
so the first frame has been drawn. `q` is appended automatically unless
`--no-quit` is given, so the program always exits.

Escape sequences in --keys are interpreted: \\r is Enter, \\e is Escape,
\\t is Tab. Use --plain to strip ANSI from the output, which is what you want
when grepping for text.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import pty
import re
import select
import struct
import sys
import termios
import time

ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][A-Z0-9]|\x1b[=>]")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keys", default="", help="keystrokes to send, in order")
    parser.add_argument("--settle", type=float, default=1.5,
                        help="seconds before the first keystroke")
    parser.add_argument("--key-delay", type=float, default=0.6,
                        help="seconds between keystrokes")
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="give up after this long")
    parser.add_argument("--cols", type=int, default=120)
    parser.add_argument("--rows", type=int, default=40)
    parser.add_argument("--plain", action="store_true",
                        help="strip ANSI escapes from the output")
    parser.add_argument("--no-quit", action="store_true",
                        help="do not append 'q'")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- followed by the command to run")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    command = [arg for arg in args.command if arg != "--"]
    if not command:
        print("no command given; use -- before it", file=sys.stderr)
        return 2

    keys = args.keys.encode().decode("unicode_escape")
    if not args.no_quit:
        keys += "q"

    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(command[0], command)

    # Without a size the program has nowhere to draw.
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", args.rows, args.cols, 0, 0))

    output = b""
    sent = 0
    start = time.time()
    while time.time() - start < args.timeout:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break          # the child closed the pty
            if not chunk:
                break
            output += chunk

        due = args.settle + sent * args.key_delay
        if sent < len(keys) and time.time() - start >= due:
            os.write(fd, keys[sent].encode())
            sent += 1

        if sent >= len(keys) and time.time() - start > due + args.key_delay:
            # Everything has been sent and answered; give it a moment to exit.
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                break

    os.close(fd)
    _, status = os.waitpid(pid, 0)

    text = output.decode(errors="replace")
    if args.plain:
        text = ANSI.sub("", text).replace("\r", "")
    sys.stdout.write(text)
    return os.waitstatus_to_exitcode(status)


if __name__ == "__main__":
    sys.exit(main())
