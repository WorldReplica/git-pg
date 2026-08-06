from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from git_pg.db.orm.models import FileVersion
from git_pg.git.objects import ObjectType, parse_tree_entries
from git_pg.store.postgres import PostgresGitStore, _is_tree_mode, _parse_commit_links


@dataclass(frozen=True)
class PathBlob:
    path: str
    blob_oid: bytes
    size: int
    content_type: str | None


@dataclass(frozen=True)
class ProjectedVersion:
    id: uuid.UUID
    path: str
    blob_oid: bytes
    commit_oid: bytes
    size: int
    content_type: str | None


async def map_tree_blobs(
    store: PostgresGitStore,
    repo_id: int,
    tree_oid: bytes,
    *,
    prefix: str = "",
) -> list[PathBlob]:
    content = await store.get_object(repo_id, tree_oid)
    if content is None:
        return []
    results: list[PathBlob] = []
    for mode, name, oid in parse_tree_entries(content):
        path = name if not prefix else f"{prefix}/{name}"
        if path.startswith(".git/") or path == ".git":
            continue
        if _is_tree_mode(mode):
            results.extend(await map_tree_blobs(store, repo_id, oid, prefix=path))
            continue
        blob = await store.get_object(repo_id, oid)
        size = len(blob) if blob is not None else 0
        guessed, _ = mimetypes.guess_type(path)
        results.append(
            PathBlob(
                path=path,
                blob_oid=oid,
                size=size,
                content_type=guessed,
            )
        )
    return results


async def project_main_tip_versions(
    session: AsyncSession,
    store: PostgresGitStore,
    repo_id: int,
    *,
    previous_commit_oid: bytes | None,
    new_commit_oid: bytes,
) -> list[ProjectedVersion]:
    """Insert file_versions for paths whose blob OID changed on main."""
    new_content = await store.get_object(repo_id, new_commit_oid)
    if new_content is None:
        return []
    _, new_tree = _parse_commit_links(new_content)
    new_blobs = {
        item.path: item for item in await map_tree_blobs(store, repo_id, new_tree)
    }

    old_by_path: dict[str, bytes] = {}
    if previous_commit_oid is not None:
        old_content = await store.get_object(repo_id, previous_commit_oid)
        if old_content is not None:
            _, old_tree = _parse_commit_links(old_content)
            for item in await map_tree_blobs(store, repo_id, old_tree):
                old_by_path[item.path] = item.blob_oid

    created: list[ProjectedVersion] = []
    for path, item in sorted(new_blobs.items()):
        prev = old_by_path.get(path)
        if prev == item.blob_oid:
            continue
        row = FileVersion(
            id=uuid.uuid4(),
            repo_id=repo_id,
            path=path,
            blob_oid=item.blob_oid,
            commit_oid=new_commit_oid,
            size=item.size,
            content_type=item.content_type,
        )
        session.add(row)
        created.append(
            ProjectedVersion(
                id=row.id,
                path=path,
                blob_oid=item.blob_oid,
                commit_oid=new_commit_oid,
                size=item.size,
                content_type=item.content_type,
            )
        )
    await session.flush()
    return created


async def tree_oid_for_commit(
    store: PostgresGitStore,
    repo_id: int,
    commit_oid: bytes,
) -> bytes | None:
    content = await store.get_object(repo_id, commit_oid)
    if content is None:
        obj_type = await store.get_object_type(repo_id, commit_oid)
        if obj_type != ObjectType.COMMIT:
            return None
        return None
    _, tree_oid = _parse_commit_links(content)
    return tree_oid
