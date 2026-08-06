from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from git_pg.db.orm.models import SpecialRule
from git_pg.file_versions import ProjectedVersion, project_main_tip_versions
from git_pg.models.repo import GitOid, RefName
from git_pg.special.registry import HandlerRegistry, default_registry
from git_pg.store.postgres import PostgresGitStore, walk_tree_files


@dataclass(frozen=True)
class PushResult:
    head: GitOid
    projected_versions: tuple[ProjectedVersion, ...] = ()


async def push_and_ingest(
    session: AsyncSession,
    repo_name: str,
    local_path: Path,
    ref: RefName,
    registry: HandlerRegistry | None = None,
    *,
    allow_main: bool = False,
    project_versions: bool = False,
    require_fast_forward: bool = True,
) -> PushResult:
    reg = registry or default_registry()
    store = PostgresGitStore(session)
    repo = await store.get_or_create_repo(repo_name)

    previous_main: bytes | None = None
    if project_versions and ref.heads_name in {
        "refs/heads/main",
        "refs/heads/master",
    }:
        previous_main = await store.get_ref_oid(repo.id, ref)

    head = await store.push_from_local(
        repo.id,
        local_path,
        allow_main=allow_main,
        require_fast_forward=require_fast_forward,
    )

    projected: list[ProjectedVersion] = []
    if project_versions and ref.heads_name in {
        "refs/heads/main",
        "refs/heads/master",
    }:
        projected = await project_main_tip_versions(
            session,
            store,
            repo.id,
            previous_commit_oid=previous_main,
            new_commit_oid=bytes.fromhex(head.hex),
        )

    rules_result = await session.execute(
        select(SpecialRule).where(SpecialRule.repo_id == repo.id)
    )
    rules = list(rules_result.scalars().all())
    if not rules:
        return PushResult(head=head, projected_versions=tuple(projected))

    commit_oid = bytes.fromhex(head.hex)
    commit_content = await store.get_object(repo.id, commit_oid)
    if commit_content is None:
        return PushResult(head=head, projected_versions=tuple(projected))
    from git_pg.store.postgres import _parse_commit_links

    _, tree_oid = _parse_commit_links(commit_content)
    async for path, blob in walk_tree_files(store, repo.id, tree_oid):
        for rule in rules:
            if path == rule.path:
                await reg.ingest_path(session, repo.id, rule, blob)
    return PushResult(head=head, projected_versions=tuple(projected))
