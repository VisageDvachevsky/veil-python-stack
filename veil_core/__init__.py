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
    "Event",
    "NewConnectionEvent",
    "DataEvent",
    "DisconnectedEvent",
    "ErrorEvent",
    "_veil_core_ext",
]
