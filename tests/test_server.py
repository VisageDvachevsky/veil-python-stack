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
        self.send_calls: list[tuple[int, bytes, int]] = []
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def send(self, session_id: int, data: bytes, stream_id: int) -> bool:
        self.send_calls.append((session_id, data, stream_id))
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

    async def test_accept_returns_session_wrapper_without_losing_other_events(self) -> None:
        server = server_mod.Server(4433)
        server.start()
        await server._queue.put(DataEvent(session_id=10, stream_id=1, data=b"pending"))
        server._on_new_connection(123, "127.0.0.1", 4567)
        await asyncio.sleep(0)

        session = await server.accept(timeout=0.1)

        self.assertEqual(session.session_id, 123)
        self.assertEqual(session.remote_host, "127.0.0.1")
        self.assertEqual(session.remote_port, 4567)
        self.assertTrue(session.send(b"hello"))
        self.assertEqual(server._node.send_calls, [(123, b"hello", 0)])
        preserved = await server.next_event(timeout=0.1)
        self.assertIsInstance(preserved, DataEvent)
        self.assertEqual(preserved.data, b"pending")
        server.stop()

    async def test_session_recv_uses_bound_session_id(self) -> None:
        server = server_mod.Server(4433)
        server.start()
        session = server_mod.Session(server, session_id=303, remote_host="127.0.0.1", remote_port=5000)
        server._on_data(404, 2, b"other")
        server._on_data(303, 7, b"target")
        await asyncio.sleep(0)

        matched = await session.recv(timeout=0.1, stream_id=7)

        self.assertEqual(matched.session_id, 303)
        self.assertEqual(matched.data, b"target")
        preserved = await server.next_event(timeout=0.1)
        self.assertIsInstance(preserved, DataEvent)
        self.assertEqual(preserved.session_id, 404)
        server.stop()

    async def test_session_send_json_serializes_payload(self) -> None:
        server = server_mod.Server(4433)
        session = server_mod.Session(server, session_id=808, remote_host="127.0.0.1", remote_port=6000)

        self.assertTrue(session.send_json({"type": "ack", "ok": True}))
        self.assertEqual(
            server._node.send_calls,
            [(808, b'{"type":"ack","ok":true}', 1)],
        )

    async def test_session_recv_json_decodes_payload(self) -> None:
        server = server_mod.Server(4433)
        server.start()
        session = server_mod.Session(server, session_id=808, remote_host="127.0.0.1", remote_port=6000)
        server._on_data(808, 1, b'{"kind":"hello","n":7}')
        await asyncio.sleep(0)

        message = await session.recv_json(timeout=0.1)

        self.assertEqual(message.session_id, 808)
        self.assertEqual(message.stream_id, 1)
        self.assertEqual(message.body, {"kind": "hello", "n": 7})
        server.stop()

    async def test_session_recv_event_filters_by_bound_session(self) -> None:
        server = server_mod.Server(4433)
        server.start()
        session = server_mod.Session(server, session_id=515, remote_host="127.0.0.1", remote_port=6000)
        server._on_disconnected(999, "other")
        server._on_disconnected(515, "target")
        await asyncio.sleep(0)

        event = await session.recv_event(timeout=0.1, predicate=lambda item: isinstance(item, DisconnectedEvent))

        self.assertIsInstance(event, DisconnectedEvent)
        self.assertEqual(event.session_id, 515)
        self.assertEqual(event.reason, "target")
        preserved = await server.next_event(timeout=0.1)
        self.assertEqual(preserved.session_id, 999)
        server.stop()

    async def test_accept_waiter_does_not_steal_session_events(self) -> None:
        server = server_mod.Server(4433)
        server.start()
        session = server_mod.Session(server, session_id=303, remote_host="127.0.0.1", remote_port=5000)

        accept_task = asyncio.create_task(server.accept(timeout=5.0))
        await asyncio.sleep(0)
        server._on_data(303, 7, b"target")
        await asyncio.sleep(0)

        matched = await session.recv(timeout=0.1, stream_id=7)

        self.assertEqual(matched.session_id, 303)
        self.assertEqual(matched.data, b"target")
        accept_task.cancel()
        await asyncio.gather(accept_task, return_exceptions=True)
        server.stop()

    async def test_session_waiters_do_not_steal_other_session_events(self) -> None:
        server = server_mod.Server(4433)
        server.start()
        session_a = server_mod.Session(server, session_id=101, remote_host="127.0.0.1", remote_port=5000)
        session_b = server_mod.Session(server, session_id=202, remote_host="127.0.0.1", remote_port=5001)

        waiter_a = asyncio.create_task(session_a.recv(timeout=5.0, stream_id=7))
        await asyncio.sleep(0)
        server._on_data(202, 7, b"for-b")
        await asyncio.sleep(0)

        matched_b = await session_b.recv(timeout=0.1, stream_id=7)

        self.assertEqual(matched_b.session_id, 202)
        self.assertEqual(matched_b.data, b"for-b")
        waiter_a.cancel()
        await asyncio.gather(waiter_a, return_exceptions=True)
        server.stop()


if __name__ == "__main__":
    unittest.main()
