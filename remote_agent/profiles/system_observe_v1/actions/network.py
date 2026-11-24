from __future__ import annotations

import asyncio
import ctypes
import re
import socket
from pathlib import Path
from typing import Any

from remote_agent.profiles.base import ActionHandler

from ..common import observation_metadata, platform_id, read_bounded, run_macos_probe
from ..windows import windows_interface_addresses, windows_listening_ports


_NETWORK_LIMIT = 128
_TCP_TABLE_LIMIT = 1024 * 1024
_MACOS_TCP_STATES = {
    b"CLOSED", b"LISTEN", b"SYN_SENT", b"SYN_RCVD", b"ESTABLISHED", b"CLOSE_WAIT",
    b"FIN_WAIT_1", b"CLOSING", b"LAST_ACK", b"FIN_WAIT_2", b"TIME_WAIT",
}


class _IfAddrs(ctypes.Structure):
    pass


_IfAddrs._fields_ = [
    ("next", ctypes.POINTER(_IfAddrs)),
    ("name", ctypes.c_void_p),
    ("flags", ctypes.c_uint),
    ("address", ctypes.c_void_p),
]


class NetworkAction(ActionHandler):
    action_id = "get_network"

    async def execute(
        self,
        request_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        del request_id
        if params:
            raise ValueError("system_observe.v1 观察动作不接受参数")
        return await asyncio.to_thread(_collect_network)


def _collect_network() -> dict[str, Any]:
    system = platform_id()
    try:
        interfaces = [
            {"index": index, "name": name}
            for index, name in socket.if_nameindex()[:_NETWORK_LIMIT]
        ]
    except (AttributeError, OSError):
        interfaces = []

    ports = None
    addresses: set[str] = set()
    if system == "windows":
        addresses = windows_interface_addresses(_NETWORK_LIMIT)
        ports = windows_listening_ports()
    elif system in {"linux", "macos"}:
        addresses = _posix_interface_addresses(system)
        ports = _linux_listening_ports() if system == "linux" else _macos_listening_ports()

    listeners = None
    if ports is not None:
        listeners = {
            "ports": sorted(ports)[:_NETWORK_LIMIT],
            "truncated": len(ports) > _NETWORK_LIMIT,
        }

    return {
        **observation_metadata(),
        "hostname": socket.gethostname(),
        "interfaces": interfaces,
        "addresses": sorted(addresses)[:_NETWORK_LIMIT],
        "tcp_listeners": listeners,
    }


def _posix_interface_addresses(system: str) -> set[str]:
    addresses: set[str] = set()
    is_macos = system == "macos"
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib" if is_macos else None)
        head = ctypes.POINTER(_IfAddrs)()
        if libc.getifaddrs(ctypes.byref(head)) != 0:
            return addresses
        try:
            current = head
            for index in range(4096):
                if not current or len(addresses) >= _NETWORK_LIMIT:
                    break
                entry = current.contents
                address = entry.address
                if address:
                    if is_macos:
                        size, family = ctypes.string_at(address, 2)
                    else:
                        size, family = 0, ctypes.c_ushort.from_address(address).value
                    if family == socket.AF_INET and (not is_macos or size >= 8):
                        addresses.add(socket.inet_ntop(family, ctypes.string_at(address + 4, 4)))
                    elif family == socket.AF_INET6 and (not is_macos or size >= 24):
                        addresses.add(socket.inet_ntop(family, ctypes.string_at(address + 8, 16)))
                current = entry.next
        finally:
            libc.freeifaddrs(head)
    except (AttributeError, OSError):
        return set()
    return addresses


def _linux_listening_ports() -> set[int] | None:
    ports: set[int] = set()
    for name, width in (("tcp", 8), ("tcp6", 32)):
        try:
            sample = read_bounded(Path(f"/proc/net/{name}"), _TCP_TABLE_LIMIT)
        except OSError:
            return None
        if not sample or not sample.endswith(b"\n"):
            return None
        header, *rows = sample.splitlines()
        columns = header.split()
        if columns[:2] != [b"sl", b"local_address"] or columns[3:4] != [b"st"]:
            return None
        endpoint_pattern = re.compile(rb"[0-9A-Fa-f]{%d}:([0-9A-Fa-f]{4})" % width)
        for row in rows:
            fields = row.split()
            if len(fields) < 4 or not re.fullmatch(rb"\d+:", fields[0]) or not re.fullmatch(rb"[0-9A-Fa-f]{2}", fields[3]):
                return None
            endpoint = endpoint_pattern.fullmatch(fields[1])
            if endpoint is None:
                return None
            if fields[3].upper() == b"0A":
                port = int(endpoint[1], 16)
                if port == 0:
                    return None
                ports.add(port)
    return ports


def _macos_listening_ports() -> set[int] | None:
    sample = run_macos_probe(("/usr/sbin/netstat", "-an", "-p", "tcp"), _TCP_TABLE_LIMIT)
    if not sample or not sample.endswith(b"\n"):
        return None
    lines = sample.splitlines()
    if (
        len(lines) < 3
        or lines[0] != b"Active Internet connections (including servers)"
        or lines[1].split() != b"Proto Recv-Q Send-Q Local Address Foreign Address (state)".split()
    ):
        return None

    ports: set[int] = set()
    for line in lines[2:]:
        fields = line.split()
        if (
            len(fields) != 6
            or fields[0] not in {b"tcp4", b"tcp6", b"tcp46"}
            or not fields[1].isdigit()
            or not fields[2].isdigit()
            or fields[5] not in _MACOS_TCP_STATES
        ):
            return None
        if fields[5] == b"LISTEN":
            _, separator, suffix = fields[3].rpartition(b".")
            if not separator or not re.fullmatch(rb"\d{1,5}", suffix):
                return None
            port = int(suffix)
            if not 1 <= port <= 65535:
                return None
            ports.add(port)
    return ports
