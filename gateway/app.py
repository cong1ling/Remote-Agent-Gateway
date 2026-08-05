from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agent_api import handle_agent_websocket
from .agent_hub import AgentHub
from .config import Settings
from .database import Database
from .mcp_api import create_mcp_server
from .web_api import create_web_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.database_path)
    hub = AgentHub()
    mcp_server = create_mcp_server(db, hub, settings)
    mcp_app = mcp_server.streamable_http_app()
    static_dir = Path(__file__).parent / "static"

    async def heartbeat_loop() -> None:
        while True:
            await asyncio.sleep(20)
            await hub.heartbeat(timeout=60)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db.initialize()
        async with mcp_app.router.lifespan_context(mcp_app):
            heartbeat_task = asyncio.create_task(heartbeat_loop())
            try:
                yield
            finally:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat_task
                await hub.close_all()

    app = FastAPI(
        title="Remote Agent Gateway",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.database = db
    app.state.agent_hub = hub
    app.state.mcp_server = mcp_server
    app.include_router(create_web_router(db, settings, hub))

    @app.websocket("/ws/agent")
    async def agent_socket(websocket: WebSocket) -> None:
        await handle_agent_websocket(websocket, db, hub)

    if static_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir), name="assets")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    app.mount("/mcp", mcp_app, name="mcp")
    return app


app = create_app()

