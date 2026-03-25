from __future__ import annotations

import base64
import json
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from veil_core.protocol_catalog import describe_protocol_selection


def generate_psk_hex(num_bytes: int = 32) -> str:
    if num_bytes < 16:
        raise ValueError("PSK must be at least 16 bytes")
    return secrets.token_hex(num_bytes)


@dataclass
class ClientConnectionProfile:
    server_host: str
    server_port: int
    client_name: str = "veil-client"
    psk_hex: str = ""
    tunnel_mode: str = "dynamic"
    tun_name: str = "veilfull0"
    tun_address: str = ""
    tun_peer: str = ""
    packet_mtu: int = 1300
    keepalive_interval: float = 10.0
    keepalive_timeout: float = 30.0
    reconnect: bool = True
    auto_connect: bool = True
    protocol_wrapper: str = "none"
    persona_preset: str = "custom"
    enable_http_handshake_emulation: bool = False
    rotation_interval_seconds: int = 30
    handshake_timeout_ms: int = 5000
    session_idle_timeout_ms: int = 0
    transport_mtu: int = 1400

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json_text(cls, raw: str) -> "ClientConnectionProfile":
        return cls(**json.loads(raw))

    @classmethod
    def from_path(cls, path: Path) -> "ClientConnectionProfile":
        return cls.from_json_text(path.read_text(encoding="utf-8"))

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")

    def to_import_token(self) -> str:
        raw = self.to_json().encode("utf-8")
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return f"veil://profile/{encoded}"

    @classmethod
    def from_import_token(cls, token: str) -> "ClientConnectionProfile":
        prefix = "veil://profile/"
        if not token.startswith(prefix):
            raise ValueError("Unsupported profile token format")
        encoded = token[len(prefix):].strip()
        padding = "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(encoded + padding)
        return cls.from_json_text(raw.decode("utf-8"))


def export_client_profile(
    *,
    server_host: str,
    server_port: int,
    psk_hex: str,
    client_name: str = "veil-client",
    tunnel_mode: str = "dynamic",
    tun_name: str = "veilfull0",
    tun_address: str = "",
    tun_peer: str = "",
    packet_mtu: int = 1300,
    keepalive_interval: float = 10.0,
    keepalive_timeout: float = 30.0,
    protocol_wrapper: str = "none",
    persona_preset: str = "custom",
    enable_http_handshake_emulation: bool = False,
    rotation_interval_seconds: int = 30,
    handshake_timeout_ms: int = 5000,
    session_idle_timeout_ms: int = 0,
    transport_mtu: int = 1400,
) -> ClientConnectionProfile:
    return ClientConnectionProfile(
        server_host=server_host,
        server_port=server_port,
        client_name=client_name,
        psk_hex=psk_hex,
        tunnel_mode=tunnel_mode,
        tun_name=tun_name,
        tun_address=tun_address,
        tun_peer=tun_peer,
        packet_mtu=packet_mtu,
        keepalive_interval=keepalive_interval,
        keepalive_timeout=keepalive_timeout,
        protocol_wrapper=protocol_wrapper,
        persona_preset=persona_preset,
        enable_http_handshake_emulation=enable_http_handshake_emulation,
        rotation_interval_seconds=rotation_interval_seconds,
        handshake_timeout_ms=handshake_timeout_ms,
        session_idle_timeout_ms=session_idle_timeout_ms,
        transport_mtu=transport_mtu,
    )


def profile_summary(profile: ClientConnectionProfile) -> dict[str, Any]:
    protocol = describe_protocol_selection(
        profile.protocol_wrapper,
        profile.persona_preset,
        profile.enable_http_handshake_emulation,
    )
    return {
        "server_host": profile.server_host,
        "server_port": profile.server_port,
        "client_name": profile.client_name,
        "tunnel_mode": profile.tunnel_mode,
        "tun_name": profile.tun_name,
        "tun_address": profile.tun_address,
        "tun_peer": profile.tun_peer,
        "packet_mtu": profile.packet_mtu,
        "protocol_wrapper": profile.protocol_wrapper,
        "persona_preset": profile.persona_preset,
        "enable_http_handshake_emulation": profile.enable_http_handshake_emulation,
        "protocol_details": protocol,
        "rotation_interval_seconds": profile.rotation_interval_seconds,
        "handshake_timeout_ms": profile.handshake_timeout_ms,
        "session_idle_timeout_ms": profile.session_idle_timeout_ms,
        "transport_mtu": profile.transport_mtu,
        "psk_hex": profile.psk_hex,
        "import_token": profile.to_import_token(),
    }
