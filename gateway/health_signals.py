from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _digest(fields: Any) -> str:
    return hashlib.sha256(json.dumps(fields, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def _percent(value: Any) -> float | None:
    return round(value, 1) if type(value) in (int, float) and 0 <= value <= 100 else None


def monitor_observation(action: str, result: dict[str, Any]) -> dict[str, Any]:
    if action == "get_host_info":
        family = result["os"]
        return {
            "platform": family if family in ("windows", "macos", "linux", "posix") else "unknown",
            "fingerprint": _digest([result[field] for field in ("os", "release", "version", "architecture")]),
        }
    if action == "get_resources":
        cpu = result["cpu"]
        load = cpu.get("load_average")
        first_load = load[0] if isinstance(load, list) and load else None
        load_ratio = None
        count = cpu.get("logical_count")
        if type(first_load) in (int, float) and 0 <= first_load <= 1_000_000 and count:
            load_ratio = round(first_load / count, 2)
        memory = result["memory"] or {}
        disks = result["disks"]
        free = [
            _percent(100 * (disk["free_bytes"] / disk["total_bytes"]))
            for disk in disks if disk["total_bytes"] and disk["free_bytes"] <= disk["total_bytes"]
        ]
        return {
            "load_per_core": load_ratio, "memory_percent": _percent(memory.get("utilization_percent")),
            "disk_count": len(disks), "lowest_free_percent": min(free, default=None),
        }
    if action == "get_processes":
        return {
            "available": result["available"], "truncated": result["truncated"],
            "sample_count": len(result["processes"]),
        }
    interfaces = result["interfaces"]
    addresses = result["addresses"]
    return {
        "interface_count": len(interfaces), "address_count": len(addresses),
        "fingerprint": _digest([
            sorted({(item["index"], item["name"]) for item in interfaces}), sorted(set(addresses)),
        ]),
    }


def monitor_changes(
    previous: dict[str, Any] | None, current: dict[str, Any],
) -> list[tuple[str, str, str, dict[str, Any]]]:
    if not previous:
        return []
    changes = []
    for action, category, trigger in (
        ("get_host_info", "monitor_os_changed", "两次已验证巡检之间的系统版本或架构发生变化"),
        ("get_network", "monitor_network_changed", "两次已验证巡检之间的本机网络接口或地址摘要发生变化"),
    ):
        old, new = previous[action], current[action]
        if old["fingerprint"] != new["fingerprint"]:
            basis = {"changed": True}
            if action == "get_network":
                basis.update(previous_interface_count=old["interface_count"], interface_count=new["interface_count"])
            changes.append((action, category, trigger, basis))
    old, new = previous["get_processes"], current["get_processes"]
    if old["available"] and new["available"] and not (old["truncated"] or new["truncated"]):
        old_count, new_count = old["sample_count"], new["sample_count"]
        if abs(new_count - old_count) >= max(20, math.ceil(old_count / 2)):
            changes.append((
                "get_processes", "monitor_process_count", "两次未截断的进程观察计数明显不同",
                {"previous_count": old_count, "current_count": new_count},
            ))
    return changes
