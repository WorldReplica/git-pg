from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from api.hub import WsHub
from api.schemas import (
    AgentCommitOut,
    AgentCommitsResponse,
    AgentDiffResponse,
    AgentRunList,
    AgentRunOut,
    AgentRunStatusOut,
    AgentSpawnRequest,
    AgentSpawnResponse,
    ApproveRequest,
    ApproveResponse,
    ChangedPath,
    DeleteResponse,
    FileCommentCreate,
    FileCommentList,
    FileCommentOut,
    FileVersionList,
    FileVersionOut,
    RebaseRequest,
    RebaseResponse,
    RejectResponse,
    TreeEntry,
    TreeResponse,
)
from api.seed import ensure_demo_repo
from api.settings import ApiSettings
from git_pg.agents import AgentService, RebaseStrategy
from git_pg.config import Settings
from git_pg.db.engine import session_scope
from git_pg.db.orm.models import AgentRun, FileComment, FileVersion
from git_pg.file_versions import map_tree_blobs
from git_pg.models.repo import RefName
from git_pg.store.postgres import PostgresGitStore, _parse_commit_links

router = APIRouter(prefix="/api")


def get_engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


def get_api_settings(request: Request) -> ApiSettings:
    return cast(ApiSettings, request.app.state.api_settings)


def get_hub(request: Request) -> WsHub:
    return cast(WsHub, request.app.state.hub)


def get_agents(request: Request) -> AgentService:
    return cast(AgentService, request.app.state.agents)


EngineDep = Annotated[AsyncEngine, Depends(get_engine)]
SettingsDep = Annotated[ApiSettings, Depends(get_api_settings)]
HubDep = Annotated[WsHub, Depends(get_hub)]
AgentsDep = Annotated[AgentService, Depends(get_agents)]


def _git_settings(api: ApiSettings) -> Settings:
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


async def _ensure_demo(engine: AsyncEngine, api: ApiSettings) -> None:
    await ensure_demo_repo(_git_settings(api), api.demo_repo, engine=engine)


def _run_out(run: AgentRun, *, base_stale: bool = False) -> AgentRunOut:
    return AgentRunOut(
        id=run.id,
        branch=run.branch,
        status=AgentRunStatusOut(run.status),
        base_commit=run.base_commit.hex(),
        head_commit=run.head_commit.hex() if run.head_commit else None,
        prompt=run.prompt,
        summary=run.summary,
        created_at=run.created_at,
        finished_at=run.finished_at,
        base_stale=base_stale,
    )


def _version_out(row: FileVersion) -> FileVersionOut:
    return FileVersionOut(
        id=row.id,
        path=row.path,
        blob_oid=row.blob_oid.hex(),
        commit_oid=row.commit_oid.hex(),
        size=row.size,
        content_type=row.content_type,
        created_at=row.created_at,
    )


@router.get("/tree", response_model=TreeResponse)
async def get_tree(
    engine: EngineDep,
    api_settings: SettingsDep,
    ref: str = Query(default="main"),
) -> TreeResponse:
    await _ensure_demo(engine, api_settings)
    async with session_scope(engine) as session:
        store = PostgresGitStore(session)
        repo = await store.get_repo(api_settings.demo_repo)
        if repo is None:
            raise HTTPException(status_code=404, detail="repo not found")
        tip = await store.get_ref_oid(repo.id, RefName(value=ref))
        if tip is None:
            raise HTTPException(status_code=404, detail="ref not found")
        content = await store.get_object(repo.id, tip)
        if content is None:
            raise HTTPException(status_code=404, detail="commit missing")
        _, tree = _parse_commit_links(content)
        blobs = await map_tree_blobs(store, repo.id, tree)
        return TreeResponse(
            ref=ref,
            commit_oid=tip.hex(),
            entries=[
                TreeEntry(path=b.path, size=b.size, content_type=b.content_type)
                for b in blobs
            ],
        )


@router.get("/files/{path:path}/versions", response_model=FileVersionList)
async def list_versions(
    path: str,
    engine: EngineDep,
    api_settings: SettingsDep,
) -> FileVersionList:
    async with session_scope(engine) as session:
        store = PostgresGitStore(session)
        repo = await store.get_repo(api_settings.demo_repo)
        if repo is None:
            raise HTTPException(status_code=404, detail="repo not found")
        result = await session.execute(
            select(FileVersion)
            .where(FileVersion.repo_id == repo.id, FileVersion.path == path)
            .order_by(FileVersion.created_at.desc())
        )
        rows = list(result.scalars().all())
        return FileVersionList(path=path, versions=[_version_out(r) for r in rows])


@router.get("/file-versions/{version_id}/content")
async def version_content(version_id: uuid.UUID, engine: EngineDep) -> Response:
    async with session_scope(engine) as session:
        result = await session.execute(
            select(FileVersion).where(FileVersion.id == version_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="version not found")
        store = PostgresGitStore(session)
        blob = await store.get_object(row.repo_id, row.blob_oid)
        if blob is None:
            raise HTTPException(status_code=404, detail="blob missing")
        media = row.content_type or "application/octet-stream"
        return Response(content=blob, media_type=media)


@router.get("/file-versions/{version_id}/comments", response_model=FileCommentList)
async def list_comments(version_id: uuid.UUID, engine: EngineDep) -> FileCommentList:
    async with session_scope(engine) as session:
        ver = await session.execute(
            select(FileVersion).where(FileVersion.id == version_id)
        )
        if ver.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="version not found")
        result = await session.execute(
            select(FileComment)
            .where(FileComment.file_version_id == version_id)
            .order_by(FileComment.created_at.asc())
        )
        rows = list(result.scalars().all())
        return FileCommentList(
            file_version_id=version_id,
            comments=[
                FileCommentOut(
                    id=c.id,
                    file_version_id=c.file_version_id,
                    body=c.body,
                    author=c.author,
                    created_at=c.created_at,
                )
                for c in rows
            ],
        )


@router.post("/file-versions/{version_id}/comments", response_model=FileCommentOut)
async def add_comment(
    version_id: uuid.UUID,
    body: FileCommentCreate,
    engine: EngineDep,
    hub: HubDep,
) -> FileCommentOut:
    async with session_scope(engine) as session:
        ver = await session.execute(
            select(FileVersion).where(FileVersion.id == version_id)
        )
        if ver.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="version not found")
        row = FileComment(
            id=uuid.uuid4(),
            file_version_id=version_id,
            body=body.body,
            author=body.author,
        )
        session.add(row)
        await session.flush()
        out = FileCommentOut(
            id=row.id,
            file_version_id=row.file_version_id,
            body=row.body,
            author=row.author,
            created_at=row.created_at,
        )
    await hub.comment_created(out)
    return out


@router.get("/agents", response_model=AgentRunList)
async def list_agents(
    engine: EngineDep,
    api_settings: SettingsDep,
) -> AgentRunList:
    await _ensure_demo(engine, api_settings)
    async with session_scope(engine) as session:
        store = PostgresGitStore(session)
        repo = await store.get_repo(api_settings.demo_repo)
        if repo is None:
            return AgentRunList(runs=[])
        main_oid = await store.get_ref_oid(repo.id, RefName(value="main"))
        result = await session.execute(
            select(AgentRun)
            .where(AgentRun.repo_id == repo.id)
            .order_by(AgentRun.created_at.desc())
        )
        rows = list(result.scalars().all())
        return AgentRunList(
            runs=[
                _run_out(
                    r,
                    base_stale=(
                        r.status == AgentRunStatusOut.AWAITING_APPROVAL.value
                        and main_oid is not None
                        and r.base_commit != main_oid
                    ),
                )
                for r in rows
            ]
        )


@router.post("/agents", response_model=AgentSpawnResponse)
async def spawn_agent(
    body: AgentSpawnRequest,
    engine: EngineDep,
    api_settings: SettingsDep,
    agents: AgentsDep,
    hub: HubDep,
) -> AgentSpawnResponse:
    await _ensure_demo(engine, api_settings)
    prompt = body.prompt.strip() if body.prompt and body.prompt.strip() else None
    try:
        spawned = await agents.enqueue_spawn(
            repo_name=api_settings.demo_repo,
            prompt=prompt,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    asyncio.create_task(
        agents.drive_spawn(
            spawned.run_id,
            repo_name=api_settings.demo_repo,
            prompt=prompt,
        )
    )

    async with session_scope(engine) as session:
        result = await session.execute(
            select(AgentRun).where(AgentRun.id == spawned.run_id)
        )
        run = result.scalar_one()
        out = _run_out(run)
    await hub.agent_updated(spawned.run_id)
    return AgentSpawnResponse(run=out)


@router.get("/agents/{run_id}", response_model=AgentRunOut)
async def get_agent(run_id: uuid.UUID, engine: EngineDep) -> AgentRunOut:
    async with session_scope(engine) as session:
        result = await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        run = result.scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return _run_out(run)


@router.get("/agents/{run_id}/diff", response_model=AgentDiffResponse)
async def agent_diff(run_id: uuid.UUID, engine: EngineDep) -> AgentDiffResponse:
    async with session_scope(engine) as session:
        result = await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        run = result.scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        store = PostgresGitStore(session)
        base_tree = await _tree_or_404(store, run.repo_id, run.base_commit)
        base_blobs = {
            b.path: b.blob_oid
            for b in await map_tree_blobs(store, run.repo_id, base_tree)
        }
        if run.head_commit is None:
            # Failed/running/rejected with no tip — don't imply mass deletes.
            return AgentDiffResponse(
                run_id=run.id,
                base_commit=run.base_commit.hex(),
                head_commit=None,
                changed_paths=[],
            )
        head_tree = await _tree_or_404(store, run.repo_id, run.head_commit)
        head_blobs = {
            b.path: b.blob_oid
            for b in await map_tree_blobs(store, run.repo_id, head_tree)
        }
        paths = sorted(set(base_blobs) | set(head_blobs))
        changed: list[ChangedPath] = []
        for path in paths:
            old = base_blobs.get(path)
            new = head_blobs.get(path)
            if old is None and new is not None:
                changed.append(ChangedPath(path=path, change="added"))
            elif old is not None and new is None:
                changed.append(ChangedPath(path=path, change="deleted"))
            elif old != new:
                changed.append(ChangedPath(path=path, change="modified"))
        return AgentDiffResponse(
            run_id=run.id,
            base_commit=run.base_commit.hex(),
            head_commit=run.head_commit.hex(),
            changed_paths=changed,
        )


@router.get("/agents/{run_id}/commits", response_model=AgentCommitsResponse)
async def agent_commits(run_id: uuid.UUID, engine: EngineDep) -> AgentCommitsResponse:
    async with session_scope(engine) as session:
        result = await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        run = result.scalar_one_or_none()
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        if run.head_commit is None:
            return AgentCommitsResponse(
                run_id=run.id,
                base_commit=run.base_commit.hex(),
                head_commit=None,
                commits=[],
            )
        store = PostgresGitStore(session)
        commits: list[AgentCommitOut] = []
        oid: bytes | None = run.head_commit
        # Newest first; stop at base (exclusive). Cap for safety.
        for _ in range(100):
            if oid is None or oid == run.base_commit:
                break
            content = await store.get_object(run.repo_id, oid)
            if content is None:
                break
            parents, _tree = _parse_commit_links(content)
            subject, author = _parse_commit_meta(content)
            commits.append(
                AgentCommitOut(oid=oid.hex(), subject=subject, author=author)
            )
            oid = parents[0] if parents else None
        return AgentCommitsResponse(
            run_id=run.id,
            base_commit=run.base_commit.hex(),
            head_commit=run.head_commit.hex(),
            commits=commits,
        )


@router.post("/agents/{run_id}/approve", response_model=ApproveResponse)
async def approve_agent(
    run_id: uuid.UUID,
    engine: EngineDep,
    agents: AgentsDep,
    body: ApproveRequest | None = None,
) -> ApproveResponse:
    request = body or ApproveRequest()
    strategy = RebaseStrategy(request.rebase_strategy.value)
    try:
        result = await agents.approve(run_id, rebase_strategy=strategy)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    async with session_scope(engine) as session:
        row = await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        run = row.scalar_one()
        return ApproveResponse(
            run=_run_out(run),
            projected_version_ids=[v.id for v in result.projected_versions],
            rebasing_run_ids=list(result.rebasing_run_ids),
        )


@router.post("/agents/{run_id}/reject", response_model=RejectResponse)
async def reject_agent(
    run_id: uuid.UUID,
    engine: EngineDep,
    agents: AgentsDep,
) -> RejectResponse:
    try:
        await agents.reject(run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    async with session_scope(engine) as session:
        result = await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        run = result.scalar_one()
        return RejectResponse(run=_run_out(run))


@router.post("/agents/{run_id}/rebase", response_model=RebaseResponse)
async def rebase_agent(
    run_id: uuid.UUID,
    engine: EngineDep,
    api_settings: SettingsDep,
    agents: AgentsDep,
    body: RebaseRequest | None = None,
) -> RebaseResponse:
    request = body or RebaseRequest()
    strategy = RebaseStrategy(request.rebase_strategy.value)
    try:
        await agents.request_rebase(
            run_id,
            repo_name=api_settings.demo_repo,
            rebase_strategy=strategy,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    async with session_scope(engine) as session:
        result = await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        run = result.scalar_one()
        return RebaseResponse(run=_run_out(run, base_stale=True))


@router.delete("/agents/{run_id}", response_model=DeleteResponse, status_code=200)
async def delete_agent(
    run_id: uuid.UUID,
    agents: AgentsDep,
) -> DeleteResponse:
    try:
        await agents.delete(run_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return DeleteResponse(run_id=run_id)


async def _tree_or_404(
    store: PostgresGitStore,
    repo_id: int,
    commit_oid: bytes,
) -> bytes:
    content = await store.get_object(repo_id, commit_oid)
    if content is None:
        raise HTTPException(status_code=404, detail="commit missing")
    _, tree = _parse_commit_links(content)
    return tree


def _parse_commit_meta(content: bytes) -> tuple[str, str | None]:
    """Return (subject, author_name) from a raw git commit object."""
    text = content.decode(errors="replace")
    author: str | None = None
    for line in text.split("\n"):
        if line.startswith("author "):
            # author Name <email> timestamp tz
            rest = line[len("author ") :]
            if " <" in rest:
                author = rest.split(" <", 1)[0].strip() or None
            break
    parts = text.split("\n\n", 1)
    if len(parts) < 2:
        return "(no message)", author
    subject = parts[1].strip().split("\n", 1)[0].strip() or "(no message)"
    return subject, author
