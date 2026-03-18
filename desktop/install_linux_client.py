from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from veil_core.linux_client_app import LinuxClientEnvironment, LinuxClientPaths, install_user_client
from veil_core.provisioning import ClientConnectionProfile


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install Veil Linux client")
    parser.add_argument("profile_path", nargs="?")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--profile-url")
    parser.add_argument("--profile-token")
    return parser.parse_args()


def load_import_profile(args: argparse.Namespace) -> ClientConnectionProfile | None:
    profile_path = Path(args.profile_path) if args.profile_path else args.profile
    sources = [profile_path is not None, args.profile_url is not None, args.profile_token is not None]
    if sum(bool(item) for item in sources) > 1:
        raise RuntimeError("Specify only one profile source")
    if profile_path is not None:
        return ClientConnectionProfile.from_path(profile_path)
    if args.profile_url is not None:
        with urllib.request.urlopen(args.profile_url) as response:
            return ClientConnectionProfile.from_json_text(response.read().decode("utf-8"))
    if args.profile_token is not None:
        return ClientConnectionProfile.from_import_token(args.profile_token)
    return None


def main() -> None:
    args = parse_args()
    paths = LinuxClientPaths.detect(repo_root=ROOT)
    env = LinuxClientEnvironment.detect()
    imported_profile = load_import_profile(args)
    payload = {
        "doctor": env.doctor(),
        "install": install_user_client(paths, env),
        "imported_profile": imported_profile.__dict__ if imported_profile is not None else None,
        "next_steps": [
            str(paths.gui_wrapper_path),
            f"{paths.ctl_wrapper_path} doctor",
            f"{paths.ctl_wrapper_path} status",
        ],
    }
    if imported_profile is not None:
        from veil_core.linux_client_app import LinuxClientConfig, save_client_config

        save_client_config(paths.config_path, LinuxClientConfig(**imported_profile.__dict__))
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
