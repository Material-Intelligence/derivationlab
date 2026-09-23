"""SSE encoding, cursor parsing, heartbeats, and disconnect cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Protocol

from .models import RunEvent


class DisconnectProbe(Protocol):
    async def is_disconnected(self) -> bool: ...


def encode_event(event: RunEvent) -> str:
    """Encode a message event.

    The type stays inside JSON instead of using a named SSE ``event`` field so
    ordinary ``EventSource.onmessage`` clients receive every domain event.
    """

    data = event.model_dump_json(by_alias=True)
    return f"id: {event.event_id}\ndata: {data}\n\n"


async def stream_sse(
    request: DisconnectProbe,
    source: AsyncIterator[RunEvent],
    *,
    heartbeat_seconds: float,
    retry_milliseconds: int,
    authorization_check: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    """Relay one event at a time and release subscriptions on disconnect.

    A single pending ``anext`` task is retained across heartbeat timeouts.  It
    is cancelled and the source is closed in ``finally``, which lets services
    unregister bounded subscriber queues when a client is slow or disconnects.
    """

    yield f"retry: {retry_milliseconds}\n\n"
    pending: asyncio.Task[RunEvent] | None = None
    try:
        while True:
            if await request.is_disconnected():
                return
            if authorization_check is not None and not await authorization_check():
                return
            if pending is None:
                pending = asyncio.create_task(anext(source))
            done, _ = await asyncio.wait({pending}, timeout=heartbeat_seconds)
            if not done:
                if authorization_check is not None and not await authorization_check():
                    return
                yield ": heartbeat\n\n"
                continue
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            pending = None
            if authorization_check is not None and not await authorization_check():
                return
            yield encode_event(event)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        close = getattr(source, "aclose", None)
        if close is not None:
            with suppress(RuntimeError):
                await close()
