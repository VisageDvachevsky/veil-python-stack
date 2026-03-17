"""
Event types emitted by Server and Client.

All events are plain dataclasses so they are easy to pattern-match,
serialize, and extend without touching C++.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class EventKind(str, enum.Enum):
    NEW_CONNECTION = "NEW_CONNECTION"
    DATA = "DATA"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class Event:
    """Base class for all Veil protocol events."""
    kind: EventKind
    session_id: int  # 64-bit session tag from the C++ layer


@dataclass(frozen=True)
class NewConnectionEvent(Event):
    """Emitted when a peer completes the session setup."""
    kind: EventKind = field(default=EventKind.NEW_CONNECTION, init=False)
    remote_host: str = ""
    remote_port: int = 0


@dataclass(frozen=True)
class DataEvent(Event):
    """Emitted when plaintext data has been received and decrypted."""
    kind: EventKind = field(default=EventKind.DATA, init=False)
    stream_id: int = 0
    data: bytes = b""


@dataclass(frozen=True)
class DisconnectedEvent(Event):
    """Emitted when a session is torn down."""
    kind: EventKind = field(default=EventKind.DISCONNECTED, init=False)
    reason: str = ""


@dataclass(frozen=True)
class ErrorEvent(Event):
    """Emitted when the C++ layer reports a non-fatal error."""
    kind: EventKind = field(default=EventKind.ERROR, init=False)
    message: str = ""
