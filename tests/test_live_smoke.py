from __future__ import annotations

import asyncio
import time
import socket
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from veil_core import _veil_core_ext  # noqa: F401
except ImportError:
    _veil_core_ext = None

from veil_core import Client, DataEvent, DisconnectedEvent, NewConnectionEvent, Server  # noqa: E402


def reserve_ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@unittest.skipIf(_veil_core_ext is None, "compiled _veil_core_ext is unavailable")
class LiveSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.psk = bytes([0x5A]) * 32

    async def test_client_server_roundtrip_over_real_extension(self) -> None:
        port = reserve_ephemeral_port()
        payload = b"smoke-ping"
        reply = b"smoke-pong"

        server = Server(port=port, host="127.0.0.1", psk=self.psk, session_idle_timeout_ms=5_000)
        client = Client(host="127.0.0.1", port=port, psk=self.psk, handshake_timeout_ms=2_000)

        async with server, client:
            connection_task = asyncio.create_task(client.connect())

            async def server_loop() -> None:
                async for event in server.events():
                    if isinstance(event, NewConnectionEvent):
                        continue
                    if isinstance(event, DataEvent):
                        self.assertEqual(event.data, payload)
                        self.assertTrue(server.send(event.session_id, reply, stream_id=event.stream_id))
                        break

            server_task = asyncio.create_task(server_loop())
            connection = await asyncio.wait_for(connection_task, timeout=5)
            self.assertIsInstance(connection, NewConnectionEvent)

            self.assertTrue(client.send(payload, stream_id=11))

            received_reply = None
            async for event in client.events():
                if isinstance(event, DataEvent):
                    received_reply = event
                    break

            await asyncio.wait_for(server_task, timeout=5)

            self.assertIsNotNone(received_reply)
            assert received_reply is not None
            self.assertEqual(received_reply.data, reply)
            self.assertEqual(received_reply.stream_id, 11)

    async def test_stream_id_and_multi_message_flow_over_real_extension(self) -> None:
        port = reserve_ephemeral_port()
        payloads = [
            (3, b"alpha"),
            (7, b"beta"),
            (11, b"gamma"),
        ]
        server = Server(port=port, host="127.0.0.1", psk=self.psk, session_idle_timeout_ms=5_000)
        client = Client(host="127.0.0.1", port=port, psk=self.psk, handshake_timeout_ms=2_000)

        async with server, client:
            await asyncio.wait_for(client.connect(), timeout=5)
            seen_on_server: list[tuple[int, bytes]] = []

            async def server_loop() -> None:
                async for event in server.events():
                    if isinstance(event, DataEvent):
                        seen_on_server.append((event.stream_id, event.data))
                        self.assertTrue(
                            server.send(
                                event.session_id,
                                b"ack:" + event.data,
                                stream_id=event.stream_id,
                            )
                        )
                        if len(seen_on_server) == len(payloads):
                            break

            server_task = asyncio.create_task(server_loop())
            for stream_id, payload in payloads:
                self.assertTrue(client.send(payload, stream_id=stream_id))

            received: list[tuple[int, bytes]] = []
            async for event in client.events():
                if isinstance(event, DataEvent):
                    received.append((event.stream_id, event.data))
                    if len(received) == len(payloads):
                        break

            await asyncio.wait_for(server_task, timeout=5)
            self.assertEqual(seen_on_server, payloads)
            self.assertEqual(received, [(sid, b"ack:" + data) for sid, data in payloads])

    async def test_disconnect_and_reconnect_over_real_extension(self) -> None:
        port = reserve_ephemeral_port()
        server = Server(port=port, host="127.0.0.1", psk=self.psk, session_idle_timeout_ms=5_000)
        client = Client(host="127.0.0.1", port=port, psk=self.psk, handshake_timeout_ms=2_000)

        async with server, client:
            first = await asyncio.wait_for(client.connect(), timeout=5)
            self.assertTrue(client.disconnect())

            disconnected = None
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    event = await asyncio.wait_for(client._queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if isinstance(event, DisconnectedEvent):
                    disconnected = event
                    break

            self.assertIsNotNone(disconnected)
            assert disconnected is not None
            self.assertEqual(disconnected.session_id, first.session_id)

            second = await asyncio.wait_for(client.connect(), timeout=5)
            self.assertNotEqual(second.session_id, 0)
            self.assertTrue(client.send(b"reconnected", stream_id=21))

            server_data = None
            async def server_probe() -> None:
                nonlocal server_data
                async for event in server.events():
                    if isinstance(event, DataEvent):
                        server_data = (event.stream_id, event.data)
                        break

            await asyncio.wait_for(server_probe(), timeout=5)
            self.assertEqual(server_data, (21, b"reconnected"))
