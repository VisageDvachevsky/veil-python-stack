from __future__ import annotations

import asyncio
import fcntl
import os
import struct
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from veil_core.vpn import VpnClient, VpnConnection, VpnPacket, VpnServer


TUN_DEVICE_PATH = "/dev/net/tun"
TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000
DEFAULT_TUN_READ_SIZE = 65535


def _run_ip_command(command: Sequence[str]) -> None:
    subprocess.run(command, check=True)


@dataclass(frozen=True)
class LinuxTunConfig:
    name: str = "veil0"
    address_cidr: str = "10.200.0.1/30"
    peer_address: str | None = None
    mtu: int = 1300
    routes: tuple[str, ...] = field(default_factory=tuple)

    def build_setup_commands(self, interface_name: str) -> list[list[str]]:
        commands: list[list[str]] = []
        address_command = ["ip", "addr", "replace", self.address_cidr]
        if self.peer_address:
            address_command.extend(["peer", self.peer_address])
        address_command.extend(["dev", interface_name])
        commands.append(address_command)
        commands.append(["ip", "link", "set", "dev", interface_name, "mtu", str(self.mtu), "up"])
        for route in self.routes:
            commands.append(["ip", "route", "replace", route, "dev", interface_name])
        return commands

    def build_cleanup_commands(self, interface_name: str) -> list[list[str]]:
        return [["ip", "link", "delete", "dev", interface_name]]


class LinuxTunDevice:
    def __init__(
        self,
        *,
        fd: int,
        name: str,
        config: LinuxTunConfig,
        read_size: int = DEFAULT_TUN_READ_SIZE,
        command_runner: Callable[[Sequence[str]], None] = _run_ip_command,
    ) -> None:
        self._fd = fd
        self._name = name
        self._config = config
        self._read_size = read_size
        self._command_runner = command_runner
        self._closed = False

    @classmethod
    def open(
        cls,
        config: LinuxTunConfig,
        *,
        command_runner: Callable[[Sequence[str]], None] = _run_ip_command,
        read_size: int = DEFAULT_TUN_READ_SIZE,
    ) -> "LinuxTunDevice":
        fd = os.open(TUN_DEVICE_PATH, os.O_RDWR)
        try:
            requested_name = config.name.encode("ascii")[:15]
            ifreq = struct.pack("16sH22x", requested_name, IFF_TUN | IFF_NO_PI)
            response = fcntl.ioctl(fd, TUNSETIFF, ifreq)
            actual_name = struct.unpack("16sH22x", response)[0].split(b"\x00", 1)[0].decode("ascii")
            device = cls(
                fd=fd,
                name=actual_name,
                config=config,
                read_size=read_size,
                command_runner=command_runner,
            )
            device.configure()
            return device
        except Exception:
            os.close(fd)
            raise

    @property
    def name(self) -> str:
        return self._name

    def configure(self) -> None:
        for command in self._config.build_setup_commands(self._name):
            self._command_runner(command)

    async def read_packet(self) -> bytes:
        return await asyncio.to_thread(os.read, self._fd, self._read_size)

    async def write_packet(self, payload: bytes) -> int:
        return await asyncio.to_thread(os.write, self._fd, payload)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            for command in self._config.build_cleanup_commands(self._name):
                try:
                    self._command_runner(command)
                except Exception:
                    pass
        finally:
            os.close(self._fd)


class LinuxVpnProxy:
    def __init__(self, tun: Any, connection: VpnConnection) -> None:
        self._tun = tun
        self._connection = connection

    async def run(self) -> str:
        tun_to_vpn = asyncio.create_task(self._pump_tun_to_vpn(), name="tun-to-vpn")
        vpn_to_tun = asyncio.create_task(self._pump_vpn_to_tun(), name="vpn-to-tun")
        failure: BaseException | None = None
        reason: str | None = None
        try:
            done, pending = await asyncio.wait(
                {tun_to_vpn, vpn_to_tun},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            failure = next((task.exception() for task in done if task.exception() is not None), None)
            reason = self._connection.close_reason
            if failure is not None and not self._is_expected_failure(failure):
                raise failure
            return reason or "proxy_stopped"
        except asyncio.CancelledError:
            if not self._connection.is_closed:
                try:
                    reason = await self._connection.close("proxy_cancelled")
                except Exception:
                    reason = reason or "proxy_cancelled"
            raise
        finally:
            for task in (tun_to_vpn, vpn_to_tun):
                if not task.done():
                    task.cancel()
            await asyncio.gather(tun_to_vpn, vpn_to_tun, return_exceptions=True)
            if not self._connection.is_closed:
                try:
                    reason = await self._connection.close("proxy_stopped")
                except Exception:
                    reason = reason or "proxy_stopped"
            try:
                self._tun.close()
            except Exception:
                pass

    def _is_expected_failure(self, failure: BaseException) -> bool:
        if isinstance(failure, (EOFError, ConnectionError)):
            return True
        if isinstance(failure, RuntimeError) and (
            self._connection.is_closed or "VPN connection is closed" in str(failure)
        ):
            return True
        return False

    async def _pump_tun_to_vpn(self) -> None:
        while True:
            packet = await self._tun.read_packet()
            if not packet:
                raise EOFError("TUN interface returned EOF")
            self._connection.send_packet(packet)

    async def _pump_vpn_to_tun(self) -> None:
        while True:
            packet: VpnPacket = await self._connection.recv_packet()
            await self._tun.write_packet(packet.payload)


class LinuxVpnProxyClient:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        tun_config: LinuxTunConfig,
        local_name: str = "client",
        packet_mtu: int = 1300,
        reconnect_delay: float = 2.0,
        max_reconnect_delay: float = 15.0,
        **vpn_kwargs: Any,
    ) -> None:
        self._vpn_client = VpnClient(
            host=host,
            port=port,
            local_name=local_name,
            packet_mtu=packet_mtu,
            **vpn_kwargs,
        )
        self._tun_config = tun_config
        self._reconnect_delay = reconnect_delay
        self._max_reconnect_delay = max_reconnect_delay

    async def run_once(self) -> str:
        async with self._vpn_client as client:
            connection = await client.connect()
            try:
                tun = LinuxTunDevice.open(self._tun_config)
            except Exception:
                await connection.close("tun_open_failed")
                raise
            proxy = LinuxVpnProxy(tun, connection)
            return await proxy.run()

    async def run_forever(self) -> None:
        delay = self._reconnect_delay
        while True:
            try:
                await self.run_once()
                delay = self._reconnect_delay
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(delay)
                delay = min(self._max_reconnect_delay, max(self._reconnect_delay, delay * 2.0))

    async def run(self) -> str:
        return await self.run_once()


class LinuxVpnProxyServer:
    def __init__(
        self,
        port: int,
        *,
        tun_config: LinuxTunConfig,
        host: str = "0.0.0.0",
        local_name: str = "server",
        packet_mtu: int = 1300,
        **vpn_kwargs: Any,
    ) -> None:
        self._vpn_server = VpnServer(
            port=port,
            host=host,
            local_name=local_name,
            packet_mtu=packet_mtu,
            **vpn_kwargs,
        )
        self._tun_config = tun_config

    async def serve_once(self) -> str:
        async with self._vpn_server as server:
            connection = await server.accept()
            try:
                tun = LinuxTunDevice.open(self._tun_config)
            except Exception:
                await connection.close("tun_open_failed")
                raise
            proxy = LinuxVpnProxy(tun, connection)
            return await proxy.run()

    async def serve_forever(self) -> None:
        async with self._vpn_server as server:
            while True:
                connection = await server.accept()
                try:
                    tun = LinuxTunDevice.open(self._tun_config)
                except Exception:
                    await connection.close("tun_open_failed")
                    raise
                proxy = LinuxVpnProxy(tun, connection)
                try:
                    await proxy.run()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await asyncio.sleep(1.0)
                finally:
                    tun.close()
