"""The keys the proxy can use: read from index.json, cooled down when limited, and the usage
each one last reported. Which key a request tries first is up to a strategy, see strategies/."""
from __future__ import annotations

import json
import os
import threading
import time
from http.client import HTTPResponse
from typing import Any, Callable, NamedTuple

from . import strategies

Usage = dict[str, Any]  # parse_usage() output: status, 5h, 7d, plus "seen" once recorded
Clock = Callable[[], float]  # epoch seconds; time.time unless a test passes its own

DEFAULT_COOLDOWN = 300  # seconds a limited key is skipped when the response does not say
BAD_AUTH_COOLDOWN = 600


def parse_usage(get: Callable[[str], str | None]) -> Usage | None:
    """Subscription usage from the anthropic-ratelimit-unified-* response headers, or None.

    `get(name)` returns a header value or None. Utilization is a fraction (0.42 = 42%),
    reset is epoch seconds.
    """
    status = get("anthropic-ratelimit-unified-status")
    if status is None:
        return None
    usage: Usage = {"status": status}
    for window in ("5h", "7d"):
        utilization = get(f"anthropic-ratelimit-unified-{window}-utilization")
        reset = get(f"anthropic-ratelimit-unified-{window}-reset")
        usage[window] = None
        if utilization is None or reset is None:
            continue
        try:
            usage[window] = {"utilization": float(utilization), "reset": int(reset)}
        except ValueError:
            continue  # a header that is not a number, keep None
    return usage


class Key(NamedTuple):
    name: str
    token: str | None  # None for API-key traffic, which is forwarded with its own key
    priority: int

    def __repr__(self) -> str:
        """Like the API, show only the token's last 4 characters, so a log or traceback never leaks it."""
        token = f"...{self.token[-4:]}" if self.token else self.token
        return f"Key(name={self.name!r}, token={token!r}, priority={self.priority})"


class Cooldowns:
    """Which keys are skipped, and until when."""

    def __init__(self, clock: Clock = time.time) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.until: dict[str, float] = {}  # key name -> epoch seconds until which the key is skipped

    def cool(self, name: str, resp: HTTPResponse, default: float) -> int:
        """Skip a key until its reset time. Returns seconds until the key is tried again."""
        reset = resp.getheader("anthropic-ratelimit-unified-reset")
        retry = resp.getheader("retry-after")
        until = self.clock() + default
        if reset and reset.isdigit():
            until = float(reset)
        elif retry and retry.isdigit():
            until = self.clock() + int(retry)
        with self.lock:
            self.until[name] = until
        return int(until - self.clock())

    def left(self, name: str) -> float:
        """Seconds until the key is ready, 0 if it is."""
        with self.lock:
            return max(0.0, self.until.get(name, 0) - self.clock())

    def status(self) -> dict[str, int]:
        now = self.clock()
        with self.lock:
            return {n: max(0, int(t - now)) for n, t in self.until.items() if t > now}

    def reset(self) -> None:
        with self.lock:
            self.until.clear()


class KeyPool:
    """The keys from index.json, their cooldowns, and the usage each one last reported.

    `strategy(keys, cooldowns)` returns the keys to try for a request, in order. See strategies/.
    """

    def __init__(self, index: str, strategy: strategies.Strategy | None = None,
                 cooldowns: Cooldowns | None = None) -> None:
        self.index = index
        self.strategy = strategy or strategies.load()
        self.cooldowns = cooldowns or Cooldowns()
        self.lock = threading.Lock()  # guards usage
        self.usage: dict[str, Usage] = {}  # key name -> parse_usage() of its last response, plus "seen"

    def load(self) -> list[Key]:
        """Every enabled key with a readable token file. Re-read on every request so edits apply at once."""
        try:
            with open(self.index) as f:
                entries = json.load(f)["keys"]
        except FileNotFoundError:  # no key added yet
            return []
        keys: list[Key] = []
        for entry in entries:
            if entry.get("disabled"):  # turned off on the page until it is turned back on
                continue
            try:
                with open(os.path.expanduser(entry["path"])) as f:
                    keys.append(Key(entry["name"], "".join(f.read().split()), entry["priority"]))
            except OSError:
                continue
        return keys

    def candidates(self) -> list[Key]:
        return self.strategy(self.load(), self.cooldowns)

    def record(self, name: str, resp: HTTPResponse) -> None:
        usage = parse_usage(resp.getheader)
        if usage:
            usage["seen"] = int(self.cooldowns.clock())
            with self.lock:
                self.usage[name] = usage

    def observe(self, name: str, resp: HTTPResponse) -> int | None:
        """Record the key's usage, and cool it down if it is limited or its token was rejected.
        Returns the cooldown in seconds, or None when the key is fine."""
        self.record(name, resp)
        bad_auth = resp.status == 401
        limited = resp.status == 429 or resp.getheader("anthropic-ratelimit-unified-status") == "rejected"
        if not (limited or bad_auth):
            return None
        return self.cooldowns.cool(name, resp, BAD_AUTH_COOLDOWN if bad_auth else DEFAULT_COOLDOWN)

    def reset(self) -> None:
        """Forget the cooldowns and the usage, as if no key had served a request."""
        self.cooldowns.reset()
        with self.lock:
            self.usage.clear()

    def seen(self) -> dict[str, Usage]:
        with self.lock:
            return dict(self.usage)
