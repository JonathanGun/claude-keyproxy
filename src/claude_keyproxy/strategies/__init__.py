"""Key strategies: which keys to try for a request, and in what order.

A strategy is a file in this folder that defines one function, matching `Strategy`:

    def choose(keys, cooldowns):
        return [...]    # the keys to try, in order

- `keys` is every key with a readable token file, as Key(name, token, priority), in no set order.
- `cooldowns.left(name)` is the seconds until a key is ready again, 0 if it is ready now.
- Return at least one key whenever `keys` is not empty, or the request fails with 503.

To add one, put a file next to priority.py and start the proxy with `--strategy <file name>`,
or set KEYPROXY_STRATEGY.
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:  # only for the type checker: keypool imports this package
    from ..keypool import Cooldowns, Key

DEFAULT = "priority"


class Strategy(Protocol):
    def __call__(self, keys: list[Key], cooldowns: Cooldowns) -> list[Key]: ...


def names() -> list[str]:
    """Every strategy in this folder, by file name."""
    return sorted(m.name for m in pkgutil.iter_modules(__path__))


def load(name: str = DEFAULT) -> Strategy:
    """The choose() function of the strategy called `name`."""
    if name not in names():
        raise ValueError(f"unknown strategy {name!r}, pick one of: {', '.join(names())}")
    choose = getattr(importlib.import_module(f"{__name__}.{name}"), "choose", None)
    if not callable(choose):
        raise ValueError(f"strategy {name!r} has no choose(keys, cooldowns) function")
    strategy: Strategy = choose
    return strategy
