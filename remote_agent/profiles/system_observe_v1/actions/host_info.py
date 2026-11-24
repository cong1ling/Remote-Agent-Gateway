from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

from remote_agent.profiles.base import ActionHandler

from ..common import observation_metadata, platform_id, read_bounded
from ..windows import current_windows_admin


class HostInfoAction(ActionHandler):
    action_id = "get_host_info"

    async def execute(
        self,
        request_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        del request_id
        if params:
            raise ValueError("system_observe.v1 观察动作不接受参数")
        return await asyncio.to_thread(_collect_host_info)


def _collect_host_info() -> dict[str, Any]:
    system = platform_id()
    uname = platform.uname()
    result = {
        **observation_metadata(),
        "os": system,
        "system": uname.system,
        "release": uname.release,
        "version": uname.version,
        "architecture": platform.machine(),
        "processor": platform.processor(),
        "hostname": socket.gethostname(),
        "python": sys.version.split()[0],
        "pid": os.getpid(),
        "privileges": _current_privileges(system),
    }
    if system == "linux":
        result["linux_distribution"] = _linux_distribution()
        result["linux_tools"] = {
            f"{tool}_on_path": shutil.which(tool) is not None
            for tool in ("sudo", "docker", "podman")
        }
    return result


def _current_privileges(system: str) -> dict[str, bool | None]:
    if system == "windows":
        return {"is_admin": current_windows_admin(), "is_root": None}
    try:
        is_root = os.geteuid() == 0
    except (AttributeError, OSError):
        is_root = None
    return {"is_admin": None, "is_root": is_root}


def _linux_distribution() -> dict[str, str | None] | None:
    for path in (Path("/etc/os-release"), Path("/usr/lib/os-release")):
        try:
            sample = read_bounded(path, 16 * 1024)
        except FileNotFoundError:
            continue
        except OSError:
            return None
        if sample is None:
            return None

        values: dict[bytes, str] = {}
        for line in sample.splitlines():
            key, _, raw = line.partition(b"=")
            if key not in {b"ID", b"VERSION_ID"}:
                continue
            raw = raw.strip()
            if len(raw) >= 2 and raw[:1] in (b"'", b'"') and raw[-1:] == raw[:1]:
                raw = raw[1:-1]
            if re.fullmatch(rb"[a-z0-9._-]{1,64}", raw):
                values[key] = raw.decode("ascii")
            else:
                values.pop(key, None)
        distribution_id = values.get(b"ID")
        return None if distribution_id is None else {
            "id": distribution_id,
            "version_id": values.get(b"VERSION_ID"),
        }
    return None
