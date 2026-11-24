from __future__ import annotations

import os
import platform
import re
import select
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def platform_id() -> str:
    system = platform.system().lower()
    return {"windows": "windows", "darwin": "macos", "linux": "linux"}.get(system, "posix")


def observed_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def observation_metadata() -> dict[str, Any]:
    return {"observed_at": observed_at(), "observed_at_unix": time.time()}


def disk_snapshot(workdir: Path) -> dict[str, int]:
    usage = shutil.disk_usage(workdir)
    return {
        "path": str(workdir.anchor or workdir),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


def read_bounded(path: Path, limit: int) -> bytes | None:
    with path.open("rb") as stream:
        sample = stream.read(limit + 1)
    return sample if len(sample) <= limit else None


def run_macos_probe(command: tuple[str, ...], limit: int) -> bytes | None:
    deadline = time.monotonic() + 2
    try:
        with subprocess.Popen(
            command,
            cwd="/",
            env={"LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as process:
            try:
                output = bytearray()
                while len(output) <= limit:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                        return None
                    chunk = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(output)))
                    if not chunk:
                        status = process.wait(timeout=max(0, deadline - time.monotonic()))
                        return bytes(output) if status == 0 else None
                    output.extend(chunk)
            finally:
                if process.poll() is None:
                    process.kill()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return None


def memory_snapshot() -> dict[str, int] | None:
    system = platform_id()
    if system == "windows":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_uint32),
                ("memory_load", ctypes.c_uint32),
                ("total_physical", ctypes.c_uint64),
                ("available_physical", ctypes.c_uint64),
                ("total_page_file", ctypes.c_uint64),
                ("available_page_file", ctypes.c_uint64),
                ("total_virtual", ctypes.c_uint64),
                ("available_virtual", ctypes.c_uint64),
                ("available_extended", ctypes.c_uint64),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {
                "total_bytes": int(status.total_physical),
                "available_bytes": int(status.available_physical),
                "used_bytes": int(status.total_physical - status.available_physical),
                "utilization_percent": int(status.memory_load),
            }
        return None

    if system == "macos":
        return _macos_memory_snapshot()

    try:
        sample = read_bounded(Path("/proc/meminfo"), 16 * 1024)
    except OSError:
        return None
    if sample is None:
        return None
    values: dict[bytes, int] = {}
    for line in sample.splitlines():
        name, _, raw_value = line.partition(b":")
        parts = raw_value.split()
        if name in {b"MemTotal", b"MemAvailable", b"MemFree"} and parts and re.fullmatch(rb"\d{1,20}", parts[0]):
            multiplier = 1024 if len(parts) > 1 and parts[1].lower() == b"kb" else 1
            values[name] = int(parts[0]) * multiplier
    total = values.get(b"MemTotal")
    available = values.get(b"MemAvailable", values.get(b"MemFree"))
    if total is None or total <= 0 or available is None or not 0 <= available <= total:
        return None
    return _memory_totals(total, total - available)


def _macos_memory_snapshot() -> dict[str, int] | None:
    total_output = run_macos_probe(("/usr/sbin/sysctl", "-n", "hw.memsize"), 64)
    if total_output is None or not re.fullmatch(rb"[1-9]\d{0,19}", total_output.strip()):
        return None
    total = int(total_output)
    if total >= 2**64:
        return None

    sample = run_macos_probe(("/usr/bin/vm_stat",), 8192)
    used = _macos_used_bytes(sample) if sample is not None else None
    if used is None or not 0 < used <= total:
        return {"total_bytes": total}
    return _memory_totals(total, used)


def _macos_used_bytes(sample: bytes) -> int | None:
    """Estimate used RAM from pageable anonymous, wired, and physical compressor pages."""
    header, _, body = sample.partition(b"\n")
    page_size_match = re.fullmatch(rb"Mach Virtual Memory Statistics: \(page size of (\d{1,6}) bytes\)", header)
    if page_size_match is None:
        return None
    page_size = int(page_size_match[1])
    if page_size < 4096 or page_size & (page_size - 1):
        return None

    required = {b"Anonymous pages", b"Pages wired down", b"Pages occupied by compressor"}
    counts: dict[bytes, int] = {}
    for line in body.splitlines():
        row = re.fullmatch(rb"([^:]+):\s*(\d{1,20})\.", line)
        if row is None:
            return None
        if row[1] in required:
            if row[1] in counts:
                return None
            counts[row[1]] = int(row[2])
    if counts.keys() != required or not counts[b"Pages wired down"]:
        return None
    return sum(counts.values()) * page_size


def _memory_totals(total: int, used: int) -> dict[str, int]:
    return {
        "total_bytes": total,
        "available_bytes": total - used,
        "used_bytes": used,
        "utilization_percent": round(used * 100 / total),
    }
