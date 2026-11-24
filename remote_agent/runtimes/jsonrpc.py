from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any


class JsonRpcError(RuntimeError):
    pass


class JsonRpcClient:
    def __init__(self, process: Any):
        self.process = process
        self._responses: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._reader_task = asyncio.create_task(self._read_loop())
        self._closed = False

    @classmethod
    async def spawn(
        cls,
        command: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> JsonRpcClient:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=1024 * 1024,
        )
        return cls(process)

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = 30,
    ) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._responses[request_id] = future
        try:
            async with asyncio.timeout(timeout):
                await self._write(
                    {"id": request_id, "method": method, "params": params or {}}
                )
                response = await future
        finally:
            self._responses.pop(request_id, None)
        if "error" in response:
            raise JsonRpcError(f"Codex app-server 拒绝 {method}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise JsonRpcError(f"Codex app-server 返回无效的 {method} 响应")
        return result

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        await self._write(message)

    async def respond(self, request_id: object, result: dict[str, Any]) -> None:
        await self._write({"id": request_id, "result": result})

    async def reject(self, request_id: object) -> None:
        await self._write(
            {
                "id": request_id,
                "error": {"code": -32601, "message": "Not supported in health checks"},
            }
        )

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            if self._closed and self._events.empty():
                return
            event = await self._events.get()
            if event.get("_closed"):
                return
            yield event

    async def close(self) -> None:
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
            if self.process.returncode is None:
                try:
                    self.process.terminate()
                    await asyncio.wait_for(self.process.wait(), timeout=3)
                except TimeoutError:
                    self.process.kill()
                    await self.process.wait()
                except ProcessLookupError:
                    await self.process.wait()
        finally:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._mark_closed()

    async def _write(self, message: dict[str, Any]) -> None:
        if self._closed or self.process.stdin is None:
            raise JsonRpcError("JSON-RPC 连接已关闭")
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            self.process.stdin.write(raw.encode("utf-8"))
            await self.process.stdin.drain()
        except (OSError, RuntimeError) as exc:
            raise JsonRpcError("Codex app-server stdio 已断开") from exc

    async def _read_loop(self) -> None:
        stdout = self.process.stdout
        try:
            if stdout is None:
                return
            while True:
                line = await stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                    break
                if not isinstance(message, dict):
                    break
                message_id = message.get("id")
                if message_id is not None and "method" not in message:
                    future = self._responses.get(str(message_id))
                    if future is not None and not future.done():
                        future.set_result(message)
                elif message_id is not None or message.get("method") in (
                    "turn/completed",
                    "item/started",
                ):
                    self._events.put_nowait(message)
        except (asyncio.QueueFull, OSError, ValueError):
            pass
        finally:
            self._mark_closed()

    def _mark_closed(self) -> None:
        if self._closed:
            return
        self._closed = True
        for future in self._responses.values():
            if not future.done():
                future.set_exception(JsonRpcError("JSON-RPC 进程已退出"))
        self._responses.clear()
        if not self._events.full():
            self._events.put_nowait({"_closed": True})


def sanitized_environment(
    environment: dict[str, str] | None = None,
    *,
    exclude: str | None = None,
) -> dict[str, str]:
    allowed = {
        "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR",
        "APPDATA", "LOCALAPPDATA", "TMP", "TEMP", "TMPDIR",
        "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "OPENAI_API_KEY", "CODEX_API_KEY",
    }
    if exclude:
        allowed.discard(exclude.upper())
    source = os.environ if environment is None else environment
    return {name: value for name, value in source.items() if name.upper() in allowed}
