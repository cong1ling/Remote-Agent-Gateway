from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .agent_api import handle_agent_websocket
from .agent_hub import AgentHub
from .config import Settings
from .database import Database
from .health_check_service import HealthCheckService
from .health_monitor import HealthMonitor
from .mcp_api import create_mcp_server
from .operation_plan_service import OperationPlanService
from .web_api import create_web_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.database_path)
    hub = AgentHub(db)
    health_checks = HealthCheckService(db, hub)
    health_monitor = HealthMonitor(db, hub, health_checks)
    operation_plans = OperationPlanService(db, hub)
    mcp_server = create_mcp_server(db, hub, settings, operation_plans)
    mcp_app = mcp_server.streamable_http_app()
    static_dir = Path(__file__).parent / "static"

    async def heartbeat_loop() -> None:
        while True:
            await asyncio.sleep(20)
            await hub.heartbeat(timeout=60)

    async def execution_stop_loop() -> None:
        while True:
            await asyncio.sleep(0.5)
            try:
                await hub.enforce_execution_stops()
            except sqlite3.Error:
                logging.getLogger(__name__).exception("无法读取停机控制状态")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db.initialize()
        db.recover_interrupted_tasks()
        db.defer_overdue_health_monitors(int(time.time()))
        async with mcp_app.router.lifespan_context(mcp_app):
            heartbeat_task = asyncio.create_task(heartbeat_loop())
            stop_task = asyncio.create_task(execution_stop_loop())
            monitor_task = asyncio.create_task(health_monitor.loop())
            try:
                yield
            finally:
                heartbeat_task.cancel()
                stop_task.cancel()
                monitor_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
                with suppress(asyncio.CancelledError):
                    await stop_task
                with suppress(asyncio.CancelledError):
                    await monitor_task
                await health_monitor.shutdown()
                await health_checks.shutdown()
                await operation_plans.shutdown()
                await hub.close_all()

    app = FastAPI(
        title="Remote Agent Gateway",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = db
    app.state.agent_hub = hub
    app.state.health_checks = health_checks
    app.state.operation_plans = operation_plans
    app.state.mcp_server = mcp_server
    app.include_router(create_web_router(db, settings, hub, health_checks, operation_plans, health_monitor))

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self' ws: wss:; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.websocket("/ws/agent")
    async def agent_socket(websocket: WebSocket) -> None:
        await handle_agent_websocket(websocket, db, hub)

    if static_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    app.mount("/mcp", mcp_app, name="mcp")
    return app


app = create_app()
