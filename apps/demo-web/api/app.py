from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from api.hub import WsHub
from api.routes import router
from api.seed import ensure_demo_repo
from api.settings import ApiSettings
from git_pg.agents import AgentService
from git_pg.config import Settings
from git_pg.db.engine import create_engine


def _to_git_settings(api: ApiSettings) -> Settings:
    return Settings(
        database_url=api.database_url,
        sessions_root=api.sessions_root,
        warm_cache_root=api.warm_cache_root,
        warm_cache_enabled=api.warm_cache_enabled,
        agent_docker_image=api.agent_docker_image,
        compose_project_name=api.compose_project_name,
        compose_project_dir=api.compose_project_dir,
        anthropic_api_key=api.anthropic_api_key,
        task_gen_model=api.task_gen_model,
    )


def create_app(api_settings: ApiSettings | None = None) -> FastAPI:
    settings = api_settings or ApiSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        git_settings = _to_git_settings(settings)
        engine = create_engine(git_settings)
        hub = WsHub()
        agents = AgentService(engine, git_settings, event_sink=hub)
        app.state.engine = engine
        app.state.api_settings = settings
        app.state.hub = hub
        app.state.agents = agents
        await ensure_demo_repo(git_settings, settings.demo_repo)
        yield
        await engine.dispose()

    app = FastAPI(title="git-pg demo-web", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    @app.websocket("/api/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        hub: WsHub = ws.app.state.hub
        await hub.connect(ws)
        try:
            while True:
                # Client may send pings; we ignore payloads (server-push only).
                await ws.receive_text()
        except WebSocketDisconnect:
            await hub.disconnect(ws)

    return app


app = create_app()
