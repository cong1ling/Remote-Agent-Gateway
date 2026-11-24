from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import ActionDecision, RuntimeEvent, RuntimeSession, RuntimeTask
from .health import HEALTH_PROFILE, OBSERVATION_TOOLS


CLAUDE_VERSION = "2.1.306"
CLAUDE_ADAPTER_VERSION = "0.1.0"
CLAUDE_KEY_ENV = "RAG_CLAUDE_API_KEY"
CLAUDE_FIXTURE_KEY = "rag-loopback-fixture-not-a-secret"
_MAX_OUTPUT_BYTES = 64 * 1024
_CLI_FLAGS = (
    "--print", "--bare", "--restricted", "--safe-mode",
    "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
    "--permission-mode", "dontAsk", "--permission-prompts", "none", "--no-chrome",
    "--disable-slash-commands", "--no-session-persistence", "--output-format", "json",
    "--system-prompt", "Return only the requested JSON object. You have no native tools.",
)
_PROMPT = (
    'Order the four independent local health observations. Return exactly {"actions":[...]} '
    "with each of these names appearing once, without arguments, Markdown or extra fields: "
    + ", ".join(OBSERVATION_TOOLS)
    + ". Do not infer or report device health; a separate broker performs the observations."
)


@dataclass(slots=True)
class ClaudeTurn:
    directory: tempfile.TemporaryDirectory[str]
    process: asyncio.subprocess.Process | None = None
    request_id: str | None = None
    decision: str | None = None


class ClaudeRuntime:
    provider_id = "claude"
    mode = "managed_limited"

    def __init__(self, executable: Path, model: str, *, api_key: str, fixture_port: int | None):
        self.executable = executable
        self.model = model
        self._api_key = api_key
        self._fixture_port = fixture_port
        self._turns: dict[str, ClaudeTurn] = {}

    @classmethod
    def from_installation(
        cls, executable: Path, model: str, *, workdir: Path, config_env: str | None = None,
        fixture_port: int | None = None,
    ) -> ClaudeRuntime:
        candidate = executable.expanduser()
        if not candidate.is_absolute():
            raise ValueError("--claude-bin 必须是本机原生可执行文件的绝对路径")
        path = candidate.resolve()
        if (
            not path.is_file() or path.is_relative_to(workdir.resolve())
            or (path.suffix.lower() != ".exe" if os.name == "nt" else not os.access(path, os.X_OK))
        ):
            raise ValueError("--claude-bin 必须指向工作区外可信的 Claude Code 原生可执行文件")
        if not re.fullmatch(r"claude-[a-z0-9-]{1,70}", model):
            raise ValueError("--claude-model 需要具体的 Claude 模型 ID")
        if config_env and config_env.upper() == CLAUDE_KEY_ENV:
            raise ValueError("不能把 Claude 专用 API Key 用作 Agent 设备凭据")
        if fixture_port is not None:
            if type(fixture_port) is not int or not 1 <= fixture_port <= 65535:
                raise ValueError("Claude 回环测试端口无效")
            api_key = CLAUDE_FIXTURE_KEY
        else:
            api_key = os.environ.get(CLAUDE_KEY_ENV, "").strip()
            if not re.fullmatch(r"[!-~]{1,512}", api_key) or api_key == CLAUDE_FIXTURE_KEY:
                raise ValueError(f"启用 Claude 需要设备所有者提供专用 {CLAUDE_KEY_ENV}")
        runtime = cls(path, model, api_key=api_key, fixture_port=fixture_port)
        try:
            with tempfile.TemporaryDirectory(prefix="rag-claude-check-") as directory:
                checked = subprocess.run(
                    [str(path), "--version"], cwd=directory, env=runtime._environment(directory),
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=8, check=True, text=True, encoding="utf-8",
                )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise ValueError("无法验证本机 Claude Code 版本") from exc
        if checked.stdout.strip() != f"{CLAUDE_VERSION} (Claude Code)":
            raise ValueError(f"当前健康检查适配器仅支持 Claude Code {CLAUDE_VERSION}")
        return runtime

    def _environment(self, directory: str, *, model_access: bool = False) -> dict[str, str]:
        system_variables = ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC")
        env = {name: os.environ[name] for name in system_variables if name in os.environ}
        env.update(dict.fromkeys(("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR"), directory))
        env.update({
            "CLAUDE_CONFIG_DIR": directory,
            "CLAUDE_CODE_TMPDIR": directory,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1024",
            "CLAUDE_CODE_MAX_TURNS": "1",
            "CLAUDE_CODE_MAX_RETRIES": "0",
            "CLAUDE_CODE_DISABLE_THINKING": "1",
            "ANTHROPIC_BASE_URL": (
                f"http://127.0.0.1:{self._fixture_port}" if self._fixture_port is not None else "https://api.anthropic.com"
            ),
        })
        if model_access:
            env["ANTHROPIC_API_KEY"] = self._api_key
        return env

    def declaration(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "runtime_version": CLAUDE_VERSION,
            "adapter_version": CLAUDE_ADAPTER_VERSION,
            "status": "available",
            "mode": self.mode,
            "supported_profiles": [HEALTH_PROFILE],
        }

    async def start(self, task: RuntimeTask) -> RuntimeSession:
        if len(task.actions) != len(OBSERVATION_TOOLS) or set(task.actions) != OBSERVATION_TOOLS.keys():
            raise ValueError("Claude 只支持四个固定 L0 观察")
        session_id = f"runtime_{uuid.uuid4().hex}"
        self._turns[session_id] = ClaudeTurn(tempfile.TemporaryDirectory(prefix="rag-claude-"))
        return RuntimeSession(session_id, self.provider_id, CLAUDE_VERSION, CLAUDE_ADAPTER_VERSION, self.mode, "running")

    async def events(self, session: RuntimeSession) -> AsyncIterator[RuntimeEvent]:
        turn = self._turns[session.session_id]
        actions = await self._request_plan(turn)
        for action in actions:
            if session.state != "running":
                return
            request_id = f"claude_call_{uuid.uuid4().hex}"
            turn.request_id, turn.decision = request_id, None
            yield RuntimeEvent("action_request", {"action": action}, request_id)
            if turn.decision not in ("allow", "deny"):
                raise ValueError("Claude 观察未收到 Broker 决策")
        turn.request_id = None
        session.state = "completed"
        yield RuntimeEvent("turn_completed", {})

    async def decide_action(self, session: RuntimeSession, decision: ActionDecision) -> None:
        turn = self._turns.get(session.session_id)
        if (
            turn is None or session.state != "running" or turn.request_id != decision.action_request_id
            or turn.decision is not None or decision.decision not in ("allow", "deny")
        ):
            raise ValueError("Claude 观察决策与会话不匹配")
        turn.decision = decision.decision

    async def _request_plan(self, turn: ClaudeTurn) -> list[str]:
        directory = turn.directory.name
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        try:
            async with asyncio.timeout(45):
                turn.process = await asyncio.create_subprocess_exec(
                    str(self.executable), *_CLI_FLAGS, "--model", self.model, "--", _PROMPT,
                    cwd=directory, env=self._environment(directory, model_access=True),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=_MAX_OUTPUT_BYTES, **options,
                )
                try:
                    await turn.process.stdout.readexactly(_MAX_OUTPUT_BYTES + 1)
                except asyncio.IncompleteReadError as exc:
                    output = exc.partial
                else:
                    raise ValueError("Claude Code 输出超过健康检查预算")
                if await turn.process.wait() != 0:
                    raise ValueError("Claude Code 未成功完成受限模型请求")
            return _parse_actions(output)
        except OSError:
            raise ValueError("无法启动受限 Claude Code") from None
        finally:
            await self._reap(turn)

    async def cancel(self, session: RuntimeSession) -> None:
        session.state = "cancelled"
        turn = self._turns.get(session.session_id)
        if turn:
            await self._reap(turn)

    async def close(self, session: RuntimeSession) -> None:
        turn = self._turns.pop(session.session_id, None)
        if turn:
            try:
                await self._reap(turn)
            finally:
                turn.directory.cleanup()

    @staticmethod
    async def _reap(turn: ClaudeTurn) -> None:
        process = turn.process
        turn.process = None
        if process:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.communicate()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError
    return value


def _parse_actions(output: bytes) -> list[str]:
    try:
        record = json.loads(output, object_pairs_hook=_unique_object)
        if (
            not isinstance(record, dict) or record.get("type") != "result" or record.get("subtype") != "success"
            or record.get("is_error") is not False or record.get("stop_reason") != "end_turn"
            or record.get("permission_denials") != []
            or type(record.get("num_turns")) is not int or record["num_turns"] != 1
            or not isinstance(record.get("result"), str) or len(record["result"]) > 1024
        ):
            raise ValueError
        plan = json.loads(record["result"], object_pairs_hook=_unique_object)
        if not isinstance(plan, dict) or plan.keys() != {"actions"} or not isinstance(plan["actions"], list):
            raise ValueError
        actions = plan["actions"]
        if (
            len(actions) != len(OBSERVATION_TOOLS) or not all(isinstance(action, str) for action in actions)
            or set(actions) != OBSERVATION_TOOLS.keys()
        ):
            raise ValueError
        return actions
    except (ValueError, RecursionError):
        raise ValueError("Claude Code 未返回四个无参、不可重复的固定观察") from None
