from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core.provisioning import ClientConnectionProfile
from veil_core.windows_client_app import (
    WindowsClientConfig,
    WindowsClientEnvironment,
    WindowsClientPaths,
    install_windows_client,
    load_client_config,
    read_runtime_status,
    save_client_config,
    start_runtime,
    stop_runtime,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Veil Windows VPN controller")
    parser.add_argument("--config", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("install")
    subparsers.add_parser("doctor")
    subparsers.add_parser("status")
    subparsers.add_parser("up")
    subparsers.add_parser("down")

    import_cmd = subparsers.add_parser("import-profile")
    import_cmd.add_argument("--profile", type=Path)
    import_cmd.add_argument("--profile-token")

    save_cmd = subparsers.add_parser("save-config")
    save_cmd.add_argument("--server-host")
    save_cmd.add_argument("--server-port", type=int)
    save_cmd.add_argument("--client-name")
    save_cmd.add_argument("--psk-hex")
    save_cmd.add_argument("--adapter-name")
    save_cmd.add_argument("--tun-address")
    save_cmd.add_argument("--tun-peer")
    save_cmd.add_argument("--packet-mtu", type=int)
    save_cmd.add_argument("--keepalive-interval", type=float)
    save_cmd.add_argument("--keepalive-timeout", type=float)
    save_cmd.add_argument("--full-tunnel")
    save_cmd.add_argument("--protocol-wrapper")
    save_cmd.add_argument("--persona-preset")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = WindowsClientPaths.detect(repo_root=ROOT)
    env = WindowsClientEnvironment.detect()
    config_path = args.config or paths.config_path

    if args.command == "install":
        print(json.dumps(install_windows_client(paths, env), ensure_ascii=False, indent=2))
        return 0

    if args.command == "doctor":
        print(json.dumps(env.doctor(paths), ensure_ascii=False, indent=2))
        return 0

    config = load_client_config(config_path)

    if args.command == "save-config":
        updated = WindowsClientConfig(
            **{
                **config.__dict__,
                **{
                    key: value
                    for key, value in {
                        "server_host": args.server_host,
                        "server_port": args.server_port,
                        "client_name": args.client_name,
                        "psk_hex": args.psk_hex,
                        "adapter_name": args.adapter_name,
                        "tun_address": args.tun_address,
                        "tun_peer": args.tun_peer,
                        "packet_mtu": args.packet_mtu,
                        "keepalive_interval": args.keepalive_interval,
                        "keepalive_timeout": args.keepalive_timeout,
                        "full_tunnel": _parse_optional_bool(args.full_tunnel),
                        "protocol_wrapper": args.protocol_wrapper,
                        "persona_preset": args.persona_preset,
                    }.items()
                    if value is not None
                },
            }
        )
        save_client_config(config_path, updated)
        print(updated.to_json(), end="")
        return 0

    if args.command == "status":
        print(json.dumps(read_runtime_status(paths, config), ensure_ascii=False, indent=2))
        return 0

    if args.command == "import-profile":
        sources = [args.profile is not None, args.profile_token is not None]
        if sum(bool(item) for item in sources) != 1:
            raise RuntimeError("Specify exactly one of --profile or --profile-token")
        if args.profile is not None:
            profile = ClientConnectionProfile.from_path(args.profile)
        else:
            profile = ClientConnectionProfile.from_import_token(args.profile_token)
        updated = WindowsClientConfig(
            **{
                **config.__dict__,
                "server_host": profile.server_host,
                "server_port": profile.server_port,
                "client_name": profile.client_name,
                "psk_hex": profile.psk_hex,
                "tun_address": profile.tun_address,
                "tun_peer": profile.tun_peer,
                "packet_mtu": profile.packet_mtu,
                "keepalive_interval": profile.keepalive_interval,
                "keepalive_timeout": profile.keepalive_timeout,
                "protocol_wrapper": profile.protocol_wrapper,
                "persona_preset": profile.persona_preset,
            }
        )
        save_client_config(config_path, updated)
        print(updated.to_json(), end="")
        return 0

    if args.command == "up":
        print(json.dumps(start_runtime(paths, env, config, config_path=config_path), ensure_ascii=False, indent=2))
        return 0

    if args.command == "down":
        print(json.dumps(stop_runtime(paths, config), ensure_ascii=False, indent=2))
        return 0

    raise RuntimeError(f"Unsupported command: {args.command}")


def _parse_optional_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"Unsupported boolean value: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
