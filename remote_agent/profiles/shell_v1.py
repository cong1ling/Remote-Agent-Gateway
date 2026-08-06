from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path
from typing import Any

from .base import ProfileHandler


MAX_CAPTURE_BYTES = 2 * 1024 * 1024
TRUNCATION_MARKER = b"\n[output truncated by remote agent]\n"


class ShellExecutor:
    def __init__(self, workdir: Path):
        self.workdir = workdir
        self.processes: dict[str, asyncio.subprocess.Process] = {}

    async def execute(self, request_id: str, params: dict[str, Any]) -> dict[str, Any]:
        command = params.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command 不能为空")
        timeout = float(params.get("timeout", 300))
        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")

        process_options: dict[str, Any] = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=self.workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_options,
        )
        self.processes[request_id] = process
        stdout_task = asyncio.create_task(self._capture_stream(process.stdout))
        stderr_task = asyncio.create_task(self._capture_stream(process.stderr))
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self._terminate(process)
            await process.wait()
            await asyncio.gather(stdout_task, stderr_task)
            raise TimeoutError(f"命令执行超时 ({timeout:g}s)")
        except asyncio.CancelledError:
            if process.returncode is None:
                await self._terminate(process)
                await process.wait()
            await asyncio.gather(stdout_task, stderr_task)
            raise
        finally:
            self.processes.pop(request_id, None)

        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        return {
            "stdout": stdout.decode("utf-8", "replace"),
            "stderr": stderr.decode("utf-8", "replace"),
            "exit_code": process.returncode,
        }

    @staticmethod
    async def _capture_stream(stream: asyncio.StreamReader | None) -> bytes:
        if stream is None:
            return b""
        captured = bytearray()
        truncated = False
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            remaining = MAX_CAPTURE_BYTES - len(captured)
            if remaining > 0:
                captured.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        if truncated:
            captured.extend(TRUNCATION_MARKER)
        return bytes(captured)

    async def cancel(self, request_id: str) -> None:
        process = self.processes.get(request_id)
        if process is not None and process.returncode is None:
            await self._terminate(process)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.returncode is None:
            process.kill()


class ShellV1Profile(ProfileHandler):
    profile_id = "shell.v1"

    def __init__(self, workdir: Path):
        self.executor = ShellExecutor(workdir)

    async def execute(
        self,
        action: str,
        request_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if action != "exec":
            raise ValueError(f"shell.v1 不支持操作: {action}")
        return await self.executor.execute(request_id, params)

    async def cancel(self, request_id: str) -> None:
        await self.executor.cancel(request_id)

