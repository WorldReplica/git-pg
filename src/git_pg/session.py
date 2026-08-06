from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from git_pg.config import Settings, get_settings
from git_pg.db.engine import session_scope
from git_pg.models.repo import RefName, RepoName, SessionHandle, SessionStartRequest
from git_pg.store.postgres import PostgresGitStore
from git_pg.warm_cache import WarmCacheManager


class SessionManager:
    def __init__(
        self,
        engine: AsyncEngine,
        settings: Settings | None = None,
    ) -> None:
        self._engine = engine
        self._settings = settings or get_settings()
        self._warm_cache = WarmCacheManager(self._settings)

    async def start(self, request: SessionStartRequest) -> SessionHandle:
        session_id = request.session_id or uuid.uuid4().hex[:12]
        sessions_root = Path(self._settings.sessions_root)
        dest = sessions_root / session_id / request.repo
        if dest.exists():
            shutil.rmtree(dest)

        t0 = time.perf_counter()
        async with session_scope(self._engine) as session:
            store = PostgresGitStore(session)
            repo = await store.get_repo(request.repo)
            if repo is None:
                msg = f"repo {request.repo!r} not found in Postgres"
                raise LookupError(msg)
            ref = RefName(value=request.ref)
            await self._warm_cache.export_session(
                store,
                repo.id,
                repo.name,
                ref,
                dest,
            )

        elapsed_ms = (time.perf_counter() - t0) * 1000
        return SessionHandle(
            session_id=session_id,
            repo=RepoName(value=request.repo),
            ref=RefName(value=request.ref),
            cwd=str(dest),
            spin_up_ms=elapsed_ms,
        )

    def stop(self, session_id: str) -> None:
        path = Path(self._settings.sessions_root) / session_id
        if path.exists():
            shutil.rmtree(path)
