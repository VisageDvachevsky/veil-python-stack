from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core.linux_server_app import (
    LinuxServerConfig,
    LinuxServerEnvironment,
    LinuxServerPaths,
    install_server_assets,
    load_server_config,
    read_server_status,
    save_server_config,
    write_client_profile,
)
from veil_core.protocol_catalog import describe_protocol_selection, protocol_catalog_payload
from veil_core.provisioning import generate_psk_hex, profile_summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Veil Linux VPN server controller")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor")
    subparsers.add_parser("install")
    subparsers.add_parser("gen-psk")
    subparsers.add_parser("status")
    subparsers.add_parser("show-config")
    subparsers.add_parser("list-protocol-options")
    subparsers.add_parser("export-client-profile")
    subparsers.add_parser("export-client-token")
    subparsers.add_parser("enable-service")
    subparsers.add_parser("disable-service")
    write_profile_cmd = subparsers.add_parser("write-client-profile")
    write_profile_cmd.add_argument("--output", type=Path, required=True)

    init_cmd = subparsers.add_parser("init")
    init_cmd.add_argument("--public-host")
    init_cmd.add_argument("--bind-host")
    init_cmd.add_argument("--bind-port", type=int)
    init_cmd.add_argument("--server-name")
    init_cmd.add_argument("--public-interface")
    init_cmd.add_argument("--psk-hex")
    init_cmd.add_argument("--tun-name")
    init_cmd.add_argument("--tun-address")
    init_cmd.add_argument("--tun-peer")
    init_cmd.add_argument("--packet-mtu", type=int)
    init_cmd.add_argument("--keepalive-interval", type=float)
    init_cmd.add_argument("--keepalive-timeout", type=float)
    init_cmd.add_argument("--protocol-wrapper")
    init_cmd.add_argument("--persona-preset")
    init_cmd.add_argument("--enable-http-handshake-emulation")
    init_cmd.add_argument("--rotation-interval-seconds", type=int)
    init_cmd.add_argument("--handshake-timeout-ms", type=int)
    init_cmd.add_argument("--session-idle-timeout-ms", type=int)
    init_cmd.add_argument("--transport-mtu", type=int)

    return parser.parse_args()


def _format_mapping(payload: dict[str, object], *, heading: str | None = None) -> str:
    lines: list[str] = [heading] if heading else []

    def render_mapping(mapping: dict[str, object], indent: str = "") -> None:
        for key, value in mapping.items():
            if isinstance(value, dict):
                lines.append(f"{indent}{key}:")
                render_mapping(value, indent + "  ")
            elif isinstance(value, list):
                if not value:
                    lines.append(f"{indent}{key}: []")
                    continue
                lines.append(f"{indent}{key}:")
                for item in value:
                    if isinstance(item, dict):
                        lines.append(f"{indent}  -")
                        render_mapping(item, indent + "    ")
                    else:
                        lines.append(f"{indent}  - {item}")
            else:
                lines.append(f"{indent}{key}: {value}")

    render_mapping(payload)
    return "\n".join(lines)


def _print_payload(payload: dict[str, object], fmt: str, *, heading: str | None = None) -> None:
    if fmt == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_format_mapping(payload, heading=heading))


def _catalog_text() -> str:
    payload = protocol_catalog_payload()
    lines = ["Wrappers:"]
    for wrapper in payload["wrappers"]:
        lines.append(f"  {wrapper['value']}: {wrapper['label']}")
        lines.append(f"    {wrapper['summary']}")
    lines.append("Personas:")
    for persona in payload["personas"]:
        lines.append(f"  {persona['value']}: {persona['label']}")
        lines.append(f"    {persona['summary']}")
    return "\n".join(lines)


def _config_payload(config: LinuxServerConfig, paths: LinuxServerPaths) -> dict[str, object]:
    protocol = describe_protocol_selection(
        config.protocol_wrapper,
        config.persona_preset,
        config.enable_http_handshake_emulation,
    )
    return {
        **config.__dict__,
        "config_path": str(paths.config_path),
        "protocol_details": protocol,
    }


def main() -> int:
    args = parse_args()
    paths = LinuxServerPaths.detect(repo_root=ROOT)
    env = LinuxServerEnvironment.detect()
    config_path = args.config or paths.config_path

    if args.command == "doctor":
        _print_payload(env.doctor(), args.format, heading="Veil Linux server doctor")
        return 0

    if args.command == "gen-psk":
        print(generate_psk_hex())
        return 0

    config = load_server_config(config_path)

    if args.command == "status":
        _print_payload(read_server_status(paths, config), args.format, heading="Veil Linux server status")
        return 0

    if args.command == "show-config":
        _print_payload(_config_payload(config, paths), args.format, heading="Veil Linux server config")
        return 0

    if args.command == "list-protocol-options":
        if args.format == "json":
            print(json.dumps(protocol_catalog_payload(), ensure_ascii=False, indent=2))
        else:
            print(_catalog_text())
        return 0

    if args.command == "init":
        updated = LinuxServerConfig(**{
            **config.__dict__,
            **{
                key: value
                for key, value in {
                    "public_host": args.public_host,
                    "bind_host": args.bind_host,
                    "bind_port": args.bind_port,
                    "server_name": args.server_name,
                    "public_interface": args.public_interface,
                    "psk_hex": args.psk_hex,
                    "tun_name": args.tun_name,
                    "tun_address": args.tun_address,
                    "tun_peer": args.tun_peer,
                    "packet_mtu": args.packet_mtu,
                    "keepalive_interval": args.keepalive_interval,
                    "keepalive_timeout": args.keepalive_timeout,
                    "protocol_wrapper": args.protocol_wrapper,
                    "persona_preset": args.persona_preset,
                    "enable_http_handshake_emulation": (
                        args.enable_http_handshake_emulation.lower() in {"1", "true", "yes", "on"}
                        if args.enable_http_handshake_emulation is not None
                        else None
                    ),
                    "rotation_interval_seconds": args.rotation_interval_seconds,
                    "handshake_timeout_ms": args.handshake_timeout_ms,
                    "session_idle_timeout_ms": args.session_idle_timeout_ms,
                    "transport_mtu": args.transport_mtu,
                }.items()
                if value is not None
            },
        }).ensure_defaults()
        save_server_config(config_path, updated)
        print(updated.to_json(), end="")
        return 0

    if args.command == "install":
        config.ensure_defaults()
        if not config.public_host:
            raise RuntimeError("public_host must be configured before install")
        payload = install_server_assets(paths, env, config)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.command == "export-client-profile":
        if not config.public_host:
            raise RuntimeError("public_host must be configured before exporting a client profile")
        profile = config.export_client_profile()
        print(profile.to_json(), end="")
        return 0

    if args.command == "export-client-token":
        if not config.public_host:
            raise RuntimeError("public_host must be configured before exporting a client profile")
        profile = config.export_client_profile()
        print(profile.to_import_token())
        return 0

    if args.command == "write-client-profile":
        if not config.public_host:
            raise RuntimeError("public_host must be configured before exporting a client profile")
        output = write_client_profile(args.output, config)
        payload = {"output": str(output), "summary": profile_summary(config.export_client_profile())}
        _print_payload(payload, args.format, heading="Veil Linux exported client profile")
        return 0

    if args.command == "enable-service":
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "--now", paths.service_path.name], check=True)
        return 0

    if args.command == "disable-service":
        subprocess.run(["systemctl", "disable", "--now", paths.service_path.name], check=False)
        return 0

    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
