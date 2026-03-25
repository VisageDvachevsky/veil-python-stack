from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core.linux_client_app import (
    LinuxClientConfig,
    LinuxClientEnvironment,
    LinuxClientPaths,
    build_action_command,
    install_user_client,
    load_client_config,
    read_runtime_status,
    save_client_config,
    start_runtime,
    stop_runtime,
)
from veil_core.protocol_catalog import describe_protocol_selection, protocol_catalog_payload
from veil_core.provisioning import ClientConnectionProfile


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Veil Linux VPN controller")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--format", choices=("json", "text"), default="json")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("install")
    subparsers.add_parser("doctor")
    subparsers.add_parser("status")
    subparsers.add_parser("show-config")
    subparsers.add_parser("list-protocol-options")
    subparsers.add_parser("up")
    subparsers.add_parser("down")
    subparsers.add_parser("internal-up")
    subparsers.add_parser("internal-down")
    import_cmd = subparsers.add_parser("import-profile")
    import_cmd.add_argument("--profile", type=Path)
    import_cmd.add_argument("--profile-url")
    import_cmd.add_argument("--profile-token")

    save = subparsers.add_parser("save-config")
    save.add_argument("--server-host")
    save.add_argument("--server-port", type=int)
    save.add_argument("--client-name")
    save.add_argument("--psk-hex")
    save.add_argument("--tunnel-mode")
    save.add_argument("--tun-name")
    save.add_argument("--tun-address")
    save.add_argument("--tun-peer")
    save.add_argument("--packet-mtu", type=int)
    save.add_argument("--keepalive-interval", type=float)
    save.add_argument("--keepalive-timeout", type=float)
    save.add_argument("--reconnect")
    save.add_argument("--auto-connect")
    save.add_argument("--protocol-wrapper")
    save.add_argument("--persona-preset")
    save.add_argument("--enable-http-handshake-emulation")
    save.add_argument("--rotation-interval-seconds", type=int)
    save.add_argument("--handshake-timeout-ms", type=int)
    save.add_argument("--session-idle-timeout-ms", type=int)
    save.add_argument("--transport-mtu", type=int)
    save.add_argument("--suspend-conflicting-services")

    return parser.parse_args()


def _parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"Unsupported boolean value: {value!r}")


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


def _config_payload(config: LinuxClientConfig, paths: LinuxClientPaths) -> dict[str, object]:
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


def _status_text(payload: dict[str, object]) -> str:
    lines = [
        f"state: {'connected' if payload.get('running') and payload.get('tun_exists') else 'idle'}",
        f"endpoint: {payload.get('server_host')}:{payload.get('server_port')}",
        f"tunnel: {payload.get('tun_name')} ({payload.get('tunnel_mode')})",
        f"protocol: {payload.get('protocol_wrapper')} / {payload.get('persona_preset')}",
        f"http_upgrade: {payload.get('enable_http_handshake_emulation')}",
        f"packet_mtu: {payload.get('packet_mtu')}",
        f"keepalive: {payload.get('keepalive_interval')}s / timeout {payload.get('keepalive_timeout')}s",
        f"transport_mtu: {payload.get('transport_mtu')}",
        f"rotation_interval_seconds: {payload.get('rotation_interval_seconds')}",
        f"handshake_timeout_ms: {payload.get('handshake_timeout_ms')}",
        f"session_idle_timeout_ms: {payload.get('session_idle_timeout_ms')}",
        f"reconnect: {payload.get('reconnect')}",
        f"auto_connect: {payload.get('auto_connect')}",
        f"suspend_conflicting_services: {payload.get('suspend_conflicting_services')}",
        f"log_path: {payload.get('log_path')}",
    ]
    underlay_route = payload.get("underlay_route")
    if isinstance(underlay_route, dict) and underlay_route:
        lines.append(f"underlay_route: {underlay_route}")
    suspended_conflicts = payload.get("suspended_conflicts")
    if isinstance(suspended_conflicts, list) and suspended_conflicts:
        lines.append(f"suspended_conflicts: {suspended_conflicts}")
    return "\n".join(lines)


def _doctor_text(payload: dict[str, object]) -> str:
    return _format_mapping(payload, heading="Veil Linux client doctor")


def _catalog_text() -> str:
    payload = protocol_catalog_payload()
    lines = ["Wrappers:"]
    for wrapper in payload["wrappers"]:
        lines.append(f"  {wrapper['value']}: {wrapper['label']}")
        lines.append(f"    {wrapper['summary']}")
        lines.append(f"    best_for={wrapper['best_for']}")
        lines.append(f"    supports_http_upgrade={wrapper['supports_http_upgrade']}")
    lines.append("Personas:")
    for persona in payload["personas"]:
        lines.append(f"  {persona['value']}: {persona['label']}")
        lines.append(f"    {persona['summary']}")
        lines.append(f"    best_with={persona['best_with']}")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    paths = LinuxClientPaths.detect(repo_root=ROOT)
    env = LinuxClientEnvironment.detect()
    config_path = args.config or paths.config_path

    if args.command == "install":
        result = install_user_client(paths, env)
        print(json.dumps({"installed": True, **result}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "doctor":
        payload = env.doctor()
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(_doctor_text(payload))
        return 0

    config = load_client_config(config_path)

    if args.command == "show-config":
        _print_payload(_config_payload(config, paths), args.format, heading="Veil Linux client config")
        return 0

    if args.command == "list-protocol-options":
        if args.format == "json":
            print(json.dumps(protocol_catalog_payload(), ensure_ascii=False, indent=2))
        else:
            print(_catalog_text())
        return 0

    if args.command == "save-config":
        updated = LinuxClientConfig(**{
            **config.__dict__,
            **{
                key: value
                for key, value in {
                    "server_host": args.server_host,
                    "server_port": args.server_port,
                    "client_name": args.client_name,
                    "psk_hex": args.psk_hex,
                    "tunnel_mode": args.tunnel_mode,
                    "tun_name": args.tun_name,
                    "tun_address": args.tun_address,
                    "tun_peer": args.tun_peer,
                    "packet_mtu": args.packet_mtu,
                    "keepalive_interval": args.keepalive_interval,
                    "keepalive_timeout": args.keepalive_timeout,
                    "reconnect": _parse_bool(args.reconnect),
                    "auto_connect": _parse_bool(args.auto_connect),
                    "protocol_wrapper": args.protocol_wrapper,
                    "persona_preset": args.persona_preset,
                    "enable_http_handshake_emulation": _parse_bool(args.enable_http_handshake_emulation),
                    "rotation_interval_seconds": args.rotation_interval_seconds,
                    "handshake_timeout_ms": args.handshake_timeout_ms,
                    "session_idle_timeout_ms": args.session_idle_timeout_ms,
                    "transport_mtu": args.transport_mtu,
                    "suspend_conflicting_services": _parse_bool(args.suspend_conflicting_services),
                }.items()
                if value is not None
            },
        }).ensure_compatible()
        save_client_config(config_path, updated)
        print(updated.to_json(), end="")
        return 0

    if args.command == "status":
        payload = read_runtime_status(paths, config)
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(_status_text(payload))
        return 0

    if args.command == "import-profile":
        sources = [args.profile is not None, args.profile_url is not None, args.profile_token is not None]
        if sum(bool(item) for item in sources) != 1:
            raise RuntimeError("Specify exactly one of --profile, --profile-url or --profile-token")
        if args.profile is not None:
            profile = ClientConnectionProfile.from_path(args.profile)
        elif args.profile_url is not None:
            with urllib.request.urlopen(args.profile_url) as response:
                profile = ClientConnectionProfile.from_json_text(response.read().decode("utf-8"))
        else:
            profile = ClientConnectionProfile.from_import_token(args.profile_token)
        updated = LinuxClientConfig(**{
            **config.__dict__,
            **profile.__dict__,
        })
        save_client_config(config_path, updated)
        print(updated.to_json(), end="")
        return 0

    if args.command in {"up", "down"}:
        command = build_action_command(
            args.command,
            config_path=config_path,
            config=config,
            paths=paths,
            env=env,
        )
        completed = subprocess.run(command, cwd=paths.repo_root, check=False)
        return int(completed.returncode)

    if args.command == "internal-up":
        print(json.dumps(start_runtime(paths, config), ensure_ascii=False, indent=2))
        return 0

    if args.command == "internal-down":
        print(json.dumps(stop_runtime(paths, config), ensure_ascii=False, indent=2))
        return 0

    raise RuntimeError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
