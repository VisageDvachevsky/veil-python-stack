from __future__ import annotations

import asyncio
import contextlib
import fcntl
import ipaddress
import os
import struct
import subprocess
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Sequence

from veil_core.vpn import VpnClient, VpnConnection, VpnPacket, VpnServer


TUN_DEVICE_PATH = "/dev/net/tun"
TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000
DEFAULT_TUN_READ_SIZE = 65535
SEND_BACKPRESSURE_RETRY_DELAY = 0.01


def _run_ip_command(command: Sequence[str]) -> None:
    subprocess.run(command, check=True)


@dataclass(frozen=True)
class LinuxTunConfig:
    name: str = "veil0"
    address_cidr: str | None = "10.200.0.1/30"
    peer_address: str | None = None
    mtu: int = 1300
    routes: tuple[str, ...] = field(default_factory=tuple)

    def build_setup_commands(self, interface_name: str) -> list[list[str]]:
        if not self.address_cidr:
            raise RuntimeError("address_cidr must be resolved before configuring a Linux TUN device")
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

    def resolved(
        self,
        *,
        address_cidr: str,
        peer_address: str | None,
        routes: Sequence[str] | None = None,
    ) -> "LinuxTunConfig":
        return replace(
            self,
            address_cidr=address_cidr,
            peer_address=peer_address,
            routes=tuple(routes) if routes is not None else self.routes,
        )


@dataclass(frozen=True)
class LinuxClientTunnelLease:
    session_id: int
    client_ip: str
    client_address_cidr: str
    peer_address: str
    network_cidr: str


class LinuxClientAddressPool:
    def __init__(self, server_address_cidr: str) -> None:
        interface = ipaddress.ip_interface(server_address_cidr)
        if interface.version != 4:
            raise ValueError("Linux multi-client VPN allocator currently supports IPv4 only")
        self._interface = interface
        self._server_ip = str(interface.ip)
        self._network = interface.network
        self._leases_by_session: dict[int, LinuxClientTunnelLease] = {}
        self._sessions_by_ip: dict[str, int] = {}

    @property
    def server_ip(self) -> str:
        return self._server_ip

    @property
    def network_cidr(self) -> str:
        return str(self._network)

    def allocate(self, session_id: int, *, preferred_address: str | None = None) -> LinuxClientTunnelLease:
        if session_id in self._leases_by_session:
            return self._leases_by_session[session_id]

        selected_ip = self._normalize_preferred_address(preferred_address)
        if selected_ip is None:
            for host in self._network.hosts():
                candidate = str(host)
                if self._is_available(candidate):
                    selected_ip = candidate
                    break

        if selected_ip is None:
            raise RuntimeError(f"No free client addresses left in tunnel network {self._network}")

        lease = LinuxClientTunnelLease(
            session_id=session_id,
            client_ip=selected_ip,
            client_address_cidr=f"{selected_ip}/{self._network.prefixlen}",
            peer_address=self._server_ip,
            network_cidr=str(self._network),
        )
        self._leases_by_session[session_id] = lease
        self._sessions_by_ip[selected_ip] = session_id
        return lease

    def release(self, session_id: int) -> None:
        lease = self._leases_by_session.pop(session_id, None)
        if lease is not None:
            self._sessions_by_ip.pop(lease.client_ip, None)

    def session_for_destination(self, destination_ip: str | None) -> int | None:
        if not destination_ip:
            return None
        return self._sessions_by_ip.get(destination_ip)

    def _normalize_preferred_address(self, preferred_address: str | None) -> str | None:
        if not preferred_address:
            return None
        try:
            preferred_ip = ipaddress.ip_interface(preferred_address).ip
        except ValueError:
            try:
                preferred_ip = ipaddress.ip_address(preferred_address)
            except ValueError:
                return None

        if preferred_ip.version != 4:
            return None
        normalized = str(preferred_ip)
        if not self._is_available(normalized):
            return None
        return normalized

    def _is_available(self, ip_text: str) -> bool:
        try:
            candidate = ipaddress.ip_address(ip_text)
        except ValueError:
            return False
        return (
            candidate.version == 4
            and candidate in self._network
            and candidate != self._network.network_address
            and candidate != self._network.broadcast_address
            and ip_text != self._server_ip
            and ip_text not in self._sessions_by_ip
        )


def _extract_destination_ipv4(payload: bytes) -> str | None:
    if len(payload) < 20:
        return None
    if (payload[0] >> 4) != 4:
        return None
    try:
        return str(ipaddress.IPv4Address(payload[16:20]))
    except ipaddress.AddressValueError:
        return None


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
            await _send_packet_with_retry(self._connection, packet)

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
            connection = await client.connect(hello_payload=self._build_hello_payload())
            tun_config = self._resolve_tun_config(connection)
            try:
                tun = LinuxTunDevice.open(tun_config)
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

    def _build_hello_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tunnel_mode": "static" if self._tun_config.address_cidr else "dynamic",
        }
        if self._tun_config.address_cidr:
            payload["requested_tun_address"] = self._tun_config.address_cidr
        if self._tun_config.peer_address:
            payload["requested_tun_peer"] = self._tun_config.peer_address
        if self._tun_config.routes:
            payload["routes"] = list(self._tun_config.routes)
        return payload

    def _resolve_tun_config(self, connection: VpnConnection) -> LinuxTunConfig:
        peer_parameters = connection.peer_parameters
        resolved_address = str(peer_parameters.get("tun_address") or self._tun_config.address_cidr or "").strip()
        if not resolved_address:
            raise RuntimeError("VPN server did not provide a tunnel address and no static client address is configured")

        resolved_peer_raw = peer_parameters.get("tun_peer")
        resolved_peer = (
            str(resolved_peer_raw).strip()
            if resolved_peer_raw is not None and str(resolved_peer_raw).strip()
            else self._tun_config.peer_address
        )

        negotiated_routes = peer_parameters.get("routes")
        routes = self._tun_config.routes
        if isinstance(negotiated_routes, (list, tuple)) and negotiated_routes:
            routes = tuple(str(item) for item in negotiated_routes if str(item).strip())

        return self._tun_config.resolved(
            address_cidr=resolved_address,
            peer_address=resolved_peer,
            routes=routes,
        )


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
        if not tun_config.address_cidr:
            raise ValueError("LinuxVpnProxyServer requires a resolved server tunnel address")
        self._vpn_server = VpnServer(
            port=port,
            host=host,
            local_name=local_name,
            packet_mtu=packet_mtu,
            **vpn_kwargs,
        )
        self._tun_config = replace(tun_config, peer_address=None)
        self._address_pool = LinuxClientAddressPool(self._tun_config.address_cidr)
        self._connections: dict[int, VpnConnection] = {}
        self._pump_tasks: dict[int, asyncio.Task[None]] = {}

    async def serve_once(self) -> str:
        async with self._vpn_server as server:
            tun = LinuxTunDevice.open(self._tun_config)
            try:
                tun_reader = asyncio.create_task(self._pump_tun_to_clients(tun), name="vpn-tun-to-clients")
                connection = await server.accept(ready_payload_factory=self._build_ready_payload)
                self._register_connection(connection, tun)
                try:
                    return await connection.wait_closed()
                finally:
                    tun_reader.cancel()
                    await asyncio.gather(tun_reader, return_exceptions=True)
            finally:
                await self._shutdown_connections()
                tun.close()

    async def serve_forever(self) -> None:
        async with self._vpn_server as server:
            tun = LinuxTunDevice.open(self._tun_config)
            accept_loop = asyncio.create_task(self._accept_loop(server, tun), name="vpn-accept-loop")
            tun_reader = asyncio.create_task(self._pump_tun_to_clients(tun), name="vpn-tun-to-clients")
            try:
                done, pending = await asyncio.wait(
                    {accept_loop, tun_reader},
                    return_when=asyncio.FIRST_EXCEPTION,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    task.result()
            finally:
                accept_loop.cancel()
                tun_reader.cancel()
                await asyncio.gather(accept_loop, tun_reader, return_exceptions=True)
                await self._shutdown_connections()
                tun.close()

    async def _accept_loop(self, server: VpnServer, tun: LinuxTunDevice) -> None:
        while True:
            connection = await server.accept(ready_payload_factory=self._build_ready_payload)
            self._register_connection(connection, tun)

    def _build_ready_payload(self, connection: VpnConnection) -> dict[str, Any]:
        requested_address = connection.peer_parameters.get("requested_tun_address")
        lease = self._address_pool.allocate(
            connection.session_id,
            preferred_address=str(requested_address) if requested_address else None,
        )
        payload: dict[str, Any] = {
            "tunnel_mode": "dynamic",
            "tun_address": lease.client_address_cidr,
            "tun_peer": "",
            "tun_network": lease.network_cidr,
        }
        if self._tun_config.routes:
            payload["routes"] = list(self._tun_config.routes)
        return payload

    def _register_connection(self, connection: VpnConnection, tun: LinuxTunDevice) -> None:
        self._connections[connection.session_id] = connection
        task = asyncio.create_task(
            self._pump_client_to_tun(connection, tun),
            name=f"vpn-client-to-tun-{connection.session_id:x}",
        )
        self._pump_tasks[connection.session_id] = task

    async def _pump_tun_to_clients(self, tun: LinuxTunDevice) -> None:
        while True:
            packet = await tun.read_packet()
            if not packet:
                raise EOFError("server TUN interface returned EOF")
            destination_ip = _extract_destination_ipv4(packet)
            session_id = self._address_pool.session_for_destination(destination_ip)
            if session_id is None:
                continue
            connection = self._connections.get(session_id)
            if connection is None or connection.is_closed:
                continue
            try:
                await _send_packet_with_retry(connection, packet)
            except (RuntimeError, ValueError):
                continue

    async def _pump_client_to_tun(self, connection: VpnConnection, tun: LinuxTunDevice) -> None:
        try:
            while True:
                packet = await connection.recv_packet()
                destination_ip = _extract_destination_ipv4(packet.payload)
                peer_session_id = self._address_pool.session_for_destination(destination_ip)
                if peer_session_id is not None and peer_session_id != connection.session_id:
                    peer_connection = self._connections.get(peer_session_id)
                    if peer_connection is not None and not peer_connection.is_closed:
                        await _send_packet_with_retry(peer_connection, packet.payload)
                        continue
                await tun.write_packet(packet.payload)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, EOFError, RuntimeError):
            pass
        finally:
            await self._remove_connection(connection.session_id)

    async def _remove_connection(self, session_id: int) -> None:
        connection = self._connections.pop(session_id, None)
        self._address_pool.release(session_id)
        task = self._pump_tasks.pop(session_id, None)
        current = asyncio.current_task()
        if task is not None and task is not current and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if connection is not None and not connection.is_closed:
            with contextlib.suppress(Exception):
                await connection.close("server_session_removed")

    async def _shutdown_connections(self) -> None:
        session_ids = list(self._connections)
        for session_id in session_ids:
            await self._remove_connection(session_id)


async def _send_packet_with_retry(connection: VpnConnection, payload: bytes) -> None:
    while True:
        accepted = connection.send_packet(payload)
        if accepted:
            return
        if connection.is_closed:
            raise RuntimeError("VPN connection is closed")
        await asyncio.sleep(SEND_BACKPRESSURE_RETRY_DELAY)
