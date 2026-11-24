from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
from pathlib import Path

from remote_agent.core import (
    AgentConfig,
    EnvironmentAgentConfig,
    FatalAgentError,
    RemoteAgent,
    load_manifest,
    privileged_shell_warning,
)
from remote_agent.profiles import build_profile_handlers
from remote_agent.runtimes.base import LocalAgentRuntime
from remote_agent.runtimes.claude import ClaudeRuntime
from remote_agent.runtimes.codex import CodexRuntime
from remote_agent.runtimes.health import HEALTH_PROFILE


def parse_args(default_config: Path | None = None) -> argparse.Namespace:
    config_path = default_config or (Path.home() / ".remote-agent" / "config.json")
    parser = argparse.ArgumentParser(description="Remote Agent Gateway 极简远程 Agent")
    parser.add_argument("--gateway", help="Agent WebSocket 地址，例如 ws://127.0.0.1:8000/ws/agent")
    parser.add_argument("--token", help="首次注册使用的一次性 Enrollment Token")
    parser.add_argument("--name", help="Agent 显示名称，默认读取 Manifest 或主机名")
    parser.add_argument("--description", help="Agent 用途描述，默认读取 Manifest")
    parser.add_argument("--workdir", default=os.getcwd(), help="远程命令工作目录")
    config_group = parser.add_mutually_exclusive_group()
    config_group.add_argument(
        "--config", type=Path, default=config_path, help="设备凭据保存位置"
    )
    config_group.add_argument(
        "--config-env",
        metavar="NAME",
        help=(
            "从指定环境变量读取 Base64 JSON 设备凭据；适用于 CI、容器和只读环境"
        ),
    )
    parser.add_argument("--manifest", type=Path, help="Agent 组装 Manifest，默认读取包内 manifest.json")
    parser.add_argument("--codex-home", type=Path, help="启用 Codex L0 健康检查的专用本地配置目录")
    parser.add_argument("--codex-bin", type=Path, help="已验证版本的本机 Codex 可执行文件")
    parser.add_argument("--claude-bin", type=Path, help="启用 L0 健康检查的专用 Claude Code 原生可执行文件")
    parser.add_argument("--claude-model", help="设备所有者选定的 Claude 模型 ID")
    parser.add_argument("--claude-loopback-fixture-port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--service-config", type=Path, help="工作区外设备本地登记的应用服务配置")
    parser.add_argument("--build-archive", type=Path, help="工作区外设备所有者已准备的 Python 源码 ZIP")
    return parser.parse_args()


async def async_main(default_config: Path | None = None) -> None:
    args = parse_args(default_config)
    manifest = load_manifest(args.manifest)
    config_store = (
        EnvironmentAgentConfig(args.config_env)
        if args.config_env
        else AgentConfig(args.config)
    )
    enrollment_token = args.token.strip() if args.token else None
    saved = config_store.prepare_startup(enrollment_token)
    gateway_url = args.gateway or (saved or {}).get("gateway_url")
    if not gateway_url:
        raise FatalAgentError("需要通过 --gateway 指定网关 WebSocket 地址")
    workdir = Path(args.workdir).expanduser().resolve()
    if not workdir.is_dir():
        raise FatalAgentError(f"工作目录不存在: {workdir}")
    handlers = build_profile_handlers(
        manifest.profiles, workdir, service_config=args.service_config, build_archive=args.build_archive,
    )
    runtimes: dict[str, LocalAgentRuntime] = {}
    if args.codex_home:
        if HEALTH_PROFILE not in handlers:
            raise FatalAgentError("启用 Codex 健康检查需要装配 system_observe.v1")
        try:
            runtimes["codex"] = CodexRuntime.from_installation(
                args.codex_home, args.codex_bin, config_env=args.config_env,
            )
        except ValueError as exc:
            raise FatalAgentError(str(exc)) from exc
    elif args.codex_bin:
        raise FatalAgentError("--codex-bin 需要同时指定 --codex-home")
    if args.claude_bin or args.claude_model or args.claude_loopback_fixture_port is not None:
        if HEALTH_PROFILE not in handlers or not args.claude_bin or not args.claude_model:
            raise FatalAgentError("启用 Claude 健康检查需要 system_observe.v1、--claude-bin 和 --claude-model")
        try:
            runtimes["claude"] = ClaudeRuntime.from_installation(
                args.claude_bin, args.claude_model, workdir=workdir, config_env=args.config_env,
                fixture_port=args.claude_loopback_fixture_port,
            )
        except ValueError as exc:
            raise FatalAgentError(str(exc)) from exc
    warning = privileged_shell_warning(manifest.profiles, workdir)
    if warning:
        print(warning, file=sys.stderr, flush=True)
    agent = RemoteAgent(
        gateway_url=gateway_url,
        config_store=config_store,
        enrollment_token=enrollment_token,
        name=(args.name.strip() if args.name else manifest.name) or socket.gethostname(),
        description=args.description.strip() if args.description is not None else manifest.description,
        build_id=manifest.build_id,
        handlers=handlers,
        runtimes=runtimes,
    )
    await agent.run_forever()


def main(default_config: Path | None = None) -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    try:
        asyncio.run(async_main(default_config))
    except FatalAgentError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        pass
