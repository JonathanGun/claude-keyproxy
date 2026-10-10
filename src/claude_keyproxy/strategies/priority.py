"""The default strategy: try the ready keys, lowest priority number first.

If every key is cooling down, try the one whose limit resets first.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..keypool import Cooldowns, Key


def choose(keys: list[Key], cooldowns: Cooldowns) -> list[Key]:
    ready: list[Key] = []
    for key in keys:
        if cooldowns.left(key.name) == 0:
            ready.append(key)

    if ready:
        ready.sort(key=lambda key: key.priority)
        return ready

    if not keys:
        return []
    soonest = min(keys, key=lambda key: cooldowns.left(key.name))
    return [soonest]
