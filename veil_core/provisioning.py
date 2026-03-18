from __future__ import annotations

import base64
import json
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


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
    tun_name: str = "veilfull0"
    tun_address: str = "10.200.0.2/30"
    tun_peer: str = "10.200.0.1"
    packet_mtu: int = 1300
    keepalive_interval: float = 10.0
    keepalive_timeout: float = 30.0
    reconnect: bool = True
    auto_connect: bool = True
    protocol_wrapper: str = "none"
    persona_preset: str = "custom"

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
    tun_name: str = "veilfull0",
    tun_address: str = "10.200.0.2/30",
    tun_peer: str = "10.200.0.1",
    packet_mtu: int = 1300,
    keepalive_interval: float = 10.0,
    keepalive_timeout: float = 30.0,
    protocol_wrapper: str = "none",
    persona_preset: str = "custom",
) -> ClientConnectionProfile:
    return ClientConnectionProfile(
        server_host=server_host,
        server_port=server_port,
        client_name=client_name,
        psk_hex=psk_hex,
        tun_name=tun_name,
        tun_address=tun_address,
        tun_peer=tun_peer,
        packet_mtu=packet_mtu,
        keepalive_interval=keepalive_interval,
        keepalive_timeout=keepalive_timeout,
        protocol_wrapper=protocol_wrapper,
        persona_preset=persona_preset,
    )


def profile_summary(profile: ClientConnectionProfile) -> dict[str, Any]:
    return {
        "server_host": profile.server_host,
        "server_port": profile.server_port,
        "client_name": profile.client_name,
        "tun_name": profile.tun_name,
        "packet_mtu": profile.packet_mtu,
        "protocol_wrapper": profile.protocol_wrapper,
        "persona_preset": profile.persona_preset,
        "psk_hex": profile.psk_hex,
        "import_token": profile.to_import_token(),
    }
