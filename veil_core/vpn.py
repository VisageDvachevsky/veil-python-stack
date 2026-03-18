from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from veil_core.client import Client
from veil_core.events import DataEvent, DisconnectedEvent, ErrorEvent, Event
from veil_core.message import decode_json_message
from veil_core.server import Server
from veil_core.session import Session


VPN_PROTOCOL_VERSION = 1
DEFAULT_CONTROL_STREAM_ID = 1
DEFAULT_PACKET_STREAM_ID = 2
DEFAULT_VPN_PACKET_MTU = 1300
DEFAULT_KEEPALIVE_INTERVAL = 10.0
DEFAULT_KEEPALIVE_TIMEOUT = 30.0


@dataclass(frozen=True)
class VpnPacket:
    session_id: int
    payload: bytes


class VpnConnection:
    """
    Session-oriented VPN overlay on top of the Veil transport session.

    Control messages are JSON frames on ``control_stream_id``.
    Packet payloads are raw bytes on ``packet_stream_id``.
    """

    def __init__(
        self,
        session: Session,
        *,
        role: str,
        local_name: str = "",
        packet_mtu: int = DEFAULT_VPN_PACKET_MTU,
        keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL,
        keepalive_timeout: float = DEFAULT_KEEPALIVE_TIMEOUT,
        control_stream_id: int = DEFAULT_CONTROL_STREAM_ID,
        packet_stream_id: int = DEFAULT_PACKET_STREAM_ID,
    ) -> None:
        if control_stream_id == packet_stream_id:
            raise ValueError("control_stream_id and packet_stream_id must differ")
        if keepalive_interval <= 0:
            raise ValueError("keepalive_interval must be > 0")
        if keepalive_timeout < keepalive_interval:
            raise ValueError("keepalive_timeout must be >= keepalive_interval")
        self._session = session
        self._role = role
        self._local_name = local_name
        self._packet_mtu = packet_mtu
        self._keepalive_interval = keepalive_interval
        self._keepalive_timeout = keepalive_timeout
        self._control_stream_id = control_stream_id
        self._packet_stream_id = packet_stream_id
        self._packet_queue: asyncio.Queue[VpnPacket] = asyncio.Queue()
        self._control_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._closed = asyncio.get_running_loop().create_future()
        self._event_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._started = False
        self._peer_name = ""
        self._peer_role = ""
        self._peer_packet_mtu = 0
        self._last_rx_time = asyncio.get_running_loop().time()

    @property
    def session(self) -> Session:
        return self._session

    @property
    def session_id(self) -> int:
        return self._session.session_id

    @property
    def role(self) -> str:
        return self._role

    @property
    def local_name(self) -> str:
        return self._local_name

    @property
    def peer_name(self) -> str:
        return self._peer_name

    @property
    def peer_role(self) -> str:
        return self._peer_role

    @property
    def peer_packet_mtu(self) -> int:
        return self._peer_packet_mtu

    @property
    def packet_mtu(self) -> int:
        return self._packet_mtu

    @property
    def keepalive_interval(self) -> float:
        return self._keepalive_interval

    @property
    def keepalive_timeout(self) -> float:
        return self._keepalive_timeout

    @property
    def effective_packet_mtu(self) -> int:
        if self._peer_packet_mtu <= 0:
            return self._packet_mtu
        return min(self._packet_mtu, self._peer_packet_mtu)

    @property
    def is_closed(self) -> bool:
        return self._closed.done()

    @property
    def close_reason(self) -> str | None:
        if not self._closed.done():
            return None
        return self._closed.result()

    async def start(
        self,
        *,
        initiator: bool,
        timeout: float = 10.0,
    ) -> "VpnConnection":
        if self._started:
            raise RuntimeError("VpnConnection already started")

        if initiator:
            self._send_control(
                {
                    "type": "vpn.hello",
                    "version": VPN_PROTOCOL_VERSION,
                    "role": self._role,
                    "name": self._local_name,
                    "packet_mtu": self._packet_mtu,
                }
            )
            ready = await self._recv_control_message(timeout=timeout)
            self._apply_ready(ready)
        else:
            hello = await self._recv_control_message(timeout=timeout)
            self._apply_hello(hello)
            self._send_control(
                {
                    "type": "vpn.ready",
                    "version": VPN_PROTOCOL_VERSION,
                    "role": self._role,
                    "name": self._local_name,
                    "packet_mtu": self._packet_mtu,
                }
            )

        self._started = True
        self._touch_activity()
        self._event_task = asyncio.create_task(self._run(), name=f"vpn-session-{self.session_id:x}")
        self._keepalive_task = asyncio.create_task(
            self._run_keepalive(),
            name=f"vpn-keepalive-{self.session_id:x}",
        )
        return self

    def send_packet(self, payload: bytes) -> bool:
        if self.is_closed:
            raise RuntimeError("VPN connection is closed")
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("payload must be bytes-like")
        payload_bytes = bytes(payload)
        if len(payload_bytes) > self.effective_packet_mtu:
            raise ValueError(
                f"payload exceeds negotiated packet MTU ({len(payload_bytes)} > {self.effective_packet_mtu})"
            )
        return self._session.send(payload_bytes, stream_id=self._packet_stream_id)

    async def recv_packet(self, *, timeout: float | None = None) -> VpnPacket:
        if timeout is None:
            return await self._packet_queue.get()
        return await asyncio.wait_for(self._packet_queue.get(), timeout=timeout)

    async def recv_control(self, *, timeout: float | None = None) -> dict[str, Any]:
        if timeout is None:
            return await self._control_queue.get()
        return await asyncio.wait_for(self._control_queue.get(), timeout=timeout)

    async def wait_closed(self, *, timeout: float | None = None) -> str:
        if timeout is None:
            return await self._closed
        return await asyncio.wait_for(self._closed, timeout=timeout)

    async def close(self, reason: str = "closed_by_local_peer") -> str:
        if not self.is_closed:
            self._send_control({"type": "vpn.close", "reason": reason})
            self._session.disconnect()
        return await self.wait_closed(timeout=5.0)

    async def _run(self) -> None:
        try:
            while True:
                event = await self._session.recv_event(timeout=None)
                if isinstance(event, DataEvent):
                    self._touch_activity()
                    if event.stream_id == self._packet_stream_id:
                        await self._packet_queue.put(
                            VpnPacket(session_id=event.session_id, payload=event.data)
                        )
                        continue
                    if event.stream_id == self._control_stream_id:
                        message = self._decode_control(event.data)
                        if self._handle_runtime_control(message):
                            break
                        continue
                    await self._control_queue.put(
                        {
                            "type": "vpn.unhandled_stream",
                            "stream_id": event.stream_id,
                            "size": len(event.data),
                        }
                    )
                    continue
                if isinstance(event, DisconnectedEvent):
                    self._mark_closed(event.reason or "transport_disconnected")
                    break
                if isinstance(event, ErrorEvent):
                    await self._control_queue.put(
                        {
                            "type": "vpn.transport_error",
                            "message": event.message,
                        }
                    )
                    continue
                await self._control_queue.put({"type": "vpn.unhandled_event", "event": event.kind.value})
        except asyncio.CancelledError:
            self._mark_closed("task_cancelled")
            raise
        except Exception as exc:
            self._mark_closed(f"runtime_error:{exc}")
            await self._control_queue.put(
                {
                    "type": "vpn.runtime_error",
                    "message": str(exc),
                }
            )
        finally:
            await self._stop_background_tasks()
            self._mark_closed(self.close_reason or "stopped")

    def _handle_runtime_control(self, message: dict[str, Any]) -> bool:
        msg_type = message.get("type")
        if msg_type == "vpn.ping":
            self._send_control({"type": "vpn.pong"})
            return False
        if msg_type == "vpn.pong":
            return False
        if msg_type == "vpn.close":
            self._mark_closed(str(message.get("reason") or "closed_by_remote_peer"))
            self._session.disconnect()
            return True
        self._control_queue.put_nowait(message)
        return False

    async def _run_keepalive(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            while not self.is_closed:
                await asyncio.sleep(self._keepalive_interval)
                if self.is_closed:
                    break
                idle_for = loop.time() - self._last_rx_time
                if idle_for > self._keepalive_timeout:
                    self._mark_closed("keepalive_timeout")
                    self._session.disconnect()
                    break
                self._send_control({"type": "vpn.ping"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._mark_closed(f"keepalive_error:{exc}")
            self._session.disconnect()

    async def _stop_background_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in (self._event_task, self._keepalive_task) if task is not None and task is not current]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _touch_activity(self) -> None:
        self._last_rx_time = asyncio.get_running_loop().time()

    def _mark_closed(self, reason: str) -> None:
        if not self._closed.done():
            self._closed.set_result(reason)

    def _send_control(self, body: dict[str, Any]) -> None:
        sent = self._session.send_json(body, stream_id=self._control_stream_id)
        if not sent:
            raise RuntimeError("control stream back-pressure prevented message delivery")

    async def _recv_control_message(self, *, timeout: float) -> dict[str, Any]:
        event = await self._session.recv_event(
            timeout=timeout,
            predicate=lambda item: isinstance(item, DataEvent) and item.stream_id == self._control_stream_id,
        )
        if not isinstance(event, DataEvent):
            raise RuntimeError("unexpected non-data control event")
        return self._decode_control(event.data)

    @staticmethod
    def _decode_control(raw: bytes) -> dict[str, Any]:
        body = decode_json_message(raw)
        if not isinstance(body, dict):
            raise RuntimeError("control message must be a JSON object")
        return body

    def _apply_hello(self, message: dict[str, Any]) -> None:
        if message.get("type") != "vpn.hello":
            raise RuntimeError(f"expected vpn.hello, got {message.get('type')!r}")
        self._apply_peer_metadata(message)

    def _apply_ready(self, message: dict[str, Any]) -> None:
        if message.get("type") != "vpn.ready":
            raise RuntimeError(f"expected vpn.ready, got {message.get('type')!r}")
        self._apply_peer_metadata(message)

    def _apply_peer_metadata(self, message: dict[str, Any]) -> None:
        version = int(message.get("version", 0))
        if version != VPN_PROTOCOL_VERSION:
            raise RuntimeError(f"unsupported VPN protocol version: {version}")
        self._peer_role = str(message.get("role") or "")
        self._peer_name = str(message.get("name") or "")
        self._peer_packet_mtu = int(message.get("packet_mtu") or 0)


class VpnServer:
    def __init__(
        self,
        port: int,
        host: str = "0.0.0.0",
        *,
        local_name: str = "server",
        packet_mtu: int = DEFAULT_VPN_PACKET_MTU,
        keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL,
        keepalive_timeout: float = DEFAULT_KEEPALIVE_TIMEOUT,
        control_stream_id: int = DEFAULT_CONTROL_STREAM_ID,
        packet_stream_id: int = DEFAULT_PACKET_STREAM_ID,
        **server_kwargs: Any,
    ) -> None:
        self._server = Server(port=port, host=host, **server_kwargs)
        self._local_name = local_name
        self._packet_mtu = packet_mtu
        self._keepalive_interval = keepalive_interval
        self._keepalive_timeout = keepalive_timeout
        self._control_stream_id = control_stream_id
        self._packet_stream_id = packet_stream_id

    @property
    def transport(self) -> Server:
        return self._server

    async def __aenter__(self) -> "VpnServer":
        await self._server.__aenter__()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._server.__aexit__()

    async def accept(self, *, timeout: float | None = None, handshake_timeout: float = 10.0) -> VpnConnection:
        session = await self._server.accept(timeout=timeout)
        connection = VpnConnection(
            session,
            role="server",
            local_name=self._local_name,
            packet_mtu=self._packet_mtu,
            keepalive_interval=self._keepalive_interval,
            keepalive_timeout=self._keepalive_timeout,
            control_stream_id=self._control_stream_id,
            packet_stream_id=self._packet_stream_id,
        )
        return await connection.start(initiator=False, timeout=handshake_timeout)


class VpnClient:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        local_name: str = "client",
        packet_mtu: int = DEFAULT_VPN_PACKET_MTU,
        keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL,
        keepalive_timeout: float = DEFAULT_KEEPALIVE_TIMEOUT,
        control_stream_id: int = DEFAULT_CONTROL_STREAM_ID,
        packet_stream_id: int = DEFAULT_PACKET_STREAM_ID,
        **client_kwargs: Any,
    ) -> None:
        self._client = Client(host=host, port=port, **client_kwargs)
        self._local_name = local_name
        self._packet_mtu = packet_mtu
        self._keepalive_interval = keepalive_interval
        self._keepalive_timeout = keepalive_timeout
        self._control_stream_id = control_stream_id
        self._packet_stream_id = packet_stream_id

    @property
    def transport(self) -> Client:
        return self._client

    async def __aenter__(self) -> "VpnClient":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.__aexit__()

    async def connect(self, *, handshake_timeout: float = 10.0) -> VpnConnection:
        session = await self._client.connect_session()
        connection = VpnConnection(
            session,
            role="client",
            local_name=self._local_name,
            packet_mtu=self._packet_mtu,
            keepalive_interval=self._keepalive_interval,
            keepalive_timeout=self._keepalive_timeout,
            control_stream_id=self._control_stream_id,
            packet_stream_id=self._packet_stream_id,
        )
        return await connection.start(initiator=True, timeout=handshake_timeout)
