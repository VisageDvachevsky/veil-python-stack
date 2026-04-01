"""
Client — async-friendly entry point for connecting to a Veil server.

Example:
    import asyncio
    from veil_core import Client, DataEvent

    async def main():
        async with Client(host="1.2.3.4", port=4433) as client:
            await client.connect()
            client.send(b"hello")

            async for event in client.events():
                if isinstance(event, DataEvent):
                    print(f"Server replied: {event.data!r}")
                    break

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import AsyncIterator, Optional

from veil_core._event_buffer import EventBuffer
from veil_core._ext_loader import load_extension
from veil_core.events import (
    DataEvent,
    DisconnectedEvent,
    ErrorEvent,
    Event,
    NewConnectionEvent,
)
from veil_core.session import Session

_ext, _EXT_AVAILABLE, _EXT_IMPORT_ERROR = load_extension()


class Client:
    """
    Veil protocol client.

    Mirrors the Server API but operates in client mode:
    - Initiates the crypto handshake after ``connect()``.
    - Emits a ``NewConnectionEvent`` once the session is established.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        client_id: str = "",
        local_port: int = 0,  # 0 = OS picks an ephemeral port
        protocol_wrapper: str = "none",
        persona_preset: str = "custom",
        enable_http_handshake_emulation: bool = False,
        rotation_interval_seconds: int = 30,
        handshake_timeout_ms: int = 5000,
        session_idle_timeout_ms: int = 0,
        mtu: int = 1400,
        psk: bytes = bytes([0xAB]) * 32,
    ) -> None:
        self._host = host
        self._port = port

        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._session_queues: dict[int, asyncio.Queue[Event]] = {}
        self._event_buffer = EventBuffer()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending_connect: Optional[asyncio.Future[NewConnectionEvent]] = None
        self._session_id: Optional[int] = None
        self._running = False

        if not _EXT_AVAILABLE:
            self._node = None
            return

        cfg = _ext.NodeConfig()
        cfg.host = host
        cfg.port = port
        cfg.local_port = local_port
        cfg.protocol_wrapper = protocol_wrapper
        cfg.persona_preset = persona_preset
        cfg.enable_http_handshake_emulation = enable_http_handshake_emulation
        cfg.rotation_interval_seconds = rotation_interval_seconds
        cfg.handshake_timeout_ms = handshake_timeout_ms
        cfg.session_idle_timeout_ms = session_idle_timeout_ms
        cfg.mtu = mtu
        cfg.psk = psk
        cfg.client_id = client_id
        cfg.is_client = True

        self._node = _ext.VeilNode(cfg)
        self._node.on_new_connection = self._on_new_connection
        self._node.on_data = self._on_data
        self._node.on_disconnected = self._on_disconnected
        self._node.on_error = self._on_error

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Bind the local UDP socket and start the C++ worker threads."""
        self._require_ext()
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._node.start()

    def stop(self) -> None:
        """Gracefully stop the client."""
        if not self._running:
            return
        self._running = False
        if self._pending_connect is not None and not self._pending_connect.done():
            self._pending_connect.set_exception(RuntimeError("Client stopped"))
        self._session_id = None
        if self._node is not None:
            self._node.stop()

    async def connect(self) -> NewConnectionEvent:
        """
        Initiate connection to the server and wait for the session to be
        established (i.e., the crypto handshake to complete).

        Returns the ``NewConnectionEvent`` once the session is ready.
        Raises ``asyncio.TimeoutError`` if no connection within 10 s.
        """
        self._require_ext()
        if self._pending_connect is not None and not self._pending_connect.done():
            raise RuntimeError("connect() already in progress")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[NewConnectionEvent] = loop.create_future()
        self._pending_connect = future
        try:
            self._node.connect(self._host, self._port)
            async with asyncio.timeout(10):
                event = await future
        finally:
            if self._pending_connect is future:
                self._pending_connect = None

        self._session_id = event.session_id
        return event

    async def connect_session(self) -> Session:
        """
        Connect and return a session-oriented wrapper for the established peer.
        """
        event = await self.connect()
        return Session(
            self,
            session_id=event.session_id,
            remote_host=event.remote_host,
            remote_port=event.remote_port,
            default_stream_id=1,
        )

    def send(
        self,
        data: bytes,
        *,
        stream_id: int = 1,
        session_id: Optional[int] = None,
    ) -> bool:
        """
        Send plaintext *data* to the server.

        If *session_id* is omitted the most recently established session is used.
        Returns True if queued, False on back-pressure.
        """
        self._require_ext()
        sid = session_id or self._session_id
        if sid is None:
            raise RuntimeError("Not connected — call connect() first.")
        return self._node.send(sid, data, stream_id)

    def disconnect(self, session_id: Optional[int] = None) -> bool:
        """Drop the local session and clear the cached active session id."""
        self._require_ext()
        sid = session_id or self._session_id
        if sid is None:
            raise RuntimeError("Not connected — call connect() first.")
        disconnected = self._node.disconnect(sid)
        if disconnected and sid == self._session_id:
            self._session_id = None
        return disconnected

    async def events(self) -> AsyncIterator[Event]:
        """Async generator — identical interface to Server.events()."""
        while self._running or self._event_buffer.has_pending() or not self._queue.empty():
            try:
                event = await self.next_event(timeout=0.1)
                yield event
            except asyncio.TimeoutError:
                if not self._running and not self._event_buffer.has_pending() and self._queue.empty():
                    break
                continue

    async def next_event(self, *, timeout: float | None = None) -> Event:
        """Return the next pending event without exposing the raw queue."""
        return await self._event_buffer.next_event(self._queue, timeout=timeout)

    async def recv(
        self,
        *,
        timeout: float | None = None,
        stream_id: int | None = None,
        session_id: int | None = None,
    ) -> DataEvent:
        """
        Wait for the next ``DataEvent`` matching the supplied filters.

        Non-matching events remain pending, so ``recv()`` can be mixed with
        ``events()`` and ``next_event()`` safely.
        """
        return await self._event_buffer.recv_data(
            self._queue,
            timeout=timeout,
            session_id=session_id,
            stream_id=stream_id,
        )

    async def recv_event(
        self,
        *,
        timeout: float | None = None,
        predicate: Callable[[Event], bool] | None = None,
    ) -> Event:
        """Wait for the next event matching *predicate*."""
        matcher = predicate or (lambda _event: True)
        return await self._event_buffer.recv_event(
            self._queue,
            timeout=timeout,
            predicate=matcher,
        )

    def stats(self) -> dict:
        """Pipeline statistics snapshot (see Server.stats())."""
        self._require_ext()
        return self._node.stats()

    def session(self) -> Session:
        """
        Return a session wrapper for the currently connected peer.

        Useful when a connection is already established and code wants a
        concrete session object instead of carrying ``session_id`` around.
        """
        self._require_ext()
        if self._session_id is None:
            raise RuntimeError("Not connected — call connect() first.")
        return Session(
            self,
            session_id=self._session_id,
            remote_host=self._host,
            remote_port=self._port,
            default_stream_id=1,
        )

    # ------------------------------------------------------------------
    # C++ callbacks
    # ------------------------------------------------------------------

    def _on_new_connection(self, session_id: int, host: str, port: int) -> None:
        event = NewConnectionEvent(session_id=session_id, remote_host=host, remote_port=port)
        self._get_session_queue(session_id)
        if self._pending_connect is not None and not self._pending_connect.done():
            self._loop.call_soon_threadsafe(self._pending_connect.set_result, event)
        self._push_event(event)

    def _on_data(self, session_id: int, stream_id: int, data: bytes) -> None:
        event = DataEvent(session_id=session_id, stream_id=stream_id, data=data)
        self._push_session_event(session_id, event)
        self._push_event(event)

    def _on_disconnected(self, session_id: int, reason: str) -> None:
        if session_id == self._session_id:
            self._session_id = None
        event = DisconnectedEvent(session_id=session_id, reason=reason)
        self._push_session_event(session_id, event)
        self._push_event(event)

    def _on_error(self, session_id: int, message: str) -> None:
        event = ErrorEvent(session_id=session_id, message=message)
        if (
            session_id == 0
            and self._pending_connect is not None
            and not self._pending_connect.done()
            and self._loop is not None
        ):
            self._loop.call_soon_threadsafe(
                self._pending_connect.set_exception,
                RuntimeError(message),
            )
        if session_id != 0:
            self._push_session_event(session_id, event)
        self._push_event(event)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _push_event(self, event: Event) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def _push_session_event(self, session_id: int, event: Event) -> None:
        if self._loop is None or not self._loop.is_running():
            return
        queue = self._get_session_queue(session_id)
        self._loop.call_soon_threadsafe(queue.put_nowait, event)

    def _get_session_queue(self, session_id: int) -> asyncio.Queue[Event]:
        queue = self._session_queues.get(session_id)
        if queue is None:
            queue = asyncio.Queue()
            self._session_queues[session_id] = queue
        return queue

    def session_queue(self, session_id: int) -> asyncio.Queue[Event]:
        return self._get_session_queue(session_id)

    def _require_ext(self) -> None:
        if not _EXT_AVAILABLE:
            details = f" Details: {_EXT_IMPORT_ERROR}" if _EXT_IMPORT_ERROR else ""
            raise RuntimeError(
                "veil_core C++ extension (_veil_core_ext) is not compiled. "
                "Run `cmake --build build --target _veil_core_ext` first."
                f"{details}"
            )

    async def __aenter__(self) -> "Client":
        self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        self.stop()
