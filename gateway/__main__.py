from __future__ import annotations

import argparse

import uvicorn

from .config import Settings
from .database import Database


def main() -> None:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="Remote Agent Gateway")
    commands = parser.add_subparsers(dest="command")
    stop = commands.add_parser("execution-stop", help="本地运维：查看、停止或恢复此实例的远端执行")
    stop.add_argument("action", choices=("status", "pause", "resume"))
    args = parser.parse_args()
    if args.command == "execution-stop":
        if not settings.database_path.is_file():
            parser.error("网关数据库不存在；请确认 GATEWAY_DATABASE 与运行中的实例一致")
        db = Database(settings.database_path)
        db.initialize()
        if args.action != "status":
            db.set_execution_stop("global", stopped=args.action == "pause")
        active = any(item["scope"] == "global" for item in db.execution_stops())
        print("instance: stopped" if active else "instance: running")
        return
    uvicorn.run(
        "gateway.app:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        workers=1,
    )


if __name__ == "__main__":
    main()
