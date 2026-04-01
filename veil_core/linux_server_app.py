from __future__ import annotations

import ipaddress
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from veil_core.provisioning import (
    ClientConnectionProfile,
    export_client_profile,
    generate_psk_hex,
    profile_summary,
)


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
class LinuxServerClientConfig:
    client_id: str = ""
    client_name: str = ""
    psk_hex: str = ""
    enabled: bool = True

    def ensure_defaults(self) -> "LinuxServerClientConfig":
        if not self.client_id:
            self.client_id = self.client_name or "veil-client"
        if not self.client_name:
            self.client_name = self.client_id
        if not self.psk_hex:
            self.psk_hex = generate_psk_hex()
        return self

    @property
    def psk(self) -> bytes:
        return bytes.fromhex(self.psk_hex)


@dataclass
class LinuxServerConfig:
    bind_host: str = "0.0.0.0"
    bind_port: int = 4433
    public_host: str = ""
    public_interface: str = ""
    server_name: str = "veil-server"
    psk_hex: str = ""
    clients: list[LinuxServerClientConfig] | None = None
    fallback_psk_hex: str = ""
    fallback_psk_policy: str = "deny_always"
    allow_legacy_unhinted: bool = False
    allow_hinted_route_miss_global_fallback: bool = False
    max_legacy_trial_decrypt_attempts: int = 8
    tun_name: str = "veil0"
    tun_address: str = "10.200.0.1/24"
    tun_peer: str = ""
    packet_mtu: int = 1300
    keepalive_interval: float = 10.0
    keepalive_timeout: float = 30.0
    protocol_wrapper: str = "none"
    persona_preset: str = "custom"
    enable_http_handshake_emulation: bool = False
    rotation_interval_seconds: int = 30
    handshake_timeout_ms: int = 5000
    session_idle_timeout_ms: int = 0
    transport_mtu: int = 1400

    def ensure_defaults(self) -> "LinuxServerConfig":
        if self.clients:
            self.clients = [client.ensure_defaults() for client in self.clients]
        elif not self.psk_hex:
            self.psk_hex = generate_psk_hex()
        if not self.public_interface:
            self.public_interface = autodetect_public_interface()
        if (
            self.protocol_wrapper == "websocket"
            and self.persona_preset == "browser_ws"
            and not self.enable_http_handshake_emulation
        ):
            self.persona_preset = "custom"
        return self

    @property
    def tunnel_interface(self) -> ipaddress.IPv4Interface:
        interface = ipaddress.ip_interface(self.tun_address)
        if interface.version != 4:
            raise ValueError("Linux VPN server currently supports IPv4 tunnel addressing only")
        return interface

    @property
    def tunnel_server_ip(self) -> str:
        return str(self.tunnel_interface.ip)

    @property
    def tunnel_network_cidr(self) -> str:
        return str(self.tunnel_interface.network)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def _resolve_export_client(self, client_id: str | None = None) -> LinuxServerClientConfig | None:
        if not self.clients:
            return None
        enabled_clients = [client.ensure_defaults() for client in self.clients if client.enabled]
        if not enabled_clients:
            raise RuntimeError("at least one enabled client is required to export a client profile")
        if client_id is None:
            return enabled_clients[0]
        for client in enabled_clients:
            if client.client_id == client_id:
                return client
        raise RuntimeError(f"no enabled client found for client_id={client_id!r}")

    def export_client_profile(self, client_id: str | None = None) -> ClientConnectionProfile:
        if not self.public_host:
            raise RuntimeError("public_host must be set before exporting a client profile")
        selected_client = self._resolve_export_client(client_id)
        return export_client_profile(
            server_host=self.public_host,
            server_port=self.bind_port,
            psk_hex=selected_client.psk_hex if selected_client else self.psk_hex,
            client_name=selected_client.client_name if selected_client else "veil-client",
            client_id=selected_client.client_id if selected_client else "",
            tunnel_mode="dynamic",
            tun_name="veilfull0",
            tun_address="",
            tun_peer="",
            packet_mtu=self.packet_mtu,
            keepalive_interval=self.keepalive_interval,
            keepalive_timeout=self.keepalive_timeout,
            protocol_wrapper=self.protocol_wrapper,
            persona_preset=self.persona_preset,
            enable_http_handshake_emulation=self.enable_http_handshake_emulation,
            rotation_interval_seconds=self.rotation_interval_seconds,
            handshake_timeout_ms=self.handshake_timeout_ms,
            session_idle_timeout_ms=self.session_idle_timeout_ms,
            transport_mtu=self.transport_mtu,
        )


def load_server_config(path: Path) -> LinuxServerConfig:
    if not path.exists():
        return LinuxServerConfig().ensure_defaults()
    raw = json.loads(path.read_text(encoding="utf-8"))
    clients_raw = raw.get("clients") or []
    raw["clients"] = [LinuxServerClientConfig(**client) for client in clients_raw]
    return LinuxServerConfig(**raw).ensure_defaults()


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


def read_server_status(paths: LinuxServerPaths, config: LinuxServerConfig) -> dict[str, Any]:
    service_name = paths.service_path.name
    systemctl = shutil.which("systemctl")
    service_active = False
    service_enabled = False
    if systemctl:
        active = subprocess.run([systemctl, "is-active", service_name], check=False, capture_output=True, text=True)
        enabled = subprocess.run([systemctl, "is-enabled", service_name], check=False, capture_output=True, text=True)
        service_active = active.returncode == 0 and active.stdout.strip() == "active"
        service_enabled = enabled.returncode == 0 and enabled.stdout.strip() in {"enabled", "static", "generated", "alias"}

    profile_payload: dict[str, Any] | None = None
    if config.public_host:
        profile_payload = profile_summary(config.export_client_profile())

    return {
        "installed": paths.config_path.exists() and paths.launcher_path.exists() and paths.service_path.exists(),
        "service_active": service_active,
        "service_enabled": service_enabled,
        "public_host": config.public_host,
        "bind_host": config.bind_host,
        "bind_port": config.bind_port,
        "public_interface": config.public_interface,
        "server_name": config.server_name,
        "tun_name": config.tun_name,
        "tun_address": config.tun_address,
        "tun_peer": config.tun_peer,
        "packet_mtu": config.packet_mtu,
        "keepalive_interval": config.keepalive_interval,
        "keepalive_timeout": config.keepalive_timeout,
        "protocol_wrapper": config.protocol_wrapper,
        "persona_preset": config.persona_preset,
        "enable_http_handshake_emulation": config.enable_http_handshake_emulation,
        "rotation_interval_seconds": config.rotation_interval_seconds,
        "handshake_timeout_ms": config.handshake_timeout_ms,
        "session_idle_timeout_ms": config.session_idle_timeout_ms,
        "transport_mtu": config.transport_mtu,
        "config_path": str(paths.config_path),
        "client_profile_path": str(paths.client_profile_path),
        "client_profile_exists": paths.client_profile_path.exists(),
        "launcher_path": str(paths.launcher_path),
        "service_path": str(paths.service_path),
        "client_profile_summary": profile_payload,
    }
