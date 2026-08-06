from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest
from api.app import create_app
from api.schemas import WsEvent
from api.settings import ApiSettings
from httpx import ASGITransport, AsyncClient
from pydantic import TypeAdapter
from sqlalchemy import delete, select

from git_pg.db.engine import session_scope
from git_pg.db.orm.models import (
    AgentRun,
    AgentRunStatus,
    FileComment,
    FileVersion,
    Repository,
)
from git_pg.models.repo import RefName
from git_pg.store.postgres import PostgresGitStore
from git_pg.sync import push_and_ingest

_WS_ADAPTER: TypeAdapter[WsEvent] = TypeAdapter(WsEvent)


def _init_repo(files: dict[str, str]) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="git-pg-fv-"))
    subprocess.run(["git", "init", "-b", "main", str(tmp)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp), "config", "user.email", "t@git-pg.local"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp), "config", "user.name", "git-pg"],
        check=True,
    )
    for rel, content in files.items():
        path = tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-m", "c1"], check=True)
    return tmp


@pytest.mark.integration
async def test_file_versions_and_comments_survive(engine, database_url: str) -> None:
    repo_name = "test-file-versions"
    root = Path(tempfile.mkdtemp(prefix="git-pg-sessions-"))
    settings = ApiSettings(
        database_url=database_url,
        sessions_root=str(root / "sessions"),
        warm_cache_root=str(root / "warm"),
        demo_repo=repo_name,
    )

    async with session_scope(engine) as session:
        await session.execute(delete(Repository).where(Repository.name == repo_name))
        await session.flush()

    local = _init_repo({"docs/a.md": "v1\n"})
    async with session_scope(engine) as session:
        push1 = await push_and_ingest(
            session,
            repo_name,
            local,
            RefName(value="main"),
            allow_main=True,
            project_versions=True,
        )
        assert len(push1.projected_versions) == 1
        v1_id = push1.projected_versions[0].id

    (local / "docs" / "a.md").write_text("v2\n")
    subprocess.run(["git", "-C", str(local), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(local), "commit", "-m", "c2"], check=True)

    async with session_scope(engine) as session:
        push2 = await push_and_ingest(
            session,
            repo_name,
            local,
            RefName(value="main"),
            allow_main=True,
            project_versions=True,
        )
        assert len(push2.projected_versions) == 1
        assert push2.projected_versions[0].path == "docs/a.md"

        session.add(
            FileComment(
                id=uuid.uuid4(),
                file_version_id=v1_id,
                body="styling was nicer",
                author="demo",
            )
        )
        await session.flush()

        before = await session.execute(select(FileVersion))
        count_before = len(list(before.scalars().all()))

    agent_dir = Path(tempfile.mkdtemp(prefix="git-pg-agent-branch-"))
    subprocess.run(["git", "clone", str(local), str(agent_dir)], check=True)
    subprocess.run(
        ["git", "-C", str(agent_dir), "checkout", "-b", f"agent/{uuid.uuid4().hex}"],
        check=True,
    )
    (agent_dir / "docs" / "a.md").write_text("agent\n")
    subprocess.run(["git", "-C", str(agent_dir), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(agent_dir), "commit", "-m", "agent edit"],
        check=True,
    )
    branch = subprocess.run(
        ["git", "-C", str(agent_dir), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    async with session_scope(engine) as session:
        await push_and_ingest(
            session,
            repo_name,
            agent_dir,
            RefName(value=branch),
            allow_main=False,
            project_versions=False,
        )
        after = await session.execute(select(FileVersion))
        count_after = len(list(after.scalars().all()))
        assert count_after == count_before

        comments = await session.execute(
            select(FileComment).where(FileComment.file_version_id == v1_id)
        )
        assert comments.scalar_one().body == "styling was nicer"

    subprocess.run(
        ["git", "-C", str(agent_dir), "checkout", "main"],
        check=True,
    )
    async with session_scope(engine) as session:
        with pytest.raises(PermissionError):
            await push_and_ingest(
                session,
                repo_name,
                agent_dir,
                RefName(value="main"),
                allow_main=False,
            )

    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        res = await client.get("/api/tree")
        assert res.status_code == 200
        versions = await client.get("/api/files/docs/a.md/versions")
        assert versions.status_code == 200
        body = versions.json()
        assert len(body["versions"]) >= 2


@pytest.mark.integration
async def test_main_protect_blocks_agent_ref_named_main(engine) -> None:
    repo_name = "test-main-protect"
    async with session_scope(engine) as session:
        await session.execute(delete(Repository).where(Repository.name == repo_name))
        await session.flush()
        local = _init_repo({"x.txt": "1\n"})
        await push_and_ingest(
            session,
            repo_name,
            local,
            RefName(value="main"),
            allow_main=True,
        )
        with pytest.raises(PermissionError):
            await push_and_ingest(
                session,
                repo_name,
                local,
                RefName(value="main"),
                allow_main=False,
            )


@pytest.mark.integration
async def test_approve_projects_versions_and_broadcasts_ws(
    engine, database_url: str
) -> None:
    repo_name = "test-approve-ws"
    root = Path(tempfile.mkdtemp(prefix="git-pg-sessions-"))
    settings = ApiSettings(
        database_url=database_url,
        sessions_root=str(root / "sessions"),
        warm_cache_root=str(root / "warm"),
        demo_repo=repo_name,
    )

    async with session_scope(engine) as session:
        await session.execute(delete(Repository).where(Repository.name == repo_name))
        await session.flush()

    local = _init_repo({"docs/hello.md": "base\n"})
    async with session_scope(engine) as session:
        await push_and_ingest(
            session,
            repo_name,
            local,
            RefName(value="main"),
            allow_main=True,
            project_versions=True,
        )
        store = PostgresGitStore(session)
        repo = await store.get_or_create_repo(repo_name)
        main_oid = await store.get_ref_oid(repo.id, RefName(value="main"))
        assert main_oid is not None
        before = await session.execute(select(FileVersion))
        count_before = len(list(before.scalars().all()))

    run_id = uuid.uuid4()
    branch = f"agent/{run_id.hex}"
    agent_dir = Path(tempfile.mkdtemp(prefix="git-pg-approve-agent-"))
    subprocess.run(["git", "clone", str(local), str(agent_dir)], check=True)
    subprocess.run(
        ["git", "-C", str(agent_dir), "checkout", "-b", branch],
        check=True,
    )
    (agent_dir / "docs" / "hello.md").write_text("from-agent\n")
    subprocess.run(["git", "-C", str(agent_dir), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(agent_dir), "commit", "-m", "agent change"],
        check=True,
    )

    async with session_scope(engine) as session:
        push = await push_and_ingest(
            session,
            repo_name,
            agent_dir,
            RefName(value=branch),
            allow_main=False,
            project_versions=False,
        )
        after_agent = await session.execute(select(FileVersion))
        assert len(list(after_agent.scalars().all())) == count_before

        store = PostgresGitStore(session)
        repo = await store.get_or_create_repo(repo_name)
        session.add(
            AgentRun(
                id=run_id,
                repo_id=repo.id,
                branch=branch,
                status=AgentRunStatus.AWAITING_APPROVAL.value,
                base_commit=main_oid,
                head_commit=bytes.fromhex(push.head.hex),
                summary="test agent",
            )
        )
        await session.flush()

    app = create_app(settings)
    transport = ASGITransport(app=app)
    captured: list[WsEvent] = []

    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        hub = app.state.hub
        real_broadcast = hub.broadcast

        async def capture_broadcast(event: WsEvent) -> None:
            captured.append(event)
            await real_broadcast(event)

        hub.broadcast = capture_broadcast  # type: ignore[method-assign]

        res = await client.post(
            f"/api/agents/{run_id}/approve",
            json={"rebase_strategy": "auto"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["run"]["status"] == "approved"
        assert len(body["projected_version_ids"]) >= 1
        assert body["rebasing_run_ids"] == []

        types = {event.type for event in captured}
        assert "agent.updated" in types
        assert "main.updated" in types
        assert "file_versions.created" in types

        # Round-trip: events are valid WsEvent envelopes
        for event in captured:
            _WS_ADAPTER.validate_python(json.loads(_WS_ADAPTER.dump_json(event)))

        versions = await client.get("/api/files/docs/hello.md/versions")
        assert versions.status_code == 200
        assert len(versions.json()["versions"]) == count_before + 1


@pytest.mark.integration
async def test_approve_fans_out_auto_rebase(engine, database_url: str) -> None:
    repo_name = "test-approve-fanout"
    root = Path(tempfile.mkdtemp(prefix="git-pg-sessions-"))
    settings = ApiSettings(
        database_url=database_url,
        sessions_root=str(root / "sessions"),
        warm_cache_root=str(root / "warm"),
        demo_repo=repo_name,
    )

    async with session_scope(engine) as session:
        await session.execute(delete(Repository).where(Repository.name == repo_name))
        await session.flush()

    local = _init_repo({"docs/hello.md": "base\n"})
    async with session_scope(engine) as session:
        await push_and_ingest(
            session,
            repo_name,
            local,
            RefName(value="main"),
            allow_main=True,
            project_versions=True,
        )
        store = PostgresGitStore(session)
        repo = await store.get_or_create_repo(repo_name)
        main_oid = await store.get_ref_oid(repo.id, RefName(value="main"))
        assert main_oid is not None

    async def _push_agent(path: str, content: str) -> tuple[uuid.UUID, str]:
        run_id = uuid.uuid4()
        branch = f"agent/{run_id.hex}"
        agent_dir = Path(tempfile.mkdtemp(prefix="git-pg-fanout-"))
        subprocess.run(["git", "clone", str(local), str(agent_dir)], check=True)
        subprocess.run(
            ["git", "-C", str(agent_dir), "checkout", "-b", branch],
            check=True,
        )
        file_path = agent_dir / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        subprocess.run(["git", "-C", str(agent_dir), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(agent_dir), "commit", "-m", f"edit {path}"],
            check=True,
        )
        async with session_scope(engine) as session:
            push = await push_and_ingest(
                session,
                repo_name,
                agent_dir,
                RefName(value=branch),
                allow_main=False,
            )
            store = PostgresGitStore(session)
            repo = await store.get_or_create_repo(repo_name)
            session.add(
                AgentRun(
                    id=run_id,
                    repo_id=repo.id,
                    branch=branch,
                    status=AgentRunStatus.AWAITING_APPROVAL.value,
                    base_commit=main_oid,
                    head_commit=bytes.fromhex(push.head.hex),
                    summary="fanout agent",
                )
            )
            await session.flush()
        return run_id, branch

    run_a, _ = await _push_agent("docs/a.md", "agent-a\n")
    run_b, _ = await _push_agent("docs/b.md", "agent-b\n")

    app = create_app(settings)
    transport = ASGITransport(app=app)
    captured: list[WsEvent] = []

    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        hub = app.state.hub
        real_broadcast = hub.broadcast

        async def capture_broadcast(event: WsEvent) -> None:
            captured.append(event)
            await real_broadcast(event)

        hub.broadcast = capture_broadcast  # type: ignore[method-assign]

        res = await client.post(
            f"/api/agents/{run_a}/approve",
            json={"rebase_strategy": "auto"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["run"]["status"] == "approved"
        assert body["rebasing_run_ids"] == [str(run_b)]

        # Fan-out marks B rebasing immediately; background task finishes shortly.
        for _ in range(50):
            agents = await client.get("/api/agents")
            assert agents.status_code == 200
            by_id = {r["id"]: r for r in agents.json()["runs"]}
            status_b = by_id[str(run_b)]["status"]
            if status_b in {"awaiting_approval", "failed"}:
                break
            await asyncio.sleep(0.1)
        else:
            raise AssertionError(f"rebase did not finish, status={status_b}")

        assert status_b == "awaiting_approval", by_id[str(run_b)].get("summary")
        assert by_id[str(run_b)]["base_commit"] != main_oid.hex()

        updated_ids = {
            str(e.run_id)
            for e in captured
            if e.type == "agent.updated"  # type: ignore[union-attr]
        }
        assert str(run_a) in updated_ids
        assert str(run_b) in updated_ids


@pytest.mark.integration
async def test_delete_agent_allows_non_approved_blocks_approved(
    engine, database_url: str
) -> None:
    repo_name = "test-delete-agent"
    root = Path(tempfile.mkdtemp(prefix="git-pg-sessions-"))
    settings = ApiSettings(
        database_url=database_url,
        sessions_root=str(root / "sessions"),
        warm_cache_root=str(root / "warm"),
        demo_repo=repo_name,
    )

    async with session_scope(engine) as session:
        await session.execute(delete(Repository).where(Repository.name == repo_name))
        await session.flush()

    local = _init_repo({"README.md": "hi\n"})
    async with session_scope(engine) as session:
        await push_and_ingest(
            session,
            repo_name,
            local,
            RefName(value="main"),
            allow_main=True,
            project_versions=True,
        )
        store = PostgresGitStore(session)
        repo = await store.get_or_create_repo(repo_name)
        main_oid = await store.get_ref_oid(repo.id, RefName(value="main"))
        assert main_oid is not None

        failed_id = uuid.uuid4()
        approved_id = uuid.uuid4()
        session.add(
            AgentRun(
                id=failed_id,
                repo_id=repo.id,
                branch=f"agent/{failed_id.hex}",
                status=AgentRunStatus.FAILED.value,
                base_commit=main_oid,
                summary="boom",
            )
        )
        session.add(
            AgentRun(
                id=approved_id,
                repo_id=repo.id,
                branch=f"agent/{approved_id.hex}",
                status=AgentRunStatus.APPROVED.value,
                base_commit=main_oid,
                head_commit=main_oid,
                summary="landed",
            )
        )
        await store.set_ref(repo.id, f"refs/heads/agent/{failed_id.hex}", main_oid)
        await session.flush()

    app = create_app(settings)
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        blocked = await client.delete(f"/api/agents/{approved_id}")
        assert blocked.status_code == 409, blocked.text

        ok = await client.delete(f"/api/agents/{failed_id}")
        assert ok.status_code == 200, ok.text
        assert ok.json()["run_id"] == str(failed_id)

        agents = await client.get("/api/agents")
        assert agents.status_code == 200
        ids = {r["id"] for r in agents.json()["runs"]}
        assert str(failed_id) not in ids
        assert str(approved_id) in ids

    async with session_scope(engine) as session:
        store = PostgresGitStore(session)
        repo = await store.get_or_create_repo(repo_name)
        assert (
            await store.get_ref_oid(repo.id, RefName(value=f"agent/{failed_id.hex}"))
            is None
        )


@pytest.mark.integration
async def test_prepare_rebase_reuses_warm_workspace(engine, database_url: str) -> None:
    repo_name = "test-same-agent-rebase"
    root = Path(tempfile.mkdtemp(prefix="git-pg-sessions-"))
    settings = ApiSettings(
        database_url=database_url,
        sessions_root=str(root / "sessions"),
        warm_cache_root=str(root / "warm"),
        demo_repo=repo_name,
    )

    async with session_scope(engine) as session:
        await session.execute(delete(Repository).where(Repository.name == repo_name))
        await session.flush()

    local = _init_repo({"README.md": "hi\n"})
    warm = Path(tempfile.mkdtemp(prefix="git-pg-warm-agent-"))
    subprocess.run(["git", "clone", str(local), str(warm)], check=True)

    run_id = uuid.uuid4()
    async with session_scope(engine) as session:
        await push_and_ingest(
            session,
            repo_name,
            local,
            RefName(value="main"),
            allow_main=True,
            project_versions=True,
        )
        # Advance main so rebase has something to fetch.
        (local / "docs").mkdir(exist_ok=True)
        (local / "docs" / "from-main.md").write_text("landed\n")
        subprocess.run(["git", "-C", str(local), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(local), "commit", "-m", "main move"],
            check=True,
        )
        await push_and_ingest(
            session,
            repo_name,
            local,
            RefName(value="main"),
            allow_main=True,
            project_versions=True,
        )
        store = PostgresGitStore(session)
        repo = await store.get_or_create_repo(repo_name)
        main_oid = await store.get_ref_oid(repo.id, RefName(value="main"))
        assert main_oid is not None
        branch = f"agent/{run_id.hex}"
        await store.set_ref(repo.id, f"refs/heads/{branch}", main_oid)
        session.add(
            AgentRun(
                id=run_id,
                repo_id=repo.id,
                branch=branch,
                status=AgentRunStatus.AGENT_REBASING.value,
                base_commit=main_oid,
                head_commit=main_oid,
                session_cwd=str(warm),
                claude_session_id="sess-warm-123",
                prompt="edit docs",
            )
        )
        await session.flush()

    from git_pg.agents import AgentService

    agents = AgentService(engine, settings)
    context = await agents._prepare_rebase_workspace(run_id, repo_name)
    assert context.keep_alive is True
    assert context.cwd == warm
    assert context.claude_session_id == "sess-warm-123"
    assert context.session_id == run_id.hex[:12]
    # Fresh main tip should now exist in the warm tree.
    show = subprocess.run(
        ["git", "-C", str(warm), "show", "main:docs/from-main.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert show.stdout == "landed\n"


@pytest.mark.integration
async def test_approve_defers_rebase_for_still_running_sibling(
    engine, database_url: str
) -> None:
    repo_name = "test-defer-rebase"
    root = Path(tempfile.mkdtemp(prefix="git-pg-sessions-"))
    settings = ApiSettings(
        database_url=database_url,
        sessions_root=str(root / "sessions"),
        warm_cache_root=str(root / "warm"),
        demo_repo=repo_name,
    )

    async with session_scope(engine) as session:
        await session.execute(delete(Repository).where(Repository.name == repo_name))
        await session.flush()

    local = _init_repo({"README.md": "hi\n"})
    async with session_scope(engine) as session:
        await push_and_ingest(
            session,
            repo_name,
            local,
            RefName(value="main"),
            allow_main=True,
            project_versions=True,
        )
        store = PostgresGitStore(session)
        repo = await store.get_or_create_repo(repo_name)
        main_oid = await store.get_ref_oid(repo.id, RefName(value="main"))
        assert main_oid is not None

        run_ready = uuid.uuid4()
        run_busy = uuid.uuid4()
        # Ready sibling with a tip == main (trivial FF).
        session.add(
            AgentRun(
                id=run_ready,
                repo_id=repo.id,
                branch=f"agent/{run_ready.hex}",
                status=AgentRunStatus.AWAITING_APPROVAL.value,
                base_commit=main_oid,
                head_commit=main_oid,
                summary="ready",
            )
        )
        session.add(
            AgentRun(
                id=run_busy,
                repo_id=repo.id,
                branch=f"agent/{run_busy.hex}",
                status=AgentRunStatus.RUNNING.value,
                base_commit=main_oid,
                summary="still working",
            )
        )
        await session.flush()

    from git_pg.agents import AgentService, RebaseStrategy

    agents = AgentService(engine, settings)
    result = await agents.approve(run_ready, rebase_strategy=RebaseStrategy.AUTO)
    assert result.rebasing_run_ids == ()
    assert run_busy in agents._pending_rebase
    assert agents._pending_rebase[run_busy] == RebaseStrategy.AUTO

    # Simulate the busy run finishing against the old base after main moved.
    async with session_scope(engine) as session:
        row = await session.execute(select(AgentRun).where(AgentRun.id == run_busy))
        busy = row.scalar_one()
        busy.status = AgentRunStatus.AWAITING_APPROVAL.value
        busy.head_commit = main_oid
        await session.flush()

    # Advance main so the finished run is stale.
    (local / "extra.md").write_text("x\n")
    subprocess.run(["git", "-C", str(local), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(local), "commit", "-m", "main moved"],
        check=True,
    )
    async with session_scope(engine) as session:
        await push_and_ingest(
            session,
            repo_name,
            local,
            RefName(value="main"),
            allow_main=True,
            project_versions=True,
        )

    await agents._queue_rebase_if_stale(run_busy, repo_name)
    async with session_scope(engine) as session:
        row = await session.execute(select(AgentRun).where(AgentRun.id == run_busy))
        busy = row.scalar_one()
        assert busy.status == AgentRunStatus.AUTO_REBASING.value
    assert run_busy not in agents._pending_rebase
