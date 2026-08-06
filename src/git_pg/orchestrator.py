from __future__ import annotations

from pathlib import Path

from git_pg.agent_runner import AgentRunConfig, run_agent_turn
from git_pg.config import Settings
from git_pg.db.engine import create_engine, session_scope
from git_pg.migrate import apply_migrations, downgrade_migrations
from git_pg.models.repo import (
    MigrateApplyResult,
    RefName,
    SessionHandle,
    SessionStartRequest,
)
from git_pg.seed import SeedOptions, SeedResult, seed_repo
from git_pg.session import SessionManager
from git_pg.sync import push_and_ingest
from git_pg.warm_cache import WarmCacheManager


class Orchestrator:
    def __init__(
        self,
        database_url: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        cfg = settings or Settings()
        if database_url is not None:
            cfg = cfg.model_copy(update={"database_url": database_url})
        settings = cfg
        self._engine = create_engine(settings)
        self._settings = settings
        self._sessions = SessionManager(self._engine, settings)
        self._warm_cache = WarmCacheManager(settings)

    async def session_start(self, request: SessionStartRequest) -> SessionHandle:
        return await self._sessions.start(request)

    def session_stop(self, session_id: str) -> None:
        self._sessions.stop(session_id)

    async def session_push(
        self,
        repo: str,
        local_path: Path,
        ref: str = "main",
    ) -> None:
        async with session_scope(self._engine) as session:
            await push_and_ingest(
                session,
                repo,
                local_path,
                RefName(value=ref),
            )
        await self._refresh_warm_cache(repo)

    async def migrate_apply(
        self,
        repo: str,
        alembic_ini: Path,
        ref: str = "main",
        target_revision: str | None = None,
    ) -> MigrateApplyResult | None:
        async with session_scope(self._engine) as session:
            return await apply_migrations(
                session,
                repo,
                RefName(value=ref),
                alembic_ini,
                target_revision=target_revision,
                settings=self._settings,
            )

    async def migrate_downgrade(
        self,
        repo: str,
        alembic_ini: Path,
        target_revision: str,
        ref: str = "main",
    ) -> MigrateApplyResult | None:
        async with session_scope(self._engine) as session:
            return await downgrade_migrations(
                session,
                repo,
                RefName(value=ref),
                alembic_ini,
                target_revision=target_revision,
                settings=self._settings,
            )

    async def seed(self, options: SeedOptions) -> SeedResult:
        result = await seed_repo(self._engine, options)
        await self._refresh_warm_cache(options.repo)
        return result

    async def run_agent(self, config: AgentRunConfig) -> str:
        return await run_agent_turn(config)

    async def _refresh_warm_cache(self, repo_name: str) -> None:
        async with session_scope(self._engine) as session:
            from git_pg.store.postgres import PostgresGitStore

            store = PostgresGitStore(session)
            repo = await store.get_repo(repo_name)
            if repo is None:
                return
            await self._warm_cache.refresh_repo(store, repo.id, repo.name)
