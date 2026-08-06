from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from git_pg.db.orm.models import SpecialRule
from git_pg.models.repo import RefName
from git_pg.special.registry import HandlerRegistry, default_registry
from git_pg.store.postgres import PostgresGitStore, walk_tree_files


async def push_and_ingest(
    session: AsyncSession,
    repo_name: str,
    local_path: Path,
    ref: RefName,
    registry: HandlerRegistry | None = None,
) -> None:
    reg = registry or default_registry()
    store = PostgresGitStore(session)
    repo = await store.get_or_create_repo(repo_name)
    head = await store.push_from_local(repo.id, local_path)

    rules_result = await session.execute(
        select(SpecialRule).where(SpecialRule.repo_id == repo.id)
    )
    rules = list(rules_result.scalars().all())
    if not rules:
        return

    commit_oid = bytes.fromhex(head.hex)
    commit_content = await store.get_object(repo.id, commit_oid)
    if commit_content is None:
        return
    from git_pg.store.postgres import _parse_commit_links

    _, tree_oid = _parse_commit_links(commit_content)
    async for path, blob in walk_tree_files(store, repo.id, tree_oid):
        for rule in rules:
            if path == rule.path:
                await reg.ingest_path(session, repo.id, rule, blob)
