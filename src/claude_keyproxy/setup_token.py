"""Run `claude setup-token` for the key manager, so nobody has to open a terminal.

    session = Session(claude)    # starts the CLI in a pseudo-terminal
    session.url()                # the Claude sign-in link to show the user
    session.token(code)          # feed it the code the sign-in page showed, get the token back

A session closes itself after token(), after LIFETIME seconds, or at the end of a `with` block.
The code and the token pass through this process only. Nothing is logged or written here.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from typing import Callable

URL_TIMEOUT = 20
TOKEN_TIMEOUT = 45
LIFETIME = 600  # a session nobody finished is killed after this many seconds

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
LINK = re.compile(rb"\x1b\]8;[^;\x07\x1b]*;(https://[^\x07\x1b]+)")  # the OSC 8 hyperlink the CLI prints
PLAIN_LINK = re.compile(r"https://\S+/oauth/authorize\?\S+")
TOKEN = re.compile(r"sk-ant-[A-Za-z0-9_-]{30,}(?=[\s\x1b])")


class SetupError(Exception):
    pass


def plain(buf: bytes) -> str:
    """Terminal output as text, without colors and escape codes."""
    return ANSI.sub("", buf.decode("utf8", "replace"))


def find_link(buf: bytes) -> str | None:
    """The sign-in link in the CLI's output so far, or None."""
    osc8 = LINK.search(buf)
    if osc8:
        return osc8.group(1).decode()
    m = PLAIN_LINK.search(plain(buf))
    return m.group(0) if m else None


def find_token(buf: bytes) -> str | None:
    """The token in the CLI's output so far, or None."""
    m = TOKEN.search(plain(buf))
    return m.group(0) if m else None


class Pty:
    """A command running in a pseudo-terminal: read what it prints, type into it, close it."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str, rows: int = 50, cols: int = 1000) -> None:
        self.master: int | None  # None once closed
        self.master, slave = pty.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                                         env=env, cwd=cwd, start_new_session=True)
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        self.buf = b""  # everything read so far

    def __enter__(self) -> Pty:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def fd(self) -> int:
        if self.master is None:
            raise SetupError("claude setup-token is no longer running")
        return self.master

    def write(self, data: bytes) -> None:
        os.write(self.fd(), data)

    def read_until(self, find: Callable[[bytes], str | None], timeout: float) -> str | None:
        """Read until find(buf) returns something, the command exits, or time runs out."""
        end = time.time() + timeout
        while time.time() < end:
            hit = find(self.buf)
            if hit:
                return hit
            if select.select([self.fd()], [], [], 0.2)[0]:
                try:
                    data = os.read(self.fd(), 65536)
                except OSError:
                    data = b""
                if not data:
                    break  # the command exited
                self.buf += data
        return find(self.buf)

    def close(self) -> None:
        if self.master is None:  # already closed
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):  # it may have exited already
            os.killpg(self.proc.pid, signal.SIGTERM)
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        os.close(self.master)
        self.master = None


class Session:
    """One `claude setup-token` run, from the sign-in link to the token."""

    def __init__(self, claude: str, spawn: Callable[..., Pty] = Pty) -> None:
        """`spawn(argv, env, cwd)` starts the terminal; a test can pass a fake one."""
        env = {k: v for k, v in os.environ.items()
               if k not in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")}
        env["TERM"] = "xterm-256color"
        env["BROWSER"] = "true"  # the page shows the link, so the CLI must not open a second tab
        self.pty = spawn([claude, "setup-token"], env, os.path.expanduser("~"))
        self.timer = threading.Timer(LIFETIME, self.close)
        self.timer.daemon = True
        self.timer.start()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def url(self) -> str:
        link = self.pty.read_until(find_link, URL_TIMEOUT)
        if not link:
            self.close()
            raise SetupError("claude setup-token did not give a sign-in link")
        return link

    def token(self, code: str) -> str:
        with self:  # one try: the session ends whatever happens
            self.pty.buf = b""
            self.pty.write(code.encode())
            time.sleep(0.3)  # a separate write, so the CLI sees Enter and not one pasted blob
            self.pty.write(b"\r")
            token = self.pty.read_until(find_token, TOKEN_TIMEOUT)
            if not token:
                tail = plain(self.pty.buf).replace(code, "").split()
                raise SetupError("claude did not accept the code" + (f": {' '.join(tail[-12:])}" if tail else ""))
            return token

    def close(self) -> None:
        self.timer.cancel()
        self.pty.close()
