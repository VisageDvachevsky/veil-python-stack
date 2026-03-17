from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core import client as client_mod  # noqa: E402
from veil_core.events import DataEvent, DisconnectedEvent, ErrorEvent  # noqa: E402


class FakeNodeConfig:
    def __init__(self) -> None:
        self.host = ""
        self.port = 0
        self.local_port = 0
        self.protocol_wrapper = "none"
        self.persona_preset = "custom"
        self.enable_http_handshake_emulation = False
        self.rotation_interval_seconds = 30
        self.handshake_timeout_ms = 5000
        self.session_idle_timeout_ms = 0
        self.mtu = 1400
        self.psk = b""
        self.is_client = False


class FakeVeilNode:
    def __init__(self, cfg: FakeNodeConfig) -> None:
        self.cfg = cfg
        self.on_new_connection = None
        self.on_data = None
        self.on_disconnected = None
        self.on_error = None
        self.disconnect_calls: list[int] = []
        self.start_calls = 0
        self.stop_calls = 0
        self.connect_calls = 0
        self.connect_handler = None
        self.stats_payload = {"active_sessions": 0}
        self.send_calls: list[tuple[int, bytes, int]] = []

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def connect(self, host: str, port: int) -> None:
        self.connect_calls += 1
        if self.connect_handler is not None:
            self.connect_handler(host, port)
            return
        if self.on_error is not None:
            self.on_error(0, f"connect failed for {host}:{port}")

    def send(self, session_id: int, data: bytes, stream_id: int) -> bool:
        self.send_calls.append((session_id, data, stream_id))
        return True

    def disconnect(self, session_id: int) -> bool:
        self.disconnect_calls.append(session_id)
        return True

    def stats(self) -> dict:
        return dict(self.stats_payload)


class ClientWrapperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.orig_ext_available = client_mod._EXT_AVAILABLE
        self.orig_ext = client_mod._ext
        client_mod._EXT_AVAILABLE = True
        client_mod._ext = types.SimpleNamespace(
            NodeConfig=FakeNodeConfig,
            VeilNode=FakeVeilNode,
        )

    def tearDown(self) -> None:
        client_mod._EXT_AVAILABLE = self.orig_ext_available
        client_mod._ext = self.orig_ext

    async def test_connect_raises_runtime_error_on_error_event(self) -> None:
        client = client_mod.Client("127.0.0.1", 4433)
        client.start()

        with self.assertRaisesRegex(RuntimeError, "connect failed"):
            await client.connect()

        client.stop()

    async def test_disconnected_callback_clears_active_session(self) -> None:
        client = client_mod.Client("127.0.0.1", 4433)
        client.start()
        client._session_id = 123

        client._on_disconnected(123, "peer disconnected")
        await asyncio.sleep(0)

        self.assertIsNone(client._session_id)
        queued = await asyncio.wait_for(client._queue.get(), timeout=1)
        self.assertIsInstance(queued, DisconnectedEvent)
        self.assertEqual(queued.reason, "peer disconnected")

        client.stop()

    async def test_constructor_forwards_session_idle_timeout(self) -> None:
        client = client_mod.Client("127.0.0.1", 4433, session_idle_timeout_ms=321)
        self.assertEqual(client._node.cfg.session_idle_timeout_ms, 321)

    async def test_disconnect_clears_cached_session_id(self) -> None:
        client = client_mod.Client("127.0.0.1", 4433)
        client._session_id = 777

        self.assertTrue(client.disconnect())
        self.assertIsNone(client._session_id)
        self.assertEqual(client._node.disconnect_calls, [777])

    async def test_events_drain_queued_items_after_stop(self) -> None:
        client = client_mod.Client("127.0.0.1", 4433)
        client.start()
        await client._queue.put(DataEvent(session_id=1, stream_id=9, data=b"payload"))
        client.stop()

        events = []
        async for event in client.events():
            events.append(event)

        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], DataEvent)
        self.assertEqual(events[0].stream_id, 9)

    async def test_async_context_manager_starts_and_stops_node(self) -> None:
        client = client_mod.Client("127.0.0.1", 4433)

        async with client as active_client:
            self.assertIs(active_client, client)
            self.assertTrue(client._running)
            self.assertEqual(client._node.start_calls, 1)

        self.assertFalse(client._running)
        self.assertEqual(client._node.stop_calls, 1)

    async def test_connect_ignores_stale_queue_events(self) -> None:
        client = client_mod.Client("127.0.0.1", 4433)
        client.start()
        await client._queue.put(DataEvent(session_id=7, stream_id=3, data=b"stale"))
        client._node.connect_handler = lambda host, port: client._node.on_new_connection(555, host, port)

        event = await client.connect()

        self.assertEqual(event.session_id, 555)
        queued = await asyncio.wait_for(client._queue.get(), timeout=1)
        self.assertIsInstance(queued, DataEvent)
        self.assertEqual(queued.data, b"stale")
        client.stop()

    async def test_stop_unblocks_pending_connect(self) -> None:
        client = client_mod.Client("127.0.0.1", 4433)
        client.start()
        client._node.connect_handler = lambda host, port: None

        task = asyncio.create_task(client.connect())
        await asyncio.sleep(0)
        client.stop()

        with self.assertRaisesRegex(RuntimeError, "Client stopped"):
            await task

    async def test_recv_filters_stream_without_losing_other_events(self) -> None:
        client = client_mod.Client("127.0.0.1", 4433)
        client.start()
        await client._queue.put(DataEvent(session_id=1, stream_id=1, data=b"one"))
        await client._queue.put(DataEvent(session_id=1, stream_id=7, data=b"seven"))

        matched = await client.recv(stream_id=7, timeout=0.1)

        self.assertEqual(matched.data, b"seven")
        preserved = await client.next_event(timeout=0.1)
        self.assertIsInstance(preserved, DataEvent)
        self.assertEqual(preserved.stream_id, 1)
        self.assertEqual(preserved.data, b"one")
        client.stop()

    async def test_recv_timeout_restores_skipped_events(self) -> None:
        client = client_mod.Client("127.0.0.1", 4433)
        client.start()
        queued = ErrorEvent(session_id=3, message="still pending")
        await client._queue.put(queued)

        with self.assertRaises(asyncio.TimeoutError):
            await client.recv(stream_id=99, timeout=0.01)

        preserved = await client.next_event(timeout=0.1)
        self.assertEqual(preserved, queued)
        client.stop()

    async def test_connect_session_returns_session_wrapper(self) -> None:
        client = client_mod.Client("127.0.0.1", 4433)
        client.start()
        client._node.connect_handler = lambda host, port: client._node.on_new_connection(555, host, port)

        session = await client.connect_session()

        self.assertEqual(session.session_id, 555)
        self.assertEqual(session.remote_host, "127.0.0.1")
        self.assertEqual(session.remote_port, 4433)
        self.assertTrue(session.send(b"hello"))
        self.assertEqual(client._node.send_calls, [(555, b"hello", 1)])
        client.stop()

    async def test_session_recv_filters_by_bound_session(self) -> None:
        client = client_mod.Client("127.0.0.1", 4433)
        client.start()
        client._session_id = 101
        session = client.session()
        await client._queue.put(DataEvent(session_id=202, stream_id=1, data=b"other"))
        await client._queue.put(DataEvent(session_id=101, stream_id=9, data=b"target"))

        matched = await session.recv(timeout=0.1, stream_id=9)

        self.assertEqual(matched.session_id, 101)
        self.assertEqual(matched.data, b"target")
        preserved = await client.next_event(timeout=0.1)
        self.assertIsInstance(preserved, DataEvent)
        self.assertEqual(preserved.session_id, 202)
        client.stop()

    async def test_session_send_json_serializes_payload(self) -> None:
        client = client_mod.Client("127.0.0.1", 4433)
        client._session_id = 919
        session = client.session()

        self.assertTrue(session.send_json({"type": "ping", "seq": 1}, stream_id=5))
        self.assertEqual(
            client._node.send_calls,
            [(919, b'{"type":"ping","seq":1}', 5)],
        )

    async def test_session_recv_json_decodes_payload(self) -> None:
        client = client_mod.Client("127.0.0.1", 4433)
        client.start()
        client._session_id = 919
        session = client.session()
        await client._queue.put(
            DataEvent(session_id=919, stream_id=3, data=b'{"ok":true,"value":"pong"}')
        )

        message = await session.recv_json(timeout=0.1, stream_id=3)

        self.assertEqual(message.session_id, 919)
        self.assertEqual(message.stream_id, 3)
        self.assertEqual(message.body, {"ok": True, "value": "pong"})
        self.assertEqual(message.raw, b'{"ok":true,"value":"pong"}')
        client.stop()


if __name__ == "__main__":
    unittest.main()
