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
from veil_core.provisioning import ClientConnectionProfile


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Veil Linux VPN controller")
    parser.add_argument("--config", type=Path)

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("install")
    subparsers.add_parser("doctor")
    subparsers.add_parser("status")
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
    save.add_argument("--tun-name")
    save.add_argument("--tun-address")
    save.add_argument("--tun-peer")
    save.add_argument("--packet-mtu", type=int)
    save.add_argument("--keepalive-interval", type=float)
    save.add_argument("--keepalive-timeout", type=float)
    save.add_argument("--protocol-wrapper")
    save.add_argument("--persona-preset")

    return parser.parse_args()


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
        print(json.dumps(env.doctor(), ensure_ascii=False, indent=2))
        return 0

    config = load_client_config(config_path)

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
                    "tun_name": args.tun_name,
                    "tun_address": args.tun_address,
                    "tun_peer": args.tun_peer,
                    "packet_mtu": args.packet_mtu,
                    "keepalive_interval": args.keepalive_interval,
                    "keepalive_timeout": args.keepalive_timeout,
                    "protocol_wrapper": args.protocol_wrapper,
                    "persona_preset": args.persona_preset,
                }.items()
                if value is not None
            },
        })
        save_client_config(config_path, updated)
        print(updated.to_json(), end="")
        return 0

    if args.command == "status":
        print(json.dumps(read_runtime_status(paths, config), ensure_ascii=False, indent=2))
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
