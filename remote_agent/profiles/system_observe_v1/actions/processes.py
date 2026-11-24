from __future__ import annotations

import asyncio
import csv
import io
import os
import subprocess
from typing import Any

from remote_agent.profiles.base import ActionHandler

from ..common import observation_metadata


class ProcessesAction(ActionHandler):
    action_id = "get_processes"

    async def execute(
        self,
        request_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        del request_id
        if params:
            raise ValueError("system_observe.v1 观察动作不接受参数")
        return await asyncio.to_thread(_collect_processes)


def _collect_processes() -> dict[str, Any]:
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                check=False,
                capture_output=True,
                timeout=5,
            )
            processes = _parse_windows(completed.stdout.decode("utf-8", "replace"))
        else:
            completed = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,stat=,%cpu=,%mem=,comm="],
                check=False,
                capture_output=True,
                timeout=5,
                env={**os.environ, "LC_ALL": "C"},
            )
            processes = _parse_posix(completed.stdout.decode("utf-8", "replace"))
        return {
            **observation_metadata(),
            "available": completed.returncode == 0,
            "processes": processes[:100],
            "truncated": len(processes) > 100,
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            **observation_metadata(),
            "available": False,
            "processes": [],
            "truncated": False,
            "error": type(exc).__name__,
        }


def _parse_windows(output: str) -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    for row in csv.reader(io.StringIO(output)):
        if len(row) < 5:
            continue
        try:
            pid = int(row[1])
        except ValueError:
            continue
        memory = row[4].replace(",", "").replace(" K", "").strip()
        processes.append(
            {
                "pid": pid,
                "name": row[0][:160],
                "memory_kb": int(memory) if memory.isdigit() else None,
            }
        )
    return processes


def _parse_posix(output: str) -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    for line in output.splitlines():
        fields = line.split(None, 5)
        if len(fields) != 6:
            continue
        try:
            pid = int(fields[0])
            ppid = int(fields[1])
            cpu_percent = float(fields[3])
            memory_percent = float(fields[4])
        except ValueError:
            continue
        processes.append(
            {
                "pid": pid,
                "ppid": ppid,
                "state": fields[2][:16],
                "name": fields[5].rstrip()[:160],
                "cpu_percent": cpu_percent,
                "memory_percent": memory_percent,
            }
        )
    return processes
