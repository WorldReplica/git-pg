from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from git_pg.config import Settings
from git_pg.db.engine import create_engine, session_scope
from git_pg.db.orm.models import Repository
from git_pg.models.repo import RefName
from git_pg.store.postgres import PostgresGitStore
from git_pg.sync import push_and_ingest
from git_pg.warm_cache import WarmCacheManager

_seed_lock = asyncio.Lock()
_seeded_ok: set[str] = set()


def _write_fixture(root: Path) -> None:
    (root / "README.md").write_text("# Demo repo\n\nSeeded for git-pg demo-web.\n")
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "sample.json").write_text('{"hello": "world"}\n')
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "notes.md").write_text("Initial notes.\n")
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "seed@git-pg.local"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "git-pg seed"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "seed demo"],
        check=True,
    )


async def _demo_ready(eng: AsyncEngine, repo_name: str) -> bool:
    async with session_scope(eng) as session:
        existing = await session.execute(
            select(Repository).where(Repository.name == repo_name)
        )
        if existing.scalar_one_or_none() is None:
            return False
        store = PostgresGitStore(session)
        repo = await store.get_repo(repo_name)
        if repo is None:
            return False
        main = await store.get_ref_oid(repo.id, RefName(value="main"))
        return main is not None


async def ensure_demo_repo(
    settings: Settings,
    repo_name: str,
    *,
    engine: AsyncEngine | None = None,
) -> bool:
    """Ensure demo repo + main exist. Returns True if a seed push ran."""
    if repo_name in _seeded_ok:
        return False

    owns_engine = engine is None
    eng = engine or create_engine(settings)
    try:
        if await _demo_ready(eng, repo_name):
            _seeded_ok.add(repo_name)
            return False

        async with _seed_lock:
            if repo_name in _seeded_ok:
                return False
            if await _demo_ready(eng, repo_name):
                _seeded_ok.add(repo_name)
                return False

            with tempfile.TemporaryDirectory(prefix="git-pg-demo-seed-") as tmp:
                root = Path(tmp) / "repo"
                root.mkdir()
                await asyncio.to_thread(_write_fixture, root)
                try:
                    async with session_scope(eng) as session:
                        await push_and_ingest(
                            session,
                            repo_name,
                            root,
                            RefName(value="main"),
                            allow_main=True,
                            project_versions=True,
                        )
                except IntegrityError:
                    # Concurrent seed raced on repositories.name — treat as ready.
                    if await _demo_ready(eng, repo_name):
                        _seeded_ok.add(repo_name)
                        return False
                    raise
                warm = WarmCacheManager(settings)
                async with session_scope(eng) as session:
                    store = PostgresGitStore(session)
                    repo = await store.get_repo(repo_name)
                    if repo is not None:
                        await warm.refresh_repo(store, repo.id, repo.name)
            _seeded_ok.add(repo_name)
            return True
    finally:
        if owns_engine:
            await eng.dispose()
