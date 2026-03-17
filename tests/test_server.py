from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core import server as server_mod  # noqa: E402
from veil_core.events import DataEvent, ErrorEvent  # noqa: E402


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

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def send(self, session_id: int, data: bytes, stream_id: int) -> bool:
        return True

    def disconnect(self, session_id: int) -> bool:
        self.disconnect_calls.append(session_id)
        return True

    def stats(self) -> dict:
        return {"active_sessions": 0}


class ServerWrapperTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.orig_ext_available = server_mod._EXT_AVAILABLE
        self.orig_ext = server_mod._ext
        server_mod._EXT_AVAILABLE = True
        server_mod._ext = types.SimpleNamespace(
            NodeConfig=FakeNodeConfig,
            VeilNode=FakeVeilNode,
        )

    def tearDown(self) -> None:
        server_mod._EXT_AVAILABLE = self.orig_ext_available
        server_mod._ext = self.orig_ext

    def test_constructor_forwards_session_idle_timeout(self) -> None:
        server = server_mod.Server(4433, session_idle_timeout_ms=654)
        self.assertEqual(server._node.cfg.session_idle_timeout_ms, 654)

    def test_disconnect_passes_through_to_node(self) -> None:
        server = server_mod.Server(4433)
        self.assertTrue(server.disconnect(888))
        self.assertEqual(server._node.disconnect_calls, [888])

    async def test_events_drain_queued_items_after_stop(self) -> None:
        server = server_mod.Server(4433)
        server.start()
        await server._queue.put(DataEvent(session_id=2, stream_id=5, data=b"pong"))
        server.stop()

        events = []
        async for event in server.events():
            events.append(event)

        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], DataEvent)
        self.assertEqual(events[0].stream_id, 5)

    async def test_async_context_manager_starts_and_stops_node(self) -> None:
        server = server_mod.Server(4433)

        async with server as active_server:
            self.assertIs(active_server, server)
            self.assertTrue(server._running)
            self.assertEqual(server._node.start_calls, 1)

        self.assertFalse(server._running)
        self.assertEqual(server._node.stop_calls, 1)

    async def test_recv_filters_session_without_losing_other_events(self) -> None:
        server = server_mod.Server(4433)
        server.start()
        await server._queue.put(DataEvent(session_id=10, stream_id=1, data=b"other"))
        await server._queue.put(DataEvent(session_id=20, stream_id=1, data=b"target"))

        matched = await server.recv(session_id=20, timeout=0.1)

        self.assertEqual(matched.session_id, 20)
        preserved = await server.next_event(timeout=0.1)
        self.assertIsInstance(preserved, DataEvent)
        self.assertEqual(preserved.session_id, 10)
        server.stop()

    async def test_recv_timeout_restores_skipped_events(self) -> None:
        server = server_mod.Server(4433)
        server.start()
        queued = ErrorEvent(session_id=8, message="pending")
        await server._queue.put(queued)

        with self.assertRaises(asyncio.TimeoutError):
            await server.recv(stream_id=77, timeout=0.01)

        preserved = await server.next_event(timeout=0.1)
        self.assertEqual(preserved, queued)
        server.stop()


if __name__ == "__main__":
    unittest.main()
