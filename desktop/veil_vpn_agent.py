from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core.vpn import VpnClient
from veil_core.windows_client_app import (
    WindowsClientConfig,
    WindowsClientEnvironment,
    WindowsClientPaths,
    cleanup_wintun_network,
    configure_wintun_network,
    create_wintun_session,
    load_client_config,
    mark_agent_stopped,
    read_agent_pid,
    read_runtime_state,
    write_agent_pid,
    write_runtime_state,
)


class WindowsVpnAgent:
    def __init__(self, paths: WindowsClientPaths, config_path: Path) -> None:
        self._paths = paths
        self._config_path = config_path
        self._env = WindowsClientEnvironment.detect()
        self._client: VpnClient | None = None
        self._connection = None
        self._active_config: WindowsClientConfig | None = None
        self._should_be_connected = False
        self._last_command_ts = ""
        self._wintun = None
        self._network_state: dict[str, Any] | None = None
        self._vpn_to_os_task: asyncio.Task[None] | None = None
        self._os_to_vpn_task: asyncio.Task[None] | None = None

    def _log(self, message: str, *, level: int = logging.INFO) -> None:
        logging.log(level, "%s", message)

    async def run(self) -> None:
        self._update_state(running=True, connected=False, last_event="agent_started", last_error="")
        try:
            while True:
                try:
                    self._consume_command_if_present()
                    if self._should_be_connected and self._connection is None:
                        await self._connect_once()
                    await self._monitor_connection()
                except Exception as exc:
                    self._log(f"agent_loop_error: {exc}", level=logging.ERROR)
                    await self._disconnect(f"agent_loop_error:{exc}")
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            raise
        finally:
            await self._disconnect("agent_shutdown")
            mark_agent_stopped(self._paths, reason="")

    def _consume_command_if_present(self) -> None:
        if not self._paths.agent_command_path.exists():
            return
        payload = json.loads(self._paths.agent_command_path.read_text(encoding="utf-8"))
        issued_at = str(payload.get("issued_at") or "")
        if issued_at == self._last_command_ts:
            return
        self._last_command_ts = issued_at
        self._active_config = WindowsClientConfig(**payload.get("config", {}))
        command = payload.get("command")
        if command == "up":
            self._should_be_connected = True
            self._log("command_up received")
            self._update_state(last_event="command_up")
        elif command == "down":
            self._should_be_connected = False
            self._log("command_down received")
            self._update_state(last_event="command_down")
        else:
            self._log(f"unknown_command received: {command}", level=logging.WARNING)
            self._update_state(last_event=f"unknown_command:{command}", last_error="")

    async def _connect_once(self) -> None:
        config = self._active_config or load_client_config(self._config_path)
        self._log(
            f"connect_attempt host={config.server_host}:{config.server_port} adapter={config.adapter_name} full_tunnel={config.full_tunnel} is_admin={self._env.is_admin}"
        )
        self._client = VpnClient(
            host=config.server_host,
            port=config.server_port,
            psk=config.psk,
            local_name=config.client_name,
            packet_mtu=config.packet_mtu,
            keepalive_interval=config.keepalive_interval,
            keepalive_timeout=config.keepalive_timeout,
            protocol_wrapper=config.protocol_wrapper,
            persona_preset=config.persona_preset,
        )
        self._update_state(connected=False, last_event="connecting", last_error="")
        try:
            self._client.transport.start()
            self._connection = await self._client.connect(handshake_timeout=10.0)
            self._log(
                f"transport_connected session=0x{self._connection.session_id:x} peer={self._connection.peer_name or '-'} role={self._connection.peer_role or '-'} mtu={self._connection.effective_packet_mtu}"
            )
        except Exception as exc:
            self._connection = None
            if self._client is not None:
                self._client.transport.stop()
                self._client = None
            self._log(f"connect_failed: {exc}", level=logging.ERROR)
            self._should_be_connected = False
            self._update_state(connected=False, last_event="connect_failed", last_error=str(exc))
            return

        try:
            self._wintun = create_wintun_session(self._paths, config)
            self._network_state = configure_wintun_network(self._env, config)
            self._log(
                f"wintun_ready adapter={config.adapter_name} network_state={json.dumps(self._network_state, ensure_ascii=False)}"
            )
        except Exception as exc:
            self._should_be_connected = False
            self._log(f"wintun_setup_failed: {exc}", level=logging.ERROR)
            await self._disconnect(f"wintun_setup_failed:{exc}")
            return

        self._vpn_to_os_task = asyncio.create_task(self._pump_vpn_to_os(), name="veil-vpn-to-os")
        self._os_to_vpn_task = asyncio.create_task(self._pump_os_to_vpn(), name="veil-os-to-vpn")

        self._update_state(
            connected=True,
            last_event="connected",
            last_error="",
            peer_name=self._connection.peer_name,
            peer_role=self._connection.peer_role,
            negotiated_packet_mtu=self._connection.effective_packet_mtu,
            tunnel_backend="wintun",
            session_id=f"0x{self._connection.session_id:x}",
            tun_address=config.tun_address,
            tun_peer=config.tun_peer,
            network_state=self._network_state,
        )

    async def _monitor_connection(self) -> None:
        if self._connection is None:
            if not self._should_be_connected:
                self._update_state(connected=False, last_event="idle")
            return

        if not self._should_be_connected:
            await self._disconnect("command_down")
            return

        if self._connection.is_closed:
            reason = self._connection.close_reason or "transport_closed"
            self._log(f"connection_closed: {reason}", level=logging.WARNING)
            await self._disconnect(reason)
            if self._should_be_connected:
                self._update_state(connected=False, last_event="reconnect_pending", last_error=reason)
                await asyncio.sleep(2.0)
            return

    async def _disconnect(self, reason: str) -> None:
        self._log(f"disconnect reason={reason}")
        for task in (self._vpn_to_os_task, self._os_to_vpn_task):
            if task is not None and not task.done():
                task.cancel()
        if self._vpn_to_os_task is not None or self._os_to_vpn_task is not None:
            await asyncio.gather(
                *(task for task in (self._vpn_to_os_task, self._os_to_vpn_task) if task is not None),
                return_exceptions=True,
            )
        self._vpn_to_os_task = None
        self._os_to_vpn_task = None
        if self._connection is not None:
            try:
                await self._connection.close(reason)
            except Exception:
                pass
        if self._client is not None:
            self._client.transport.stop()
        if self._wintun is not None:
            try:
                self._wintun.close()
            except Exception:
                pass
        if self._active_config is not None:
            cleanup_wintun_network(self._env, self._active_config, self._network_state)
        self._connection = None
        self._client = None
        self._wintun = None
        self._network_state = None
        self._update_state(connected=False, last_event="disconnected", last_error=reason)

    async def _pump_vpn_to_os(self) -> None:
        assert self._connection is not None
        assert self._wintun is not None
        try:
            while not self._connection.is_closed:
                try:
                    packet = await self._connection.recv_packet(timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                self._wintun.send_packet(packet.payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log(f"vpn_to_os_failed: {exc}", level=logging.ERROR)
            self._update_state(last_event="vpn_to_os_failed", last_error=str(exc))
            self._should_be_connected = False

    async def _pump_os_to_vpn(self) -> None:
        assert self._connection is not None
        assert self._wintun is not None
        try:
            while not self._connection.is_closed:
                packet = await asyncio.to_thread(self._wintun.recv_packet, 1000)
                if not packet:
                    continue
                self._connection.send_packet(packet)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log(f"os_to_vpn_failed: {exc}", level=logging.ERROR)
            self._update_state(last_event="os_to_vpn_failed", last_error=str(exc))
            self._should_be_connected = False

    def _update_state(self, **overrides: Any) -> None:
        state = read_runtime_state(self._paths)
        state.update(
            {
                "installed": True,
                "running": True,
                "connected": state.get("connected", False),
                "pid": os_getpid(),
                "mode": "windows-agent",
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        state.update(overrides)
        write_runtime_state(self._paths, state)


def os_getpid() -> int:
    import os

    return os.getpid()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Veil Windows VPN agent")
    parser.add_argument("command", choices=["run-daemon"])
    parser.add_argument("--config", type=Path)
    return parser.parse_args()


async def _run_daemon(config_path: Path) -> None:
    paths = WindowsClientPaths.detect(repo_root=ROOT)
    existing_pid = read_agent_pid(paths)
    if existing_pid and existing_pid != os_getpid() and _pid_alive(existing_pid):
        logging.info("agent instance already active pid=%s, exiting duplicate pid=%s", existing_pid, os_getpid())
        return
    write_agent_pid(paths, os_getpid())
    agent = WindowsVpnAgent(paths, config_path)
    try:
        await agent.run()
    finally:
        current_pid = read_agent_pid(paths)
        if current_pid == os_getpid():
            write_agent_pid(paths, None)


def _pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "ctl":
        from veil_vpn_windows_ctl import main as ctl_main

        sys.argv = [sys.argv[0], *sys.argv[2:]]
        return ctl_main()

    args = parse_args()
    paths = WindowsClientPaths.detect(repo_root=ROOT)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=paths.agent_log_path,
        level=logging.INFO,
        format="[%(asctime)s] [python] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    config_path = args.config or paths.config_path
    if args.command == "run-daemon":
        asyncio.run(_run_daemon(config_path))
        return 0
    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
