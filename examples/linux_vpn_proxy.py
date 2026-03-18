from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core import LinuxTunConfig, LinuxVpnProxyClient, LinuxVpnProxyServer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Linux-only Veil TUN proxy")
    parser.add_argument("--mode", choices=("server", "client"), required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4433)
    parser.add_argument("--tun-name", default="veil0")
    parser.add_argument("--tun-address", required=True, help="CIDR for local TUN address, e.g. 10.200.0.1/30")
    parser.add_argument("--tun-peer", default=None, help="Optional peer address for point-to-point setup")
    parser.add_argument(
        "--route",
        action="append",
        default=[],
        help="Route CIDR to install via the TUN interface. May be repeated.",
    )
    parser.add_argument("--packet-mtu", type=int, default=1300)
    parser.add_argument("--keepalive-interval", type=float, default=10.0)
    parser.add_argument("--keepalive-timeout", type=float, default=30.0)
    parser.add_argument("--reconnect", action="store_true", help="Client mode: reconnect forever on failure")
    parser.add_argument("--reconnect-delay", type=float, default=2.0)
    parser.add_argument("--max-reconnect-delay", type=float, default=15.0)
    parser.add_argument("--name", default=None)
    parser.add_argument("--psk-hex", default="ab" * 32)
    parser.add_argument("--protocol-wrapper", default="none")
    parser.add_argument("--persona-preset", default="custom")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    psk = bytes.fromhex(args.psk_hex)
    tun_config = LinuxTunConfig(
        name=args.tun_name,
        address_cidr=args.tun_address,
        peer_address=args.tun_peer,
        mtu=args.packet_mtu,
        routes=tuple(args.route),
    )

    if args.mode == "server":
        proxy = LinuxVpnProxyServer(
            port=args.port,
            host=args.host,
            tun_config=tun_config,
            local_name=args.name or "server",
            packet_mtu=args.packet_mtu,
            keepalive_interval=args.keepalive_interval,
            keepalive_timeout=args.keepalive_timeout,
            psk=psk,
            protocol_wrapper=args.protocol_wrapper,
            persona_preset=args.persona_preset,
        )
        await proxy.serve_forever()
        return

    proxy = LinuxVpnProxyClient(
        host=args.host,
        port=args.port,
        tun_config=tun_config,
        local_name=args.name or "client",
        packet_mtu=args.packet_mtu,
        reconnect_delay=args.reconnect_delay,
        max_reconnect_delay=args.max_reconnect_delay,
        keepalive_interval=args.keepalive_interval,
        keepalive_timeout=args.keepalive_timeout,
        psk=psk,
        protocol_wrapper=args.protocol_wrapper,
        persona_preset=args.persona_preset,
    )
    if args.reconnect:
        await proxy.run_forever()
        return
    await proxy.run()


if __name__ == "__main__":
    asyncio.run(main())
