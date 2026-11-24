from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.managed_contracts import GENERATION, IDENTIFIER, project_service_status


_CONTENT_LIMIT = 8192
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")


@dataclass(slots=True)
class LocalSupervisor:
    service_id: str
    port: int
    credential: str = field(repr=False)
    restart_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def _request(self, restart_generation: str | None = None) -> tuple[int, bytes]:
        if restart_generation is None:
            method, path, body = "GET", "/rag/v1/status", b""
        else:
            method, path = "POST", "/rag/v1/restart"
            body = json.dumps({"expected_generation": restart_generation}, separators=(",", ":")).encode("ascii")
        headers = (
            f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nConnection: close\r\n"
            f"Authorization: Bearer {self.credential}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode("ascii")
        writer = None
        try:
            async with asyncio.timeout(2):
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port, limit=_CONTENT_LIMIT)
                writer.write(headers + body)
                await writer.drain()
                raw = await reader.readuntil(b"\r\n\r\n")
                first, *fields = raw[:-4].split(b"\r\n")
                matched = re.fullmatch(rb"HTTP/1\.[01] ([1-5][0-9]{2})(?: [^\r\n]*)?", first)
                lengths = []
                for line in fields:
                    name, separator, value = line.partition(b":")
                    if not separator or name.lower() == b"transfer-encoding":
                        raise ValueError("invalid HTTP response")
                    if name.lower() == b"content-length":
                        lengths.append(value.strip())
                if matched is None or len(lengths) != 1 or re.fullmatch(rb"[0-9]{1,5}", lengths[0]) is None:
                    raise ValueError("invalid HTTP response")
                length = int(lengths[0])
                if length > _CONTENT_LIMIT:
                    raise ValueError("oversized HTTP response")
                return int(matched[1]), await reader.readexactly(length)
        except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            raise ValueError("service.unavailable") from None
        finally:
            if writer is not None:
                writer.close()

    async def inspect(self) -> dict[str, Any]:
        status, body = await self._request()
        if status != 200:
            raise ValueError("service.unavailable")
        try:
            projected = project_service_status(json.loads(body))
        except (ValueError, RecursionError):
            raise ValueError("service.invalid_response") from None
        return {"service_id": self.service_id, **projected}

    async def restart(self, generation: str) -> dict[str, Any]:
        if not isinstance(generation, str) or GENERATION.fullmatch(generation) is None:
            raise ValueError("service.invalid_generation")
        if self.restart_lock.locked():
            raise ValueError("service.busy")
        async with self.restart_lock:
            before = await self.inspect()
            if before["generation"] != generation:
                raise ValueError("service.generation_changed")
            if before["state"] not in {"degraded", "stopped"}:
                raise ValueError("service.not_recoverable")
            try:
                status, _ = await self._request(generation)
            except ValueError:
                raise ValueError("service.recovery_unconfirmed") from None
            if status == 409:
                raise ValueError("service.generation_changed")
            if status != 202:
                raise ValueError("service.recovery_unconfirmed")

            latest = before
            try:
                async with asyncio.timeout(12):
                    while True:
                        try:
                            latest = await self.inspect()
                        except ValueError:
                            pass
                        if latest["generation"] != generation and latest["state"] == "ready":
                            break
                        await asyncio.sleep(0.3)
            except TimeoutError:
                pass
            return {**latest, "previous_generation": generation}


def load_local_supervisors(path: Path | None, workdir: Path) -> dict[str, LocalSupervisor]:
    if path is None:
        raise ValueError("service_control.v1 需要设备本地 --service-config")
    try:
        resolved = path.expanduser().resolve(strict=True)
        if resolved.is_relative_to(workdir.resolve()) or not resolved.is_file():
            raise ValueError
        with resolved.open("rb") as stream:
            contents = stream.read(_CONTENT_LIMIT + 1)
        if len(contents) > _CONTENT_LIMIT:
            raise ValueError
        config = json.loads(contents)
    except (OSError, RuntimeError, ValueError):
        raise ValueError("--service-config 必须是工作区外有效的本地服务配置文件") from None
    if (
        not isinstance(config, dict) or set(config) != {"services"}
        or not isinstance(config["services"], list) or not 1 <= len(config["services"]) <= 16
    ):
        raise ValueError("本地服务配置需要 1–16 个服务")
    supervisors = {}
    for item in config["services"]:
        if not isinstance(item, dict) or set(item) != {"id", "port", "credential"}:
            raise ValueError("本地服务必须使用 id、port 和 credential")
        service_id, port, credential = item["id"], item["port"], item["credential"]
        if (
            not isinstance(service_id, str) or IDENTIFIER.fullmatch(service_id) is None or service_id in supervisors
            or type(port) is not int or not 1024 <= port <= 65535
            or not isinstance(credential, str) or _TOKEN.fullmatch(credential) is None
        ):
            raise ValueError("本地服务 ID 重复或端口、凭据格式无效")
        supervisors[service_id] = LocalSupervisor(service_id, port, credential)
    return supervisors


def select_supervisor(supervisors: dict[str, LocalSupervisor], service_id: object) -> LocalSupervisor:
    if not isinstance(service_id, str) or service_id not in supervisors:
        raise ValueError("service.not_registered")
    return supervisors[service_id]
