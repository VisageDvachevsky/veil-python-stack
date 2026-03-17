from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from typing import Deque

from veil_core.events import DataEvent, Event


class EventBuffer:
    def __init__(self) -> None:
        self._backlog: Deque[Event] = deque()

    async def next_event(
        self,
        queue: asyncio.Queue[Event],
        *,
        timeout: float | None = None,
    ) -> Event:
        if self._backlog:
            return self._backlog.popleft()
        if timeout is None:
            return await queue.get()
        return await asyncio.wait_for(queue.get(), timeout=timeout)

    async def recv_data(
        self,
        queue: asyncio.Queue[Event],
        *,
        timeout: float | None = None,
        session_id: int | None = None,
        stream_id: int | None = None,
        predicate: Callable[[DataEvent], bool] | None = None,
    ) -> DataEvent:
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        skipped: list[Event] = []

        try:
            while True:
                remaining = None if deadline is None else max(0.0, deadline - loop.time())
                event = await self.next_event(queue, timeout=remaining)
                if self._matches_data(
                    event,
                    session_id=session_id,
                    stream_id=stream_id,
                    predicate=predicate,
                ):
                    return event
                skipped.append(event)
        finally:
            if skipped:
                self._backlog.extendleft(reversed(skipped))

    async def recv_event(
        self,
        queue: asyncio.Queue[Event],
        *,
        timeout: float | None = None,
        predicate: Callable[[Event], bool],
    ) -> Event:
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        skipped: list[Event] = []

        try:
            while True:
                remaining = None if deadline is None else max(0.0, deadline - loop.time())
                event = await self.next_event(queue, timeout=remaining)
                if predicate(event):
                    return event
                skipped.append(event)
        finally:
            if skipped:
                self._backlog.extendleft(reversed(skipped))

    def has_pending(self) -> bool:
        return bool(self._backlog)

    @staticmethod
    def _matches_data(
        event: Event,
        *,
        session_id: int | None,
        stream_id: int | None,
        predicate: Callable[[DataEvent], bool] | None,
    ) -> bool:
        if not isinstance(event, DataEvent):
            return False
        if session_id is not None and event.session_id != session_id:
            return False
        if stream_id is not None and event.stream_id != stream_id:
            return False
        if predicate is not None and not predicate(event):
            return False
        return True
