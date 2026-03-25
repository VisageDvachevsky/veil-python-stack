from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ProtocolWrapperOption:
    value: str
    label: str
    summary: str
    best_for: str
    supports_http_upgrade: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PersonaPresetOption:
    value: str
    label: str
    summary: str
    best_with: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


WRAPPER_OPTIONS: tuple[ProtocolWrapperOption, ...] = (
    ProtocolWrapperOption(
        value="none",
        label="Raw UDP",
        summary="Lowest overhead. No wrapper camouflage, best for trusted paths and lab validation.",
        best_for="Maximum throughput and minimal transport overhead.",
        supports_http_upgrade=False,
    ),
    ProtocolWrapperOption(
        value="websocket",
        label="WebSocket",
        summary="Wraps the transport as browser-like WebSocket traffic. Best choice for camouflage-oriented deployments.",
        best_for="Browser-shaped traffic and HTTP upgrade prelude support.",
        supports_http_upgrade=True,
    ),
    ProtocolWrapperOption(
        value="tls",
        label="TLS",
        summary="TLS-like framing for low-noise enterprise-looking traffic with less overhead than WebSocket+HTTP.",
        best_for="Enterprise-looking traffic with a cleaner envelope than raw UDP.",
        supports_http_upgrade=False,
    ),
)


PERSONA_OPTIONS: tuple[PersonaPresetOption, ...] = (
    PersonaPresetOption(
        value="custom",
        label="Custom",
        summary="Use explicit wrapper settings without additional persona shaping.",
        best_with="Best when you know the transport profile you want.",
    ),
    PersonaPresetOption(
        value="browser_ws",
        label="Browser WebSocket",
        summary="Biases transport behaviour toward browser/WebSocket patterns.",
        best_with="Pairs naturally with the WebSocket wrapper, often with HTTP upgrade emulation.",
    ),
    PersonaPresetOption(
        value="quic_media",
        label="QUIC Media",
        summary="Performance-oriented persona with media-like traffic shape.",
        best_with="Good fit for throughput-focused paths and lighter camouflage overhead.",
    ),
    PersonaPresetOption(
        value="interactive_game_udp",
        label="Interactive Game UDP",
        summary="Latency-oriented persona for bursty interactive UDP-like cadence.",
        best_with="Useful when low latency matters more than strict browser mimicry.",
    ),
    PersonaPresetOption(
        value="low_noise_enterprise",
        label="Low Noise Enterprise",
        summary="Conservative low-noise profile aimed at enterprise-like traffic appearance.",
        best_with="Usually a good companion for TLS or low-friction corporate egress.",
    ),
)


def protocol_wrapper_catalog() -> list[dict[str, Any]]:
    return [option.to_dict() for option in WRAPPER_OPTIONS]


def persona_preset_catalog() -> list[dict[str, Any]]:
    return [option.to_dict() for option in PERSONA_OPTIONS]


def protocol_catalog_payload() -> dict[str, Any]:
    return {
        "wrappers": protocol_wrapper_catalog(),
        "personas": persona_preset_catalog(),
    }


def _find_wrapper(value: str) -> ProtocolWrapperOption | None:
    normalized = (value or "").strip().lower()
    for option in WRAPPER_OPTIONS:
        if option.value == normalized:
            return option
    return None


def _find_persona(value: str) -> PersonaPresetOption | None:
    normalized = (value or "").strip().lower()
    for option in PERSONA_OPTIONS:
        if option.value == normalized:
            return option
    return None


def describe_protocol_selection(
    protocol_wrapper: str,
    persona_preset: str,
    enable_http_handshake_emulation: bool,
) -> dict[str, Any]:
    wrapper = _find_wrapper(protocol_wrapper)
    persona = _find_persona(persona_preset)
    notes: list[str] = []

    if enable_http_handshake_emulation and (wrapper is None or wrapper.value != "websocket"):
        notes.append("HTTP upgrade emulation has an effect only with the WebSocket wrapper.")
    if wrapper and wrapper.value == "websocket" and enable_http_handshake_emulation:
        notes.append("WebSocket+HTTP upgrade is the most browser-like combination currently exposed in the Python stack.")
    if persona and persona.value == "browser_ws" and (wrapper is None or wrapper.value != "websocket"):
        notes.append("The browser_ws persona is strongest when paired with the WebSocket wrapper.")
    if persona and persona.value == "low_noise_enterprise" and wrapper and wrapper.value == "none":
        notes.append("low_noise_enterprise usually benefits from TLS framing instead of raw UDP.")
    if persona and persona.value == "custom":
        notes.append("Custom keeps shaping conservative and leaves the wrapper choice as the dominant behaviour.")

    return {
        "wrapper": wrapper.to_dict() if wrapper is not None else {
            "value": protocol_wrapper,
            "label": protocol_wrapper or "unknown",
            "summary": "Unknown wrapper value.",
            "best_for": "",
            "supports_http_upgrade": False,
        },
        "persona": persona.to_dict() if persona is not None else {
            "value": persona_preset,
            "label": persona_preset or "unknown",
            "summary": "Unknown persona preset.",
            "best_with": "",
        },
        "http_upgrade_enabled": bool(enable_http_handshake_emulation),
        "notes": notes,
    }
