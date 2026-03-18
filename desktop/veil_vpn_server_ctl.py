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
    save_server_config,
    write_client_profile,
)
from veil_core.provisioning import generate_psk_hex, profile_summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Veil Linux VPN server controller")
    parser.add_argument("--config", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor")
    subparsers.add_parser("install")
    subparsers.add_parser("gen-psk")
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
    init_cmd.add_argument("--protocol-wrapper")
    init_cmd.add_argument("--persona-preset")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = LinuxServerPaths.detect(repo_root=ROOT)
    env = LinuxServerEnvironment.detect()
    config_path = args.config or paths.config_path

    if args.command == "doctor":
        print(json.dumps(env.doctor(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "gen-psk":
        print(generate_psk_hex())
        return 0

    config = load_server_config(config_path)

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
                    "protocol_wrapper": args.protocol_wrapper,
                    "persona_preset": args.persona_preset,
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
        print(json.dumps({
            "output": str(output),
            "summary": profile_summary(config.export_client_profile()),
        }, ensure_ascii=False, indent=2))
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
