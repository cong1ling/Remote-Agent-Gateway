from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import sys
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection


PROTOCOL_VERSION = 1
SUPPORTED_PROFILES = ["shell.v1"]
RECONNECT_MIN_SECONDS = 2
RECONNECT_MAX_SECONDS = 30


class FatalAgentError(Exception):
    pass


class AgentConfig:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()

    def load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        with self.path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise FatalAgentError(f"设备配置格式无效: {self.path}")
        return value

    def save(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, self.path)
        if os.name != "nt":
            os.chmod(self.path, 0o600)


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

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=self.workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.processes[request_id] = process
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise TimeoutError(f"命令执行超时 ({timeout:g}s)")
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.communicate()
            raise
        finally:
            self.processes.pop(request_id, None)

        return {
            "stdout": stdout.decode("utf-8", "replace"),
            "stderr": stderr.decode("utf-8", "replace"),
            "exit_code": process.returncode,
        }

    async def cancel(self, request_id: str) -> None:
        process = self.processes.get(request_id)
        if process is not None and process.returncode is None:
            process.kill()


class RemoteAgent:
    def __init__(
        self,
        gateway_url: str,
        config_store: AgentConfig,
        enrollment_token: str | None,
        name: str,
        description: str,
        workdir: Path,
    ):
        self.gateway_url = gateway_url
        self.config_store = config_store
        self.enrollment_token = enrollment_token
        self.name = name
        self.description = description
        self.executor = ShellExecutor(workdir)
        self.running: dict[str, asyncio.Task[None]] = {}
        self.send_lock = asyncio.Lock()

    def hello_message(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "type": "hello",
            "profiles": SUPPORTED_PROFILES,
            "metadata": {
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "python": platform.python_version(),
                "pid": os.getpid(),
            },
        }
        if self.enrollment_token:
            base.update(
                {
                    "enrollment_token": self.enrollment_token,
                    "name": self.name,
                    "description": self.description,
                }
            )
            return base
        config = self.config_store.load()
        if not config:
            raise FatalAgentError("首次运行需要提供 --token 完成 Agent 注册")
        base.update({"agent_id": config.get("agent_id"), "credential": config.get("credential")})
        return base

    async def send(self, websocket: ClientConnection, message: dict[str, Any]) -> None:
        async with self.send_lock:
            await websocket.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))

    async def run_request(self, websocket: ClientConnection, message: dict[str, Any]) -> None:
        request_id = message.get("request_id")
        try:
            if message.get("profile") != "shell.v1" or message.get("action") != "exec":
                raise ValueError("Agent 不支持该操作")
            params = message.get("params")
            if not isinstance(params, dict):
                raise ValueError("params 必须是对象")
            result = await self.executor.execute(request_id, params)
            response = {
                "v": PROTOCOL_VERSION,
                "type": "result",
                "request_id": request_id,
                "ok": True,
                "result": result,
            }
        except asyncio.CancelledError:
            response = {
                "v": PROTOCOL_VERSION,
                "type": "result",
                "request_id": request_id,
                "ok": False,
                "error": "任务已取消",
            }
        except Exception as exc:
            response = {
                "v": PROTOCOL_VERSION,
                "type": "result",
                "request_id": request_id,
                "ok": False,
                "error": str(exc),
            }
        try:
            await self.send(websocket, response)
        finally:
            self.running.pop(request_id, None)

    async def session(self) -> None:
        async with websockets.connect(
            self.gateway_url,
            open_timeout=15,
            ping_interval=20,
            ping_timeout=20,
            max_size=4 * 1024 * 1024,
        ) as websocket:
            await self.send(websocket, self.hello_message())
            raw = await asyncio.wait_for(websocket.recv(), timeout=15)
            response = json.loads(raw)
            if response.get("type") == "error":
                raise FatalAgentError(str(response.get("error") or "Agent 认证失败"))
            if response.get("type") == "registered":
                self.config_store.save(
                    {
                        "gateway_url": self.gateway_url,
                        "agent_id": response["agent_id"],
                        "credential": response["credential"],
                    }
                )
                self.enrollment_token = None
                print(f"Agent 注册成功: {response['agent_id']}")
            elif response.get("type") != "ready":
                raise FatalAgentError("网关返回了无效的握手响应")

            print(f"Agent 已连接: {self.gateway_url}")
            async for raw_message in websocket:
                message = json.loads(raw_message)
                message_type = message.get("type")
                if message_type == "ping":
                    await self.send(
                        websocket,
                        {"v": PROTOCOL_VERSION, "type": "pong", "ts": message.get("ts")},
                    )
                elif message_type == "request":
                    request_id = message.get("request_id")
                    if not isinstance(request_id, str) or request_id in self.running:
                        continue
                    task = asyncio.create_task(self.run_request(websocket, message))
                    self.running[request_id] = task
                elif message_type == "cancel":
                    request_id = message.get("request_id")
                    await self.executor.cancel(request_id)
                    task = self.running.get(request_id)
                    if task is not None:
                        task.cancel()

    async def run_forever(self) -> None:
        delay = RECONNECT_MIN_SECONDS
        while True:
            try:
                await self.session()
                delay = RECONNECT_MIN_SECONDS
            except FatalAgentError:
                raise
            except (OSError, TimeoutError, websockets.ConnectionClosed, json.JSONDecodeError) as exc:
                print(f"连接中断: {exc}; {delay} 秒后重试", file=sys.stderr)
            finally:
                for task in list(self.running.values()):
                    task.cancel()
                self.running.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)


def parse_args() -> argparse.Namespace:
    default_config = Path.home() / ".remote-agent" / "config.json"
    parser = argparse.ArgumentParser(description="Remote Agent Gateway 极简远程 Agent")
    parser.add_argument("--gateway", help="Agent WebSocket 地址，例如 ws://127.0.0.1:8000/ws/agent")
    parser.add_argument("--token", help="首次注册使用的一次性 Enrollment Token")
    parser.add_argument("--name", default=socket.gethostname(), help="Agent 显示名称")
    parser.add_argument("--description", default="", help="Agent 用途描述")
    parser.add_argument("--workdir", default=os.getcwd(), help="远程命令工作目录")
    parser.add_argument("--config", type=Path, default=default_config, help="设备凭据保存位置")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    config_store = AgentConfig(args.config)
    saved = config_store.load()
    gateway_url = args.gateway or (saved or {}).get("gateway_url")
    if not gateway_url:
        raise FatalAgentError("需要通过 --gateway 指定网关 WebSocket 地址")
    workdir = Path(args.workdir).expanduser().resolve()
    if not workdir.is_dir():
        raise FatalAgentError(f"工作目录不存在: {workdir}")
    agent = RemoteAgent(
        gateway_url=gateway_url,
        config_store=config_store,
        enrollment_token=args.token,
        name=args.name.strip() or socket.gethostname(),
        description=args.description.strip(),
        workdir=workdir,
    )
    await agent.run_forever()


def main() -> None:
    try:
        asyncio.run(async_main())
    except FatalAgentError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
