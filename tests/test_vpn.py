from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core._event_buffer import EventBuffer  # noqa: E402
from veil_core.events import DataEvent, DisconnectedEvent, Event  # noqa: E402
from veil_core.session import Session  # noqa: E402
from veil_core.vpn import VpnConnection  # noqa: E402


class FakePeerOwner:
    def __init__(self, *, session_id: int) -> None:
        self.session_id = session_id
        self.peer: FakePeerOwner | None = None
        self.queue: asyncio.Queue[Event] = asyncio.Queue()
        self.event_buffer = EventBuffer()
        self.disconnect_calls: list[int] = []

    def bind_peer(self, peer: "FakePeerOwner") -> None:
        self.peer = peer

    def send(self, session_id: int, data: bytes, *, stream_id: int) -> bool:
        if self.peer is None:
            raise RuntimeError("peer is not bound")
        self.peer.queue.put_nowait(
            DataEvent(session_id=self.peer.session_id, stream_id=stream_id, data=data)
        )
        return True

    def disconnect(self, session_id: int) -> bool:
        self.disconnect_calls.append(session_id)
        self.queue.put_nowait(DisconnectedEvent(session_id=self.session_id, reason="local_disconnect"))
        if self.peer is not None:
            self.peer.queue.put_nowait(
                DisconnectedEvent(session_id=self.peer.session_id, reason="remote_disconnect")
            )
        return True

    async def recv_event(self, *, timeout: float | None = None, predicate=None):
        matcher = predicate or (lambda _event: True)
        return await self.event_buffer.recv_event(self.queue, timeout=timeout, predicate=matcher)


class VpnOverlayTests(unittest.IsolatedAsyncioTestCase):
    def make_pair(self) -> tuple[Session, Session]:
        client_owner = FakePeerOwner(session_id=1001)
        server_owner = FakePeerOwner(session_id=2002)
        client_owner.bind_peer(server_owner)
        server_owner.bind_peer(client_owner)
        client_session = Session(
            client_owner,
            session_id=client_owner.session_id,
            remote_host="127.0.0.1",
            remote_port=4433,
        )
        server_session = Session(
            server_owner,
            session_id=server_owner.session_id,
            remote_host="127.0.0.1",
            remote_port=50000,
        )
        return client_session, server_session

    async def test_vpn_connection_handshake_and_packet_flow(self) -> None:
        client_session, server_session = self.make_pair()
        client_conn = VpnConnection(client_session, role="client", local_name="alice", packet_mtu=1200)
        server_conn = VpnConnection(server_session, role="server", local_name="edge", packet_mtu=1100)

        accepted, connected = await asyncio.gather(
            server_conn.start(initiator=False, timeout=1.0),
            client_conn.start(initiator=True, timeout=1.0),
        )

        self.assertIs(accepted, server_conn)
        self.assertIs(connected, client_conn)
        self.assertEqual(client_conn.peer_name, "edge")
        self.assertEqual(client_conn.peer_role, "server")
        self.assertEqual(server_conn.peer_name, "alice")
        self.assertEqual(server_conn.peer_role, "client")
        self.assertEqual(client_conn.effective_packet_mtu, 1100)
        self.assertEqual(server_conn.effective_packet_mtu, 1100)

        self.assertTrue(client_conn.send_packet(b"ip-packet"))
        packet = await server_conn.recv_packet(timeout=1.0)
        self.assertEqual(packet.payload, b"ip-packet")
        self.assertEqual(packet.session_id, server_session.session_id)

        self.assertTrue(server_conn.send_packet(b"reply-packet"))
        reply = await client_conn.recv_packet(timeout=1.0)
        self.assertEqual(reply.payload, b"reply-packet")
        self.assertEqual(reply.session_id, client_session.session_id)

        await client_conn.close("done")
        self.assertEqual(await server_conn.wait_closed(timeout=1.0), "done")

    async def test_vpn_connection_rejects_payload_above_negotiated_mtu(self) -> None:
        client_session, server_session = self.make_pair()
        client_conn = VpnConnection(client_session, role="client", local_name="alice", packet_mtu=1200)
        server_conn = VpnConnection(server_session, role="server", local_name="edge", packet_mtu=900)

        await asyncio.gather(
            server_conn.start(initiator=False, timeout=1.0),
            client_conn.start(initiator=True, timeout=1.0),
        )

        with self.assertRaises(ValueError):
            client_conn.send_packet(b"x" * 901)

        await client_conn.close("done")
        self.assertEqual(await server_conn.wait_closed(timeout=1.0), "done")

    async def test_vpn_connection_close_propagates_reason(self) -> None:
        client_session, server_session = self.make_pair()
        client_conn = VpnConnection(client_session, role="client", local_name="alice", packet_mtu=1200)
        server_conn = VpnConnection(server_session, role="server", local_name="edge", packet_mtu=1200)

        await asyncio.gather(
            server_conn.start(initiator=False, timeout=1.0),
            client_conn.start(initiator=True, timeout=1.0),
        )

        close_task = asyncio.create_task(server_conn.wait_closed(timeout=1.0))
        closed_reason = await client_conn.close("shutdown")
        self.assertEqual(closed_reason, "local_disconnect")

        remote_reason = await close_task
        self.assertEqual(remote_reason, "shutdown")

    async def test_vpn_connection_keepalive_keeps_session_alive(self) -> None:
        client_session, server_session = self.make_pair()
        client_conn = VpnConnection(
            client_session,
            role="client",
            local_name="alice",
            packet_mtu=1200,
            keepalive_interval=0.05,
            keepalive_timeout=0.2,
        )
        server_conn = VpnConnection(
            server_session,
            role="server",
            local_name="edge",
            packet_mtu=1200,
            keepalive_interval=0.05,
            keepalive_timeout=0.2,
        )

        await asyncio.gather(
            server_conn.start(initiator=False, timeout=1.0),
            client_conn.start(initiator=True, timeout=1.0),
        )

        await asyncio.sleep(0.18)
        self.assertFalse(client_conn.is_closed)
        self.assertFalse(server_conn.is_closed)

        with self.assertRaises(asyncio.TimeoutError):
            await client_conn.recv_control(timeout=0.05)

        await client_conn.close("done")
        self.assertEqual(await server_conn.wait_closed(timeout=1.0), "done")


if __name__ == "__main__":
    unittest.main()
