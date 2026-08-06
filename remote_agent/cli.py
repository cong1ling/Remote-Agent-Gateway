from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
from pathlib import Path

from remote_agent.core import AgentConfig, FatalAgentError, RemoteAgent, load_manifest
from remote_agent.profiles import build_profile_handlers


def parse_args() -> argparse.Namespace:
    default_config = Path.home() / ".remote-agent" / "config.json"
    parser = argparse.ArgumentParser(description="Remote Agent Gateway 极简远程 Agent")
    parser.add_argument("--gateway", help="Agent WebSocket 地址，例如 ws://127.0.0.1:8000/ws/agent")
    parser.add_argument("--token", help="首次注册使用的一次性 Enrollment Token")
    parser.add_argument("--name", default=socket.gethostname(), help="Agent 显示名称")
    parser.add_argument("--description", default="", help="Agent 用途描述")
    parser.add_argument("--workdir", default=os.getcwd(), help="远程命令工作目录")
    parser.add_argument("--config", type=Path, default=default_config, help="设备凭据保存位置")
    parser.add_argument("--manifest", type=Path, help="Agent 组装 Manifest，默认读取包内 manifest.json")
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    manifest = load_manifest(args.manifest)
    config_store = AgentConfig(args.config)
    saved = config_store.load()
    gateway_url = args.gateway or (saved or {}).get("gateway_url")
    if not gateway_url:
        raise FatalAgentError("需要通过 --gateway 指定网关 WebSocket 地址")
    workdir = Path(args.workdir).expanduser().resolve()
    if not workdir.is_dir():
        raise FatalAgentError(f"工作目录不存在: {workdir}")
    handlers = build_profile_handlers(manifest.profiles, workdir)
    agent = RemoteAgent(
        gateway_url=gateway_url,
        config_store=config_store,
        enrollment_token=args.token,
        name=args.name.strip() or socket.gethostname(),
        description=args.description.strip(),
        handlers=handlers,
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

