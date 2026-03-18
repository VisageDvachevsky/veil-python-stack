from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from veil_core.provisioning import ClientConnectionProfile, export_client_profile, generate_psk_hex


@dataclass(frozen=True)
class LinuxServerPaths:
    repo_root: Path
    config_dir: Path
    config_path: Path
    state_dir: Path
    client_profile_path: Path
    service_path: Path
    launcher_path: Path
    ctl_script_path: Path
    server_script_path: Path

    @classmethod
    def detect(cls, *, repo_root: Path | None = None) -> "LinuxServerPaths":
        root = repo_root or Path(__file__).resolve().parents[1]
        config_dir = Path("/etc/veil-vpn")
        state_dir = Path("/var/lib/veil-vpn")
        return cls(
            repo_root=root,
            config_dir=config_dir,
            config_path=config_dir / "server.json",
            state_dir=state_dir,
            client_profile_path=state_dir / "client-profile.json",
            service_path=Path("/etc/systemd/system/veil-vpn-server.service"),
            launcher_path=Path("/usr/local/bin/veil-vpn-server"),
            ctl_script_path=root / "desktop" / "veil_vpn_server_ctl.py",
            server_script_path=root / "desktop" / "veil_vpn_server.py",
        )


@dataclass(frozen=True)
class LinuxServerEnvironment:
    python3: str | None
    ip: str | None
    iptables: str | None
    systemctl: str | None
    tun_device_path: str = "/dev/net/tun"

    @classmethod
    def detect(cls) -> "LinuxServerEnvironment":
        return cls(
            python3=sys.executable,
            ip=shutil.which("ip"),
            iptables=shutil.which("iptables"),
            systemctl=shutil.which("systemctl"),
        )

    def doctor(self) -> dict[str, Any]:
        return {
            "python3": self.python3,
            "ip": self.ip,
            "iptables": self.iptables,
            "systemctl": self.systemctl,
            "tun_device": {
                "path": self.tun_device_path,
                "exists": Path(self.tun_device_path).exists(),
                "readable": os.access(self.tun_device_path, os.R_OK),
                "writable": os.access(self.tun_device_path, os.W_OK),
            },
            "is_root": os.geteuid() == 0,
        }


@dataclass
class LinuxServerConfig:
    bind_host: str = "0.0.0.0"
    bind_port: int = 4433
    public_host: str = ""
    public_interface: str = ""
    server_name: str = "veil-server"
    psk_hex: str = ""
    tun_name: str = "veil0"
    tun_address: str = "10.200.0.1/30"
    tun_peer: str = "10.200.0.2"
    packet_mtu: int = 1300
    keepalive_interval: float = 10.0
    keepalive_timeout: float = 30.0
    protocol_wrapper: str = "none"
    persona_preset: str = "custom"

    def ensure_defaults(self) -> "LinuxServerConfig":
        if not self.psk_hex:
            self.psk_hex = generate_psk_hex()
        if not self.public_interface:
            self.public_interface = autodetect_public_interface()
        return self

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def export_client_profile(self) -> ClientConnectionProfile:
        if not self.public_host:
            raise RuntimeError("public_host must be set before exporting a client profile")
        return export_client_profile(
            server_host=self.public_host,
            server_port=self.bind_port,
            psk_hex=self.psk_hex,
            tun_name="veilfull0",
            tun_address=self.tun_peer + "/30" if "/" not in self.tun_peer else self.tun_peer,
            tun_peer=self.tun_address.split("/", 1)[0],
            packet_mtu=self.packet_mtu,
            keepalive_interval=self.keepalive_interval,
            keepalive_timeout=self.keepalive_timeout,
            protocol_wrapper=self.protocol_wrapper,
            persona_preset=self.persona_preset,
        )


def load_server_config(path: Path) -> LinuxServerConfig:
    if not path.exists():
        return LinuxServerConfig().ensure_defaults()
    return LinuxServerConfig(**json.loads(path.read_text(encoding="utf-8"))).ensure_defaults()


def save_server_config(path: Path, config: LinuxServerConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.to_json(), encoding="utf-8")


def write_client_profile(path: Path, config: LinuxServerConfig) -> Path:
    profile = config.ensure_defaults().export_client_profile()
    profile.write(path)
    return path


def autodetect_public_interface() -> str:
    route = subprocess.run(
        ["ip", "route", "get", "1.1.1.1"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if "dev" not in route:
        raise RuntimeError("Could not detect public interface")
    return route[route.index("dev") + 1]


def render_server_launcher(paths: LinuxServerPaths, env: LinuxServerEnvironment) -> str:
    python_bin = env.python3 or sys.executable
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"export PYTHONPATH={shlex.quote(str(paths.repo_root))}\n"
        f"exec {shlex.quote(python_bin)} {shlex.quote(str(paths.server_script_path))} --config {shlex.quote(str(paths.config_path))}\n"
    )


def render_server_service(paths: LinuxServerPaths) -> str:
    return (
        "[Unit]\n"
        "Description=Veil VPN Server\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={paths.launcher_path}\n"
        "Restart=always\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def install_server_assets(
    paths: LinuxServerPaths,
    env: LinuxServerEnvironment,
    config: LinuxServerConfig,
) -> dict[str, str]:
    if os.geteuid() != 0:
        raise RuntimeError("Server installation must run as root")
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.launcher_path.parent.mkdir(parents=True, exist_ok=True)
    paths.service_path.parent.mkdir(parents=True, exist_ok=True)
    save_server_config(paths.config_path, config.ensure_defaults())
    write_client_profile(paths.client_profile_path, config)
    paths.launcher_path.write_text(render_server_launcher(paths, env), encoding="utf-8")
    paths.launcher_path.chmod(0o755)
    paths.service_path.write_text(render_server_service(paths), encoding="utf-8")
    return {
        "config_path": str(paths.config_path),
        "client_profile_path": str(paths.client_profile_path),
        "launcher_path": str(paths.launcher_path),
        "service_path": str(paths.service_path),
    }
