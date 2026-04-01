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
        self.psk_alice = bytes([0x41]) * 32
        self.psk_bob = bytes([0x42]) * 32

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

    async def test_websocket_http_handshake_emulation_roundtrip_over_real_extension(self) -> None:
        port = reserve_ephemeral_port()
        payload = b"http-upgrade-ping"
        reply = b"http-upgrade-pong"

        server = Server(
            port=port,
            host="127.0.0.1",
            psk=self.psk,
            protocol_wrapper="websocket",
            enable_http_handshake_emulation=True,
            session_idle_timeout_ms=5_000,
        )
        client = Client(
            host="127.0.0.1",
            port=port,
            psk=self.psk,
            protocol_wrapper="websocket",
            enable_http_handshake_emulation=True,
            handshake_timeout_ms=2_000,
        )

        async with server, client:
            async def server_loop() -> None:
                async for event in server.events():
                    if isinstance(event, DataEvent):
                        self.assertEqual(event.data, payload)
                        self.assertTrue(server.send(event.session_id, reply, stream_id=event.stream_id))
                        break

            server_task = asyncio.create_task(server_loop())
            connection = await asyncio.wait_for(client.connect(), timeout=5)
            self.assertIsInstance(connection, NewConnectionEvent)
            self.assertTrue(client.send(payload, stream_id=17))

            received_reply = None
            async for event in client.events():
                if isinstance(event, DataEvent):
                    received_reply = event
                    break

            await asyncio.wait_for(server_task, timeout=5)

            self.assertIsNotNone(received_reply)
            assert received_reply is not None
            self.assertEqual(received_reply.data, reply)
            self.assertEqual(received_reply.stream_id, 17)

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

    async def test_multi_client_server_roundtrip_over_real_extension(self) -> None:
        port = reserve_ephemeral_port()
        server = Server(
            port=port,
            host="127.0.0.1",
            clients=[
                {"client_id": "alice", "psk": self.psk_alice},
                {"client_id": "bob", "psk": self.psk_bob},
            ],
            session_idle_timeout_ms=5_000,
        )
        alice = Client(
            host="127.0.0.1",
            port=port,
            client_id="alice",
            psk=self.psk_alice,
            handshake_timeout_ms=2_000,
        )
        bob = Client(
            host="127.0.0.1",
            port=port,
            client_id="bob",
            psk=self.psk_bob,
            handshake_timeout_ms=2_000,
        )

        async with server, alice, bob:
            expected_payloads = {
                "alice": b"alice-ping",
                "bob": b"bob-ping",
            }
            replies: dict[str, bytes] = {}

            async def server_loop() -> None:
                seen = 0
                async for event in server.events():
                    if not isinstance(event, DataEvent):
                        continue
                    if event.data == expected_payloads["alice"]:
                        self.assertTrue(server.send(event.session_id, b"alice-pong", stream_id=event.stream_id))
                        seen += 1
                    elif event.data == expected_payloads["bob"]:
                        self.assertTrue(server.send(event.session_id, b"bob-pong", stream_id=event.stream_id))
                        seen += 1
                    if seen == 2:
                        break

            server_task = asyncio.create_task(server_loop())
            await asyncio.gather(
                asyncio.wait_for(alice.connect(), timeout=5),
                asyncio.wait_for(bob.connect(), timeout=5),
            )

            self.assertTrue(alice.send(expected_payloads["alice"], stream_id=31))
            self.assertTrue(bob.send(expected_payloads["bob"], stream_id=37))

            async def collect_reply(client: Client, expected: bytes) -> bytes:
                async for event in client.events():
                    if isinstance(event, DataEvent):
                        self.assertEqual(event.data, expected)
                        return event.data
                raise AssertionError("client event stream ended before reply")

            replies["alice"], replies["bob"] = await asyncio.gather(
                collect_reply(alice, b"alice-pong"),
                collect_reply(bob, b"bob-pong"),
            )

            await asyncio.wait_for(server_task, timeout=5)
            self.assertEqual(replies["alice"], b"alice-pong")
            self.assertEqual(replies["bob"], b"bob-pong")
