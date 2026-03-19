"""
veil_core — Python bindings for the Veil Protocol core library.

Usage:
    from veil_core import Server, Client

The heavy lifting (encryption, fragmentation, UDP I/O) happens entirely in
C++ via the ``_veil_core_ext`` extension module.  This package exposes a
friendly asyncio-compatible API on top.
"""

import importlib
import sys

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
from veil_core.provisioning import (
    ClientConnectionProfile,
    export_client_profile,
    generate_psk_hex,
    profile_summary,
)
from veil_core.windows_client_app import (
    WindowsClientConfig,
    WindowsClientEnvironment,
    WindowsClientPaths,
    configure_wintun_network,
    create_wintun_session,
    install_windows_client,
    load_client_config as load_windows_client_config,
    read_runtime_status as read_windows_runtime_status,
    save_client_config as save_windows_client_config,
    start_runtime as start_windows_runtime,
    stop_runtime as stop_windows_runtime,
)
from veil_core._ext_loader import load_extension
from veil_core.events import (
    Event,
    NewConnectionEvent,
    DataEvent,
    DisconnectedEvent,
    ErrorEvent,
)

if not sys.platform.startswith("win"):
    linux_proxy = importlib.import_module("veil_core.linux_proxy")
    LinuxTunConfig = linux_proxy.LinuxTunConfig
    LinuxTunDevice = linux_proxy.LinuxTunDevice
    LinuxVpnProxy = linux_proxy.LinuxVpnProxy
    LinuxVpnProxyClient = linux_proxy.LinuxVpnProxyClient
    LinuxVpnProxyServer = linux_proxy.LinuxVpnProxyServer

    linux_client_app = importlib.import_module("veil_core.linux_client_app")
    LinuxClientConfig = linux_client_app.LinuxClientConfig
    LinuxClientEnvironment = linux_client_app.LinuxClientEnvironment
    LinuxClientPaths = linux_client_app.LinuxClientPaths
    build_action_command = linux_client_app.build_action_command
    install_user_client = linux_client_app.install_user_client
    load_client_config = linux_client_app.load_client_config
    read_runtime_status = linux_client_app.read_runtime_status
    save_client_config = linux_client_app.save_client_config
    start_runtime = linux_client_app.start_runtime
    stop_runtime = linux_client_app.stop_runtime

    linux_server_app = importlib.import_module("veil_core.linux_server_app")
    LinuxServerConfig = linux_server_app.LinuxServerConfig
    LinuxServerEnvironment = linux_server_app.LinuxServerEnvironment
    LinuxServerPaths = linux_server_app.LinuxServerPaths
    install_server_assets = linux_server_app.install_server_assets
    load_server_config = linux_server_app.load_server_config
    save_server_config = linux_server_app.save_server_config
else:
    LinuxTunConfig = None
    LinuxTunDevice = None
    LinuxVpnProxy = None
    LinuxVpnProxyClient = None
    LinuxVpnProxyServer = None
    LinuxClientConfig = None
    LinuxClientEnvironment = None
    LinuxClientPaths = None
    build_action_command = None
    install_user_client = None
    load_client_config = None
    read_runtime_status = None
    save_client_config = None
    start_runtime = None
    stop_runtime = None
    LinuxServerConfig = None
    LinuxServerEnvironment = None
    LinuxServerPaths = None
    install_server_assets = None
    load_server_config = None
    save_server_config = None

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
    "WindowsClientConfig",
    "WindowsClientEnvironment",
    "WindowsClientPaths",
    "load_windows_client_config",
    "save_windows_client_config",
    "install_windows_client",
    "create_wintun_session",
    "configure_wintun_network",
    "read_windows_runtime_status",
    "start_windows_runtime",
    "stop_windows_runtime",
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
