"""
veil_core — Python bindings for the Veil Protocol core library.

Usage:
    from veil_core import Server, Client

The heavy lifting (encryption, fragmentation, UDP I/O) happens entirely in
C++ via the ``_veil_core_ext`` extension module.  This package exposes a
friendly asyncio-compatible API on top.
"""

from veil_core.server import Server
from veil_core.client import Client
from veil_core.session import Session, SessionInfo
from veil_core.message import Message, encode_json_message, decode_json_message
from veil_core.vpn import (
    DEFAULT_CONTROL_STREAM_ID,
    DEFAULT_KEEPALIVE_INTERVAL,
    DEFAULT_KEEPALIVE_TIMEOUT,
    DEFAULT_PACKET_STREAM_ID,
    DEFAULT_VPN_PACKET_MTU,
    VPN_PROTOCOL_VERSION,
    VpnClient,
    VpnConnection,
    VpnPacket,
    VpnServer,
)
from veil_core.linux_proxy import (
    LinuxTunConfig,
    LinuxTunDevice,
    LinuxVpnProxy,
    LinuxVpnProxyClient,
    LinuxVpnProxyServer,
)
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
from veil_core.linux_server_app import (
    LinuxServerConfig,
    LinuxServerEnvironment,
    LinuxServerPaths,
    install_server_assets,
    load_server_config,
    save_server_config,
)
from veil_core.provisioning import (
    ClientConnectionProfile,
    export_client_profile,
    generate_psk_hex,
    profile_summary,
)
from veil_core._ext_loader import load_extension
from veil_core.events import (
    Event,
    NewConnectionEvent,
    DataEvent,
    DisconnectedEvent,
    ErrorEvent,
)

_veil_core_ext, _, _ = load_extension()

__all__ = [
    "Server",
    "Client",
    "Session",
    "SessionInfo",
    "Message",
    "encode_json_message",
    "decode_json_message",
    "VpnClient",
    "VpnConnection",
    "VpnPacket",
    "VpnServer",
    "LinuxTunConfig",
    "LinuxTunDevice",
    "LinuxVpnProxy",
    "LinuxVpnProxyClient",
    "LinuxVpnProxyServer",
    "LinuxClientConfig",
    "LinuxClientEnvironment",
    "LinuxClientPaths",
    "load_client_config",
    "save_client_config",
    "install_user_client",
    "build_action_command",
    "read_runtime_status",
    "start_runtime",
    "stop_runtime",
    "LinuxServerConfig",
    "LinuxServerEnvironment",
    "LinuxServerPaths",
    "load_server_config",
    "save_server_config",
    "install_server_assets",
    "ClientConnectionProfile",
    "generate_psk_hex",
    "export_client_profile",
    "profile_summary",
    "VPN_PROTOCOL_VERSION",
    "DEFAULT_CONTROL_STREAM_ID",
    "DEFAULT_KEEPALIVE_INTERVAL",
    "DEFAULT_KEEPALIVE_TIMEOUT",
    "DEFAULT_PACKET_STREAM_ID",
    "DEFAULT_VPN_PACKET_MTU",
    "Event",
    "NewConnectionEvent",
    "DataEvent",
    "DisconnectedEvent",
    "ErrorEvent",
    "_veil_core_ext",
]
