"""
Server — async-friendly entry point for running a Veil node in server mode.

Example:
    import asyncio
    from veil_core import Server, DataEvent

    async def main():
        server = Server(port=4433, host="0.0.0.0")
        server.start()

        async for event in server.events():
            if isinstance(event, DataEvent):
                print(f"[{event.session_id:#x}] received: {event.data!r}")
                server.send(event.session_id, b"pong", stream_id=event.stream_id)

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, AsyncIterator, Optional

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


class Server:
    """
    Veil protocol server.

    Wraps the C++ ``VeilNode`` object (EventLoop + PipelineProcessor) and
    converts raw C++ callbacks into Python asyncio events that callers can
    consume via ``async for event in server.events()``.

    Thread safety:
        ``start()`` / ``stop()`` must be called from the same thread.
        ``send()`` is thread-safe and can be called from the asyncio loop.
        Internally the C++ layer runs its own worker threads; all callbacks
        are marshalled back onto the asyncio event loop via
        ``loop.call_soon_threadsafe``.
    """

    def __init__(
        self,
        port: int,
        host: str = "0.0.0.0",
        *,
        protocol_wrapper: str = "none",  # "none" | "websocket" | "tls"
        persona_preset: str = "custom",  # "custom" | "browser_ws" | "quic_media" | ...
        enable_http_handshake_emulation: bool = False,
        rotation_interval_seconds: int = 30,
        handshake_timeout_ms: int = 5000,
        session_idle_timeout_ms: int = 0,
        mtu: int = 1400,
        psk: bytes = bytes([0xAB]) * 32,
        clients: list[dict[str, Any] | Any] | None = None,
        fallback_psk: bytes | None = None,
        fallback_psk_policy: str = "deny_always",
        allow_legacy_unhinted: bool = False,
        allow_hinted_route_miss_global_fallback: bool = False,
        max_legacy_trial_decrypt_attempts: int = 8,
    ) -> None:
        self._host = host
        self._port = port

        # asyncio event queue: C++ callbacks push events here; callers pop them.
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._accept_queue: asyncio.Queue[NewConnectionEvent] = asyncio.Queue()
        self._session_queues: dict[int, asyncio.Queue[Event]] = {}
        self._event_buffer = EventBuffer()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False

        if not _EXT_AVAILABLE:
            # Graceful degradation: the extension is not compiled yet.
            # All real I/O calls will raise RuntimeError below.
            self._node = None
            return

        cfg = _ext.NodeConfig()
        cfg.host = host
        cfg.port = port
        cfg.protocol_wrapper = protocol_wrapper
        cfg.persona_preset = persona_preset
        cfg.enable_http_handshake_emulation = enable_http_handshake_emulation
        cfg.rotation_interval_seconds = rotation_interval_seconds
        cfg.handshake_timeout_ms = handshake_timeout_ms
        cfg.session_idle_timeout_ms = session_idle_timeout_ms
        cfg.mtu = mtu
        cfg.psk = psk
        cfg.clients = [self._build_client_credential(item) for item in (clients or [])]
        cfg.fallback_psk = fallback_psk or b""
        cfg.fallback_psk_policy = fallback_psk_policy
        cfg.allow_legacy_unhinted = allow_legacy_unhinted
        cfg.allow_hinted_route_miss_global_fallback = (
            allow_hinted_route_miss_global_fallback
        )
        cfg.max_legacy_trial_decrypt_attempts = max_legacy_trial_decrypt_attempts

        self._node = _ext.VeilNode(cfg)
        self._node.on_new_connection = self._on_new_connection
        self._node.on_data = self._on_data
        self._node.on_disconnected = self._on_disconnected
        self._node.on_error = self._on_error

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Bind the UDP socket and start the C++ worker threads."""
        self._require_ext()
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._node.start()

    def stop(self) -> None:
        """Gracefully stop the node and block until all threads join."""
        if not self._running:
            return
        self._running = False
        if self._node is not None:
            self._node.stop()

    def send(
        self,
        session_id: int,
        data: bytes,
        *,
        stream_id: int = 0,
    ) -> bool:
        """
        Encrypt and send *data* to the peer identified by *session_id*.

        Returns True if the data was accepted into the send pipeline,
        False if the pipeline queue is full (back-pressure).
        """
        self._require_ext()
        return self._node.send(session_id, data, stream_id)

    def disconnect(self, session_id: int) -> bool:
        """Drop a local session and emit a DisconnectedEvent if it existed."""
        self._require_ext()
        return self._node.disconnect(session_id)

    def stats(self) -> dict:
        """
        Return a snapshot of pipeline statistics from the C++ layer.

        Keys:
            rx_packets, tx_packets, processed_packets,
            rx_bytes, tx_bytes, decrypt_errors, queue_full_drops.
        """
        self._require_ext()
        return self._node.stats()

    async def events(self) -> AsyncIterator[Event]:
        """
        Async generator that yields events as they arrive from the C++ layer.

        Yields:
            NewConnectionEvent, DataEvent, DisconnectedEvent, or ErrorEvent.

        Usage::

            async for event in server.events():
                ...
        """
        while self._running or self._event_buffer.has_pending() or not self._queue.empty():
            try:
                event = await self.next_event(timeout=0.1)
                yield event
            except asyncio.TimeoutError:
                # No events yet — keep looping so we check self._running again.
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
        session_id: int | None = None,
        stream_id: int | None = None,
    ) -> DataEvent:
        """
        Wait for the next ``DataEvent`` matching the supplied filters.

        Non-matching events remain pending for later consumers.
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
        """
        Wait for the next event matching *predicate*.

        Non-matching events remain pending for later consumers.
        """
        matcher = predicate or (lambda _event: True)
        return await self._event_buffer.recv_event(
            self._queue,
            timeout=timeout,
            predicate=matcher,
        )

    async def accept(self, *, timeout: float | None = None) -> Session:
        """
        Wait for the next established peer session and return a session wrapper.

        Non-connection events remain pending for later consumers.
        """
        if timeout is None:
            event = await self._accept_queue.get()
        else:
            event = await asyncio.wait_for(self._accept_queue.get(), timeout=timeout)
        return Session(
            self,
            session_id=event.session_id,
            remote_host=event.remote_host,
            remote_port=event.remote_port,
            default_stream_id=0,
        )

    # ------------------------------------------------------------------
    # C++ callback receivers (called from C++ worker threads)
    # ------------------------------------------------------------------

    def _build_client_credential(self, item: dict[str, Any] | Any) -> Any:
        credential = (
            _ext.ClientCredential()
            if hasattr(_ext, "ClientCredential")
            else SimpleNamespace(client_id="", enabled=True, psk=b"")
        )
        if isinstance(item, dict):
            client_id = str(item.get("client_id", "")).strip()
            enabled = bool(item.get("enabled", True))
            psk = item.get("psk", b"")
        else:
            client_id = str(getattr(item, "client_id", "")).strip()
            enabled = bool(getattr(item, "enabled", True))
            psk = getattr(item, "psk", b"")

        if isinstance(psk, str):
            psk_bytes = bytes.fromhex(psk)
        else:
            psk_bytes = bytes(psk)

        credential.client_id = client_id
        credential.enabled = enabled
        credential.psk = psk_bytes
        return credential

    def _on_new_connection(self, session_id: int, host: str, port: int) -> None:
        evt = NewConnectionEvent(
            session_id=session_id, remote_host=host, remote_port=port
        )
        self._get_session_queue(session_id)
        self._push_accept_event(evt)
        self._push_event(evt)

    def _on_data(self, session_id: int, stream_id: int, data: bytes) -> None:
        evt = DataEvent(session_id=session_id, stream_id=stream_id, data=data)
        self._push_session_event(session_id, evt)
        self._push_event(evt)

    def _on_disconnected(self, session_id: int, reason: str) -> None:
        evt = DisconnectedEvent(session_id=session_id, reason=reason)
        self._push_session_event(session_id, evt)
        self._push_event(evt)

    def _on_error(self, session_id: int, message: str) -> None:
        evt = ErrorEvent(session_id=session_id, message=message)
        if session_id != 0:
            self._push_session_event(session_id, evt)
        self._push_event(evt)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _push_event(self, event: Event) -> None:
        """Thread-safe: schedule event delivery onto the asyncio loop."""
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def _push_accept_event(self, event: NewConnectionEvent) -> None:
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._accept_queue.put_nowait, event)

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

    # Context manager support — lets users write `async with Server(...) as s:`
    async def __aenter__(self) -> "Server":
        self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        self.stop()
