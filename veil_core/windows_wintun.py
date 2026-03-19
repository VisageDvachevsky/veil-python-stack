from __future__ import annotations

import ctypes
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


WINTUN_MIN_RING_CAPACITY = 0x20000
WINTUN_MAX_RING_CAPACITY = 0x4000000
WINTUN_MAX_IP_PACKET_SIZE = 0xFFFF
ERROR_BUFFER_OVERFLOW = 111
ERROR_HANDLE_EOF = 38
ERROR_INVALID_DATA = 13
ERROR_NO_MORE_ITEMS = 259
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def guid_from_uuid(value: uuid.UUID) -> GUID:
    data = value.bytes_le
    guid = GUID()
    ctypes.memmove(ctypes.byref(guid), data, ctypes.sizeof(guid))
    return guid


class WintunError(RuntimeError):
    pass


@dataclass
class NetworkRouteSnapshot:
    interface_index: int
    interface_alias: str
    next_hop: str
    server_ip: str


class WintunDll:
    def __init__(self, dll_path: Path) -> None:
        if not dll_path.exists():
            raise WintunError(f"wintun.dll not found at {dll_path}")
        self._dll = ctypes.WinDLL(str(dll_path), use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        self.WintunOpenAdapter = self._dll.WintunOpenAdapter
        self.WintunOpenAdapter.argtypes = [ctypes.c_wchar_p]
        self.WintunOpenAdapter.restype = ctypes.c_void_p

        self.WintunCreateAdapter = self._dll.WintunCreateAdapter
        self.WintunCreateAdapter.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(GUID)]
        self.WintunCreateAdapter.restype = ctypes.c_void_p

        self.WintunCloseAdapter = self._dll.WintunCloseAdapter
        self.WintunCloseAdapter.argtypes = [ctypes.c_void_p]
        self.WintunCloseAdapter.restype = None

        self.WintunStartSession = self._dll.WintunStartSession
        self.WintunStartSession.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.WintunStartSession.restype = ctypes.c_void_p

        self.WintunEndSession = self._dll.WintunEndSession
        self.WintunEndSession.argtypes = [ctypes.c_void_p]
        self.WintunEndSession.restype = None

        self.WintunGetReadWaitEvent = self._dll.WintunGetReadWaitEvent
        self.WintunGetReadWaitEvent.argtypes = [ctypes.c_void_p]
        self.WintunGetReadWaitEvent.restype = ctypes.c_void_p

        self.WintunReceivePacket = self._dll.WintunReceivePacket
        self.WintunReceivePacket.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self.WintunReceivePacket.restype = ctypes.c_void_p

        self.WintunReleaseReceivePacket = self._dll.WintunReleaseReceivePacket
        self.WintunReleaseReceivePacket.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.WintunReleaseReceivePacket.restype = None

        self.WintunAllocateSendPacket = self._dll.WintunAllocateSendPacket
        self.WintunAllocateSendPacket.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.WintunAllocateSendPacket.restype = ctypes.c_void_p

        self.WintunSendPacket = self._dll.WintunSendPacket
        self.WintunSendPacket.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.WintunSendPacket.restype = None

        self.WaitForSingleObject = self._kernel32.WaitForSingleObject
        self.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.WaitForSingleObject.restype = ctypes.c_uint32


class WintunSession:
    def __init__(self, dll: WintunDll, adapter_name: str, ring_capacity: int = 0x400000) -> None:
        if ring_capacity < WINTUN_MIN_RING_CAPACITY or ring_capacity > WINTUN_MAX_RING_CAPACITY:
            raise ValueError("ring_capacity out of Wintun bounds")
        self._dll = dll
        self._adapter_name = adapter_name
        self._adapter = self._open_or_create_adapter(adapter_name)
        self._session = self._dll.WintunStartSession(self._adapter, ring_capacity)
        if not self._session:
            raise WintunError(f"Failed to start Wintun session for {adapter_name}: {ctypes.get_last_error()}")
        self._read_event = self._dll.WintunGetReadWaitEvent(self._session)
        self._closed = False
        self._lock = threading.Lock()

    @property
    def adapter_name(self) -> str:
        return self._adapter_name

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._session:
                self._dll.WintunEndSession(self._session)
                self._session = None
            if self._adapter:
                self._dll.WintunCloseAdapter(self._adapter)
                self._adapter = None

    def send_packet(self, payload: bytes) -> None:
        if self._closed:
            raise WintunError("Wintun session is closed")
        if len(payload) > WINTUN_MAX_IP_PACKET_SIZE:
            raise WintunError(f"Packet too large for Wintun: {len(payload)}")
        packet_ptr = self._dll.WintunAllocateSendPacket(self._session, len(payload))
        if not packet_ptr:
            code = ctypes.get_last_error()
            if code == ERROR_BUFFER_OVERFLOW:
                return
            raise WintunError(f"WintunAllocateSendPacket failed: {code}")
        ctypes.memmove(packet_ptr, payload, len(payload))
        self._dll.WintunSendPacket(self._session, packet_ptr)

    def recv_packet(self, timeout_ms: int = 1000) -> bytes | None:
        if self._closed:
            return None
        size = ctypes.c_uint32(0)
        packet_ptr = self._dll.WintunReceivePacket(self._session, ctypes.byref(size))
        if packet_ptr:
            try:
                return ctypes.string_at(packet_ptr, size.value)
            finally:
                self._dll.WintunReleaseReceivePacket(self._session, packet_ptr)

        code = ctypes.get_last_error()
        if code == ERROR_NO_MORE_ITEMS:
            wait_result = self._dll.WaitForSingleObject(self._read_event, timeout_ms)
            if wait_result == WAIT_OBJECT_0 or wait_result == WAIT_TIMEOUT:
                return None
            raise WintunError(f"WaitForSingleObject failed: {wait_result}")
        if code in (ERROR_HANDLE_EOF, ERROR_INVALID_DATA):
            raise WintunError(f"Wintun receive failed: {code}")
        if code != 0:
            raise WintunError(f"WintunReceivePacket failed: {code}")
        return None

    def _open_or_create_adapter(self, adapter_name: str) -> ctypes.c_void_p:
        adapter = self._dll.WintunOpenAdapter(adapter_name)
        if adapter:
            return adapter
        requested_guid = guid_from_uuid(uuid.uuid5(uuid.NAMESPACE_DNS, f"veil-vpn:{adapter_name}"))
        adapter = self._dll.WintunCreateAdapter(adapter_name, "VeilVPN", ctypes.byref(requested_guid))
        if not adapter:
            raise WintunError(f"Failed to open or create Wintun adapter '{adapter_name}': {ctypes.get_last_error()}")
        return adapter


def resolve_ipv4(host: str) -> str:
    return socket.gethostbyname(host)

