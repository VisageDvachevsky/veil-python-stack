from __future__ import annotations

import argparse
import asyncio
import ipaddress
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core import LinuxTunConfig, LinuxVpnProxyServer
from veil_core.linux_server_app import load_server_config


def _iptables(*args: str) -> None:
    subprocess.run(["iptables", *args], check=True)


def ensure_server_forwarding(config) -> None:
    tunnel_network = str(ipaddress.ip_interface(config.tun_address).network)
    subprocess.run(["sysctl", "-w", "net.ipv4.ip_forward=1"], check=True, capture_output=True)
    _iptables(
        "-t",
        "nat",
        "-C",
        "POSTROUTING",
        "-s",
        tunnel_network,
        "-o",
        config.public_interface,
        "-j",
        "MASQUERADE",
    )


def ensure_rule(command: list[str], add_command: list[str]) -> None:
    result = subprocess.run(command, check=False, capture_output=True)
    if result.returncode != 0:
        subprocess.run(add_command, check=True)


def configure_network(config) -> None:
    tunnel_network = str(ipaddress.ip_interface(config.tun_address).network)
    subprocess.run(["sysctl", "-w", "net.ipv4.ip_forward=1"], check=True, capture_output=True)
    ensure_rule(
        ["iptables", "-t", "nat", "-C", "POSTROUTING", "-s", tunnel_network, "-o", config.public_interface, "-j", "MASQUERADE"],
        ["iptables", "-t", "nat", "-A", "POSTROUTING", "-s", tunnel_network, "-o", config.public_interface, "-j", "MASQUERADE"],
    )
    ensure_rule(
        ["iptables", "-C", "FORWARD", "-i", config.tun_name, "-o", config.public_interface, "-j", "ACCEPT"],
        ["iptables", "-A", "FORWARD", "-i", config.tun_name, "-o", config.public_interface, "-j", "ACCEPT"],
    )
    ensure_rule(
        ["iptables", "-C", "FORWARD", "-i", config.public_interface, "-o", config.tun_name, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
        ["iptables", "-A", "FORWARD", "-i", config.public_interface, "-o", config.tun_name, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()

    config = load_server_config(args.config)
    configure_network(config)

    server = LinuxVpnProxyServer(
        port=config.bind_port,
        host=config.bind_host,
        tun_config=LinuxTunConfig(
            name=config.tun_name,
            address_cidr=config.tun_address,
            mtu=config.packet_mtu,
        ),
        local_name=config.server_name,
        packet_mtu=config.packet_mtu,
        keepalive_interval=config.keepalive_interval,
        keepalive_timeout=config.keepalive_timeout,
        psk=bytes.fromhex(config.psk_hex),
        clients=[
            {
                "client_id": client.client_id,
                "psk": bytes.fromhex(client.psk_hex),
                "enabled": client.enabled,
            }
            for client in (config.clients or [])
        ],
        fallback_psk=bytes.fromhex(config.fallback_psk_hex) if config.fallback_psk_hex else None,
        fallback_psk_policy=config.fallback_psk_policy,
        allow_legacy_unhinted=config.allow_legacy_unhinted,
        allow_hinted_route_miss_global_fallback=config.allow_hinted_route_miss_global_fallback,
        max_legacy_trial_decrypt_attempts=config.max_legacy_trial_decrypt_attempts,
        protocol_wrapper=config.protocol_wrapper,
        persona_preset=config.persona_preset,
        enable_http_handshake_emulation=config.enable_http_handshake_emulation,
        rotation_interval_seconds=config.rotation_interval_seconds,
        handshake_timeout_ms=config.handshake_timeout_ms,
        session_idle_timeout_ms=config.session_idle_timeout_ms,
        mtu=config.transport_mtu,
    )
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
