"""The commands people run. The proxy itself is server.py.

    keyproxy-up [--restart] [--open] [proxy options]   start the proxy in the background if it is down
    claude-keyproxy [claude args]                       start it if needed, then run claude through it

Proxy options, such as --headless or --strategy, apply when the proxy starts. --restart stops a
running proxy first so they take effect.
"""
from __future__ import annotations

import argparse
import http.client
import os
import shutil
import signal
import subprocess
import sys
import time
import webbrowser

from . import __version__

HEALTH = "/_keyproxy/api/cache"  # answered by every proxy, headless or not


def port() -> int:
    return int(os.environ.get("KEYPROXY_PORT", 18787))


def log_path() -> str:
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(cache, "keyproxy.log")


def running(at: int) -> bool:
    """True if something answers HTTP on the port. http.client, so no system proxy gets in the way."""
    conn = http.client.HTTPConnection("127.0.0.1", at, timeout=1)
    try:
        conn.request("GET", HEALTH)
        conn.getresponse().read()
        return True
    except OSError:
        return False
    finally:
        conn.close()


def wait_until(up: bool, at: int, seconds: float = 3.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if running(at) == up:
            return True
        time.sleep(0.2)
    return running(at) == up


def listener_pids(at: int) -> list[int]:
    """Processes listening on the port, found with lsof."""
    try:
        found = subprocess.run(["lsof", "-t", "-nP", f"-iTCP:{at}", "-sTCP:LISTEN"],
                               capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise SystemExit("keyproxy-up --restart needs lsof to find the running proxy") from None
    return [int(pid) for pid in found.stdout.split()]


def stop(at: int) -> None:
    for pid in listener_pids(at):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue  # it already exited
    wait_until(False, at)


def start(at: int, options: list[str]) -> subprocess.Popen[bytes]:
    path = log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "ab") as log:
        return subprocess.Popen([sys.executable, "-m", "claude_keyproxy", "--port", str(at), *options],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def started(proc: subprocess.Popen[bytes], at: int, seconds: float = 15.0) -> bool:
    """Wait for a new proxy to answer. A cold start on a slow machine can take seconds;
    a proxy that exits, for example on a bad option, fails at once."""
    end = time.time() + seconds
    while proc.poll() is None and time.time() < end:
        if running(at):
            return True
        time.sleep(0.2)
    return running(at)


def ensure(options: list[str] | None = None, restart: bool = False) -> bool:
    """Start the proxy if it is not running. False if it did not come up."""
    at = port()
    if restart:
        stop(at)
    if running(at):
        return True
    if started(start(at, options or []), at):
        return True
    print(f"keyproxy did not start, see {log_path()}", file=sys.stderr)
    return False


def up(argv: list[str] | None = None) -> int:
    """keyproxy-up"""
    parser = argparse.ArgumentParser(
        prog="keyproxy-up", description="Start keyproxy in the background if it is down. Safe to call often.",
        epilog="Any other option goes to the proxy when it starts, e.g. --headless or --strategy priority.")
    parser.add_argument("--restart", action="store_true", help="stop a running proxy first, so new options apply")
    parser.add_argument("--open", action="store_true", help="open the key manager page in the browser")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args, options = parser.parse_known_args(argv)
    if not ensure(options, args.restart):
        return 1
    if args.open:
        url = f"http://127.0.0.1:{port()}/"
        if not webbrowser.open(url):
            print(f"Open {url}")
    return 0


def claude(argv: list[str] | None = None) -> int:
    """claude-keyproxy: every argument goes to claude."""
    command = shutil.which("claude")
    if command is None:
        print("claude-keyproxy: the claude command was not found", file=sys.stderr)
        return 1
    if not ensure():
        return 1
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port()}"
    env["CLAUDE_CODE_OAUTH_TOKEN"] = "proxy"  # a placeholder login so Claude Code starts; the proxy swaps in a key
    args = sys.argv[1:] if argv is None else argv
    os.execve(command, [command, *args], env)
    return 0  # not reached: execve replaces this process


def main_up() -> None:
    sys.exit(up())


def main_claude() -> None:
    sys.exit(claude())
