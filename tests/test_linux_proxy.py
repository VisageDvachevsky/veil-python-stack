from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core.vpn import VpnPacket  # noqa: E402
from veil_core.linux_proxy import LinuxTunConfig, LinuxVpnProxy  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
