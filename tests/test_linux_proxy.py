from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core.vpn import VpnPacket  # noqa: E402
from veil_core.linux_proxy import (  # noqa: E402
    LinuxClientAddressPool,
    LinuxTunConfig,
    LinuxVpnProxy,
    LinuxVpnProxyClient,
    LinuxVpnProxyServer,
    _extract_destination_ipv4,
)


class FakeTun:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False
        self._reads = [b"ip-from-tun"]
        self._reads_exhausted = asyncio.Event()

    async def read_packet(self) -> bytes:
        if self._reads:
            return self._reads.pop(0)
        await self._reads_exhausted.wait()
        raise EOFError("tun closed")

    async def write_packet(self, payload: bytes) -> int:
        self.writes.append(payload)
        self._reads_exhausted.set()
        return len(payload)

    def close(self) -> None:
        self.closed = True
        self._reads_exhausted.set()


class FakeConnection:
    def __init__(self) -> None:
        self.sent_packets: list[bytes] = []
        self.is_closed = False
        self.close_reason: str | None = None
        self._packets = [VpnPacket(session_id=11, payload=b"ip-from-vpn")]
        self._stopped = asyncio.Event()

    def send_packet(self, payload: bytes) -> bool:
        self.sent_packets.append(payload)
        return True

    async def recv_packet(self) -> VpnPacket:
        if self._packets:
            return self._packets.pop(0)
        await self._stopped.wait()
        raise ConnectionError("connection closed")

    async def close(self, reason: str = "proxy_stopped") -> str:
        self.is_closed = True
        self.close_reason = reason
        self._stopped.set()
        return reason


class LinuxProxyTests(unittest.IsolatedAsyncioTestCase):
    def test_tun_config_builds_expected_ip_commands(self) -> None:
        config = LinuxTunConfig(
            name="veil9",
            address_cidr="10.55.0.1/30",
            peer_address="10.55.0.2",
            mtu=1280,
            routes=("10.60.0.0/24", "10.61.0.0/24"),
        )

        commands = config.build_setup_commands("veil9")

        self.assertEqual(
            commands,
            [
                ["ip", "addr", "replace", "10.55.0.1/30", "peer", "10.55.0.2", "dev", "veil9"],
                ["ip", "link", "set", "dev", "veil9", "mtu", "1280", "up"],
                ["ip", "route", "replace", "10.60.0.0/24", "dev", "veil9"],
                ["ip", "route", "replace", "10.61.0.0/24", "dev", "veil9"],
            ],
        )
        self.assertEqual(config.build_cleanup_commands("veil9"), [["ip", "link", "delete", "dev", "veil9"]])

    async def test_linux_vpn_proxy_bridges_packets_both_directions(self) -> None:
        tun = FakeTun()
        connection = FakeConnection()
        proxy = LinuxVpnProxy(tun, connection)

        reason = await proxy.run()

        self.assertEqual(reason, "proxy_stopped")
        self.assertEqual(connection.sent_packets, [b"ip-from-tun"])
        self.assertEqual(tun.writes, [b"ip-from-vpn"])
        self.assertTrue(connection.is_closed)
        self.assertTrue(tun.closed)

    async def test_linux_vpn_proxy_closes_tun_on_cancel(self) -> None:
        class BlockingTun(FakeTun):
            async def read_packet(self) -> bytes:
                await asyncio.sleep(10)
                return b""

        class BlockingConnection(FakeConnection):
            async def recv_packet(self) -> VpnPacket:
                await asyncio.sleep(10)
                raise ConnectionError("connection closed")

        tun = BlockingTun()
        connection = BlockingConnection()
        proxy = LinuxVpnProxy(tun, connection)

        task = asyncio.create_task(proxy.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(connection.is_closed)
        self.assertEqual(connection.close_reason, "proxy_cancelled")
        self.assertTrue(tun.closed)

    async def test_linux_vpn_proxy_treats_closed_connection_runtime_error_as_expected(self) -> None:
        class ClosedConnectionTun(FakeTun):
            async def read_packet(self) -> bytes:
                return b"late-packet"

        class ClosedConnection(FakeConnection):
            def __init__(self) -> None:
                super().__init__()
                self.is_closed = True
                self.close_reason = "remote_disconnect"

            def send_packet(self, payload: bytes) -> bool:
                raise RuntimeError("VPN connection is closed")

            async def recv_packet(self) -> VpnPacket:
                raise ConnectionError("connection closed")

        tun = ClosedConnectionTun()
        connection = ClosedConnection()
        proxy = LinuxVpnProxy(tun, connection)

        reason = await proxy.run()

        self.assertEqual(reason, "remote_disconnect")
        self.assertTrue(tun.closed)

    def test_client_address_pool_allocates_distinct_client_ips(self) -> None:
        pool = LinuxClientAddressPool("10.77.0.1/29")

        first = pool.allocate(100)
        second = pool.allocate(200)

        self.assertEqual(first.client_address_cidr, "10.77.0.2/29")
        self.assertEqual(second.client_address_cidr, "10.77.0.3/29")
        self.assertEqual(pool.session_for_destination("10.77.0.2"), 100)
        self.assertEqual(pool.session_for_destination("10.77.0.3"), 200)

        pool.release(100)
        recycled = pool.allocate(300, preferred_address="10.77.0.2/29")
        self.assertEqual(recycled.client_ip, "10.77.0.2")

    def test_extract_destination_ipv4_reads_inner_packet_destination(self) -> None:
        payload = bytes(
            [
                0x45,
                0x00,
                0x00,
                0x14,
                0x00,
                0x00,
                0x00,
                0x00,
                0x40,
                0x11,
                0x00,
                0x00,
                10,
                77,
                0,
                2,
                10,
                77,
                0,
                9,
            ]
        )

        self.assertEqual(_extract_destination_ipv4(payload), "10.77.0.9")
        self.assertIsNone(_extract_destination_ipv4(b""))

    def test_dynamic_client_tun_config_uses_server_assigned_address(self) -> None:
        proxy = LinuxVpnProxyClient(
            host="127.0.0.1",
            port=4433,
            tun_config=LinuxTunConfig(name="veiltest", address_cidr=None, peer_address=None),
        )

        class DummyConnection:
            peer_parameters = {
                "tun_address": "10.88.0.2/24",
                "tun_peer": "",
                "routes": ["10.99.0.0/24"],
            }

        resolved = proxy._resolve_tun_config(DummyConnection())

        self.assertEqual(resolved.address_cidr, "10.88.0.2/24")
        self.assertIsNone(resolved.peer_address)
        self.assertEqual(resolved.routes, ("10.99.0.0/24",))

    def test_multi_client_server_ready_payload_contains_assigned_tunnel(self) -> None:
        server = LinuxVpnProxyServer(
            port=4433,
            tun_config=LinuxTunConfig(name="veil0", address_cidr="10.90.0.1/24"),
        )

        class DummyConnection:
            session_id = 501
            peer_parameters = {"requested_tun_address": "10.90.0.44/24"}

        payload = server._build_ready_payload(DummyConnection())

        self.assertEqual(payload["tun_address"], "10.90.0.44/24")
        self.assertEqual(payload["tun_peer"], "")
        self.assertEqual(payload["tunnel_mode"], "dynamic")


if __name__ == "__main__":
    unittest.main()
