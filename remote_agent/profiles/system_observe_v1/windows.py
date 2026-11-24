from __future__ import annotations

import ctypes
import socket
from collections.abc import Callable
from typing import Any


_SYSTEM32_ONLY = 0x0800
_WIN_BUILTIN_ADMINISTRATORS_SID = 26
_SECURITY_MAX_SID_SIZE = 68
_SKIP_NON_UNICAST_AND_NAMES = 0x02 | 0x04 | 0x08 | 0x20
_TCP_TABLE_OWNER_PID_LISTENER = 3
_MIB_TCP_STATE_LISTEN = 2
_ERROR_BUFFER_OVERFLOW = 111
_ERROR_INSUFFICIENT_BUFFER = 122
_MAX_NATIVE_BUFFER = 1024 * 1024


class _Adapter(ctypes.Structure):
    _fields_ = [
        ("alignment", ctypes.c_uint64),
        ("next", ctypes.c_void_p),
        ("adapter_name", ctypes.c_void_p),
        ("first_unicast", ctypes.c_void_p),
    ]


class _UnicastAddress(ctypes.Structure):
    _fields_ = [
        ("alignment", ctypes.c_uint64),
        ("next", ctypes.c_void_p),
        ("sockaddr", ctypes.c_void_p),
        ("sockaddr_size", ctypes.c_int),
    ]


class _Tcp4Row(ctypes.Structure):
    _fields_ = [
        ("state", ctypes.c_uint32),
        ("local_address", ctypes.c_uint32),
        ("local_port", ctypes.c_uint32),
        ("remote_address", ctypes.c_uint32),
        ("remote_port", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
    ]


class _Tcp6Row(ctypes.Structure):
    _fields_ = [
        ("local_address", ctypes.c_ubyte * 16),
        ("local_scope", ctypes.c_uint32),
        ("local_port", ctypes.c_uint32),
        ("remote_address", ctypes.c_ubyte * 16),
        ("remote_scope", ctypes.c_uint32),
        ("remote_port", ctypes.c_uint32),
        ("state", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
    ]


def current_windows_admin() -> bool | None:
    try:
        advapi = ctypes.WinDLL("advapi32.dll", winmode=_SYSTEM32_ONLY)
        size = ctypes.c_uint32(_SECURITY_MAX_SID_SIZE)
        sid = ctypes.create_string_buffer(size.value)
        if not advapi.CreateWellKnownSid(_WIN_BUILTIN_ADMINISTRATORS_SID, None, sid, ctypes.byref(size)):
            return None
        is_member = ctypes.c_int()
        if not advapi.CheckTokenMembership(None, sid, ctypes.byref(is_member)):
            return None
        return bool(is_member.value)
    except (AttributeError, OSError):
        return None


def _read_ip_helper_buffer(query: Callable[[Any, Any], int], retry_status: int) -> tuple[Any, int] | None:
    size = ctypes.c_uint32(16 * 1024)
    for attempt in range(3):
        if not 0 < size.value <= _MAX_NATIVE_BUFFER:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        status = query(buffer, ctypes.byref(size))
        if status == 0 and 0 < size.value <= len(buffer):
            return buffer, size.value
        if status != retry_status:
            return None
    return None


def windows_interface_addresses(limit: int) -> set[str]:
    try:
        query = ctypes.WinDLL("iphlpapi.dll", winmode=_SYSTEM32_ONLY).GetAdaptersAddresses
        sample = _read_ip_helper_buffer(
            lambda buffer, size: query(socket.AF_UNSPEC, _SKIP_NON_UNICAST_AND_NAMES, None, buffer, size),
            _ERROR_BUFFER_OVERFLOW,
        )
    except (AttributeError, OSError):
        return set()
    if sample is None:
        return set()

    buffer, size = sample
    start = ctypes.addressof(buffer)
    end = start + size
    addresses: set[str] = set()
    adapter_pointer = start
    for adapter_index in range(4096):
        if not adapter_pointer or len(addresses) >= limit:
            break
        if not start <= adapter_pointer <= end - ctypes.sizeof(_Adapter):
            return set()
        adapter = _Adapter.from_address(adapter_pointer)
        unicast_pointer = adapter.first_unicast
        for address_index in range(4096):
            if not unicast_pointer or len(addresses) >= limit:
                break
            if not start <= unicast_pointer <= end - ctypes.sizeof(_UnicastAddress):
                return set()
            address = _UnicastAddress.from_address(unicast_pointer)
            sockaddr = address.sockaddr
            if sockaddr and address.sockaddr_size >= 2 and start <= sockaddr <= end - address.sockaddr_size:
                family = ctypes.c_uint16.from_address(sockaddr).value
                if family == socket.AF_INET and address.sockaddr_size >= 8:
                    addresses.add(socket.inet_ntop(family, ctypes.string_at(sockaddr + 4, 4)))
                elif family == socket.AF_INET6 and address.sockaddr_size >= 24:
                    addresses.add(socket.inet_ntop(family, ctypes.string_at(sockaddr + 8, 16)))
            unicast_pointer = address.next
        adapter_pointer = adapter.next
    return addresses


def windows_listening_ports() -> set[int] | None:
    ports: set[int] = set()
    try:
        query = ctypes.WinDLL("iphlpapi.dll", winmode=_SYSTEM32_ONLY).GetExtendedTcpTable
        for family, row_type in ((socket.AF_INET, _Tcp4Row), (socket.AF_INET6, _Tcp6Row)):
            sample = _read_ip_helper_buffer(
                lambda buffer, size: query(buffer, size, False, family, _TCP_TABLE_OWNER_PID_LISTENER, 0),
                _ERROR_INSUFFICIENT_BUFFER,
            )
            if sample is None:
                return None
            buffer, size = sample
            row_start = ctypes.sizeof(ctypes.c_uint32)
            row_size = ctypes.sizeof(row_type)
            if size < row_start:
                return None
            count = ctypes.c_uint32.from_buffer(buffer).value
            if count > (size - row_start) // row_size:
                return None
            for index in range(count):
                row = row_type.from_buffer(buffer, row_start + index * row_size)
                port = socket.ntohs(row.local_port & 0xFFFF)
                if not port or row.state != _MIB_TCP_STATE_LISTEN:
                    return None
                ports.add(port)
    except (AttributeError, OSError):
        return None
    return ports
