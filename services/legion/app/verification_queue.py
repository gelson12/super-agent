"""
Module-level asyncio.Queue so the webhook handler and L4/L5 healing can
rendezvous on incoming magic-link verification messages without threading
shared state through every call site.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class VerificationMessage:
    url: str | None
    code: str | None
    account: str  # 'A' or 'B'


_queue: asyncio.Queue[VerificationMessage] = asyncio.Queue()


async def put(message: VerificationMessage) -> None:
    await _queue.put(message)


async def get(timeout_s: float = 240) -> VerificationMessage | None:
    try:
        return await asyncio.wait_for(_queue.get(), timeout=timeout_s)
    except asyncio.TimeoutError:
        return None


def drain() -> int:
    """Clear any pending messages. Use between healing attempts or in tests."""
    n = 0
    while not _queue.empty():
        try:
            _queue.get_nowait()
            n += 1
        except asyncio.QueueEmpty:
            break
    return n
