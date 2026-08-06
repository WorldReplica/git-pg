from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import WebSocket
from pydantic import TypeAdapter

from api.schemas import (
    FileCommentOut,
    WsAgentUpdated,
    WsCommentCreated,
    WsEvent,
    WsFileVersionsCreated,
    WsMainUpdated,
)
from git_pg.agents import EventSink
from git_pg.file_versions import ProjectedVersion

_EVENT_ADAPTER: TypeAdapter[WsEvent] = TypeAdapter(WsEvent)


class WsHub(EventSink):
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, event: WsEvent) -> None:
        payload = _EVENT_ADAPTER.dump_json(event)
        async with self._lock:
            clients = list(self._clients)
        dead: list[WebSocket] = []
        for ws in clients:
            try:
                await ws.send_text(payload.decode())
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)

    async def agent_updated(self, run_id: UUID) -> None:
        await self.broadcast(WsAgentUpdated(run_id=run_id))

    async def main_updated(self) -> None:
        await self.broadcast(WsMainUpdated())

    async def file_versions_created(self, versions: list[ProjectedVersion]) -> None:
        await self.broadcast(
            WsFileVersionsCreated(
                version_ids=[v.id for v in versions],
                paths=[v.path for v in versions],
            )
        )

    async def comment_created(self, comment: FileCommentOut) -> None:
        await self.broadcast(WsCommentCreated(comment=comment))
