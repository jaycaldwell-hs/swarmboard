from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any


class EventBroker:
    """Process-local fan-out for committed database events.

    SQLite remains the source of truth. This broker only tells connected browsers
    that they should read the newly committed state.
    """

    def __init__(self, *, queue_size: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._lock = asyncio.Lock()
        self._queue_size = queue_size

    async def publish(self, event: dict[str, Any]) -> None:
        async with self._lock:
            subscribers = tuple(self._subscribers)
        for queue in subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self._queue_size)
        async with self._lock:
            self._subscribers.add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                self._subscribers.discard(queue)

    async def encode(self, queue: asyncio.Queue[dict[str, Any]]) -> AsyncIterator[str]:
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
                event_type = str(event.get("event_type", "update"))
                event_id = event.get("id")
                if event_id is not None:
                    yield f"id: {event_id}\n"
                yield f"event: {event_type}\n"
                yield f"data: {json.dumps(event, default=str, separators=(',', ':'))}\n\n"
            except TimeoutError:
                yield ": keep-alive\n\n"
