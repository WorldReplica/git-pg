from __future__ import annotations

import asyncio
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from git_pg.config import Settings
from git_pg.db.engine import session_scope
from git_pg.db.orm.models import AgentRun, AgentRunStatus, GitRef
from git_pg.file_versions import (
    ProjectedVersion,
    map_tree_blobs,
    project_main_tip_versions,
)
from git_pg.models.repo import RefName, SessionStartRequest
from git_pg.session import SessionManager
from git_pg.store.postgres import PostgresGitStore, _parse_commit_links
from git_pg.sync import push_and_ingest
from git_pg.warm_cache import WarmCacheManager

DEFAULT_AGENT_PROMPT = (
    "You are editing a demo git repository at /workspace. "
    "Make a small, useful change: update or create files under data/ and docs/ "
    "(prefer JSON or Markdown). Work in small steps and make **atomic commits** "
    "on the current branch — typically 2–4 commits, each with one clear purpose "
    "and a focused message (e.g. add a file, then update related docs). "
    "Do not squash everything into a single commit. "
    "Do not push. Do not modify .git configuration."
)

_CLAUDE_SESSION_RE = re.compile(r"GIT_PG_CLAUDE_SESSION_ID=(\S+)")


class RebaseStrategy(StrEnum):
    AUTO = "auto"
    AGENT = "agent"


@dataclass(frozen=True)
class DockerRunResult:
    container_id: str
    exit_code: int
    summary: str
    claude_session_id: str | None


@dataclass(frozen=True)
class AgentSpawnResult:
    run_id: uuid.UUID
    branch: str
    session_cwd: str
    container_id: str | None


@dataclass(frozen=True)
class ApproveResult:
    projected_versions: tuple[ProjectedVersion, ...]
    rebasing_run_ids: tuple[uuid.UUID, ...]


class AgentDockerRunner:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _compose_dir(self) -> Path:
        if self._settings.compose_project_dir:
            return Path(self._settings.compose_project_dir)
        # src/git_pg/agents.py -> repo root
        return Path(__file__).resolve().parents[2]

    async def start(
        self,
        *,
        workspace: Path,
        prompt: str,
        resume: str | None = None,
    ) -> str:
        """Start the agent container in the git-pg compose project; return id."""
        api_key = self._settings.anthropic_api_key
        if not api_key:
            msg = (
                "ANTHROPIC_API_KEY is not set; add it to .env "
                "(see .env.example) or the process environment"
            )
            raise RuntimeError(msg)
        compose_dir = self._compose_dir()
        uid = os.getuid()
        gid = os.getgid()
        # Persist Claude transcript next to the repo so resume works across
        # short-lived containers (HOME is not the container's ephemeral /tmp).
        agent_home = workspace.parent / "agent-home"
        agent_home.mkdir(parents=True, exist_ok=True)
        cmd = [
            "docker",
            "compose",
            "--project-name",
            self._settings.compose_project_name,
            "--project-directory",
            str(compose_dir),
            "-f",
            str(compose_dir / "docker-compose.yml"),
            "run",
            "-d",
            "--no-deps",
            # Claude CLI refuses --dangerously-skip-permissions as root.
            "-u",
            f"{uid}:{gid}",
            "-e",
            "HOME=/tmp/agent-home",
            "-v",
            f"{workspace.resolve()}:/workspace",
            "-v",
            f"{agent_home.resolve()}:/tmp/agent-home",
            "-e",
            f"ANTHROPIC_API_KEY={api_key}",
            "-e",
            f"GIT_PG_AGENT_PROMPT={prompt}",
            "-e",
            "PYTHONUNBUFFERED=1",
        ]
        if resume:
            cmd.extend(["-e", f"GIT_PG_CLAUDE_RESUME={resume}"])
        cmd.append("agent")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(compose_dir),
            env={
                **os.environ,
                "GIT_PG_AGENT_DOCKER_IMAGE": self._settings.agent_docker_image,
            },
        )
        out, err = await proc.communicate()
        if proc.returncode != 0:
            msg = err.decode() or out.decode() or "docker compose run failed"
            raise RuntimeError(msg)
        return out.decode().strip()

    async def wait(self, container_id: str) -> DockerRunResult:
        try:
            wait = await asyncio.create_subprocess_exec(
                "docker",
                "wait",
                container_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            wait_out, _ = await wait.communicate()
            exit_code = int(wait_out.decode().strip() or "1")
        except asyncio.CancelledError:
            # Leave the container running; caller may reconcile later.
            raise

        logs = await asyncio.create_subprocess_exec(
            "docker",
            "logs",
            container_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        log_out, _ = await logs.communicate()
        summary = log_out.decode()[-4000:]
        match = _CLAUDE_SESSION_RE.search(summary)
        claude_session_id = match.group(1) if match else None
        await self.stop(container_id)
        return DockerRunResult(
            container_id=container_id,
            exit_code=exit_code,
            summary=summary,
            claude_session_id=claude_session_id,
        )

    async def stop(self, container_id: str) -> None:
        """Force-remove a container (no-op if already gone)."""
        await asyncio.create_subprocess_exec(
            "docker",
            "rm",
            "-f",
            container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def run(
        self,
        *,
        workspace: Path,
        prompt: str,
        resume: str | None = None,
    ) -> DockerRunResult:
        container_id = await self.start(
            workspace=workspace, prompt=prompt, resume=resume
        )
        return await self.wait(container_id)


class AgentService:
    def __init__(
        self,
        engine: AsyncEngine,
        settings: Settings,
        *,
        event_sink: EventSink | None = None,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._sessions = SessionManager(engine, settings)
        self._warm = WarmCacheManager(settings)
        self._docker = AgentDockerRunner(settings)
        self._events = event_sink
        # run_id -> strategy when main moved while the sibling was still running
        self._pending_rebase: dict[uuid.UUID, RebaseStrategy] = {}

    async def spawn(
        self,
        *,
        repo_name: str,
        prompt: str = DEFAULT_AGENT_PROMPT,
    ) -> AgentSpawnResult:
        run_id = uuid.uuid4()
        branch = f"agent/{run_id.hex}"

        async with session_scope(self._engine) as session:
            store = PostgresGitStore(session)
            repo = await store.get_or_create_repo(repo_name)
            main_oid = await store.get_ref_oid(repo.id, RefName(value="main"))
            if main_oid is None:
                msg = "main branch missing; seed the demo repo first"
                raise LookupError(msg)
            await store.set_ref(repo.id, f"refs/heads/{branch}", main_oid)
            run = AgentRun(
                id=run_id,
                repo_id=repo.id,
                branch=branch,
                status=AgentRunStatus.RUNNING.value,
                base_commit=main_oid,
                prompt=prompt,
            )
            session.add(run)
            await session.flush()

        handle = await self._sessions.start(
            SessionStartRequest(repo=repo_name, ref=branch, session_id=run_id.hex[:12])
        )

        async with session_scope(self._engine) as session:
            result = await session.execute(
                select(AgentRun).where(AgentRun.id == run_id)
            )
            run = result.scalar_one()
            run.session_cwd = handle.cwd
            await session.flush()

        asyncio.create_task(
            self._run_and_finish(
                run_id=run_id,
                repo_name=repo_name,
                branch=branch,
                cwd=Path(handle.cwd),
                prompt=prompt,
                session_id=handle.session_id,
            )
        )
        return AgentSpawnResult(
            run_id=run_id,
            branch=branch,
            session_cwd=handle.cwd,
            container_id=None,
        )

    async def enqueue_spawn(
        self,
        *,
        repo_name: str,
        prompt: str | None = None,
    ) -> AgentSpawnResult:
        """Create a running row immediately; call drive_spawn in the background."""
        run_id = uuid.uuid4()
        branch = f"agent/{run_id.hex}"

        async with session_scope(self._engine) as session:
            store = PostgresGitStore(session)
            repo = await store.get_or_create_repo(repo_name)
            main_oid = await store.get_ref_oid(repo.id, RefName(value="main"))
            if main_oid is None:
                msg = "main branch missing; seed the demo repo first"
                raise LookupError(msg)
            await store.set_ref(repo.id, f"refs/heads/{branch}", main_oid)
            run = AgentRun(
                id=run_id,
                repo_id=repo.id,
                branch=branch,
                status=AgentRunStatus.RUNNING.value,
                base_commit=main_oid,
                prompt=prompt,
                summary="queued — generating task…" if prompt is None else "queued",
            )
            session.add(run)
            await session.flush()

        return AgentSpawnResult(
            run_id=run_id,
            branch=branch,
            session_cwd="",
            container_id=None,
        )

    async def drive_spawn(
        self,
        run_id: uuid.UUID,
        *,
        repo_name: str,
        prompt: str | None = None,
    ) -> None:
        """Generate prompt if needed, materialize workspace, run Docker agent."""
        try:
            if not await self._run_exists(run_id):
                return
            resolved = prompt
            if resolved is None or not resolved.strip():
                tree_paths = await self._main_tree_paths(repo_name)
                from git_pg.task_gen import generate_agent_task

                generated = await generate_agent_task(
                    api_key=self._settings.anthropic_api_key,
                    tree_paths=tree_paths,
                    model=self._settings.task_gen_model,
                )
                resolved = generated.prompt
                async with session_scope(self._engine) as session:
                    result = await session.execute(
                        select(AgentRun).where(AgentRun.id == run_id)
                    )
                    run = result.scalar_one_or_none()
                    if run is None:
                        return
                    run.prompt = resolved
                    run.summary = "task ready — starting sandbox…"
                    await session.flush()
                if self._events is not None:
                    await self._events.agent_updated(run_id)

            async with session_scope(self._engine) as session:
                result = await session.execute(
                    select(AgentRun).where(AgentRun.id == run_id)
                )
                run = result.scalar_one_or_none()
                if run is None:
                    return
                branch = run.branch

            handle = await self._sessions.start(
                SessionStartRequest(
                    repo=repo_name, ref=branch, session_id=run_id.hex[:12]
                )
            )
            async with session_scope(self._engine) as session:
                result = await session.execute(
                    select(AgentRun).where(AgentRun.id == run_id)
                )
                run = result.scalar_one_or_none()
                if run is None:
                    self._sessions.stop(handle.session_id)
                    return
                run.session_cwd = handle.cwd
                run.summary = "sandbox ready — agent running…"
                await session.flush()
            if self._events is not None:
                await self._events.agent_updated(run_id)

            await self._run_and_finish(
                run_id=run_id,
                repo_name=repo_name,
                branch=branch,
                cwd=Path(handle.cwd),
                prompt=resolved,
                session_id=handle.session_id,
            )
        except Exception as exc:
            if await self._run_exists(run_id):
                await self._mark(
                    run_id,
                    status=AgentRunStatus.FAILED,
                    summary=f"spawn failed: {exc}",
                    container_id=None,
                )

    async def _main_tree_paths(self, repo_name: str) -> tuple[str, ...]:
        async with session_scope(self._engine) as session:
            store = PostgresGitStore(session)
            repo = await store.get_repo(repo_name)
            if repo is None:
                return ()
            tip = await store.get_ref_oid(repo.id, RefName(value="main"))
            if tip is None:
                return ()
            content = await store.get_object(repo.id, tip)
            if content is None:
                return ()
            _, tree_oid = _parse_commit_links(content)
            blobs = await map_tree_blobs(store, repo.id, tree_oid)
            return tuple(sorted(b.path for b in blobs))

    async def _run_and_finish(
        self,
        *,
        run_id: uuid.UUID,
        repo_name: str,
        branch: str,
        cwd: Path,
        prompt: str,
        session_id: str,
    ) -> None:
        container_id: str | None = None
        keep_session = False
        try:
            # Ensure agent branch is checked out in the worktree
            await asyncio.to_thread(_ensure_branch_checkout, cwd, branch)
            container_id = await self._docker.start(workspace=cwd, prompt=prompt)
            if not await self._run_exists(run_id):
                await self._docker.stop(container_id)
                return
            async with session_scope(self._engine) as session:
                result = await session.execute(
                    select(AgentRun).where(AgentRun.id == run_id)
                )
                run = result.scalar_one_or_none()
                if run is None:
                    await self._docker.stop(container_id)
                    return
                run.container_id = container_id
                await session.flush()
            if self._events is not None:
                await self._events.agent_updated(run_id)

            docker_result = await self._docker.wait(container_id)
            container_id = None  # wait() already removed the container
            if not await self._run_exists(run_id):
                return
            if docker_result.exit_code != 0:
                await self._mark(
                    run_id,
                    status=AgentRunStatus.FAILED,
                    summary=docker_result.summary,
                    container_id=None,
                )
                return

            async with session_scope(self._engine) as session:
                push = await push_and_ingest(
                    session,
                    repo_name,
                    cwd,
                    RefName(value=branch),
                    allow_main=False,
                )
                result = await session.execute(
                    select(AgentRun).where(AgentRun.id == run_id)
                )
                run = result.scalar_one_or_none()
                if run is None:
                    return
                run.status = AgentRunStatus.AWAITING_APPROVAL.value
                run.head_commit = bytes.fromhex(push.head.hex)
                run.container_id = None
                run.claude_session_id = docker_result.claude_session_id
                run.summary = docker_result.summary[-2000:]
                # Live until approve/reject/delete — keep workspace warm for rebase.
                run.finished_at = None
                await session.flush()
            keep_session = True
            await self._warm_refresh(repo_name)
            if self._events is not None:
                await self._events.agent_updated(run_id)
            await self._queue_rebase_if_stale(run_id, repo_name)
        except asyncio.CancelledError:
            # Uvicorn --reload (or shutdown) cancelled the watcher. Keep the
            # session checkout so work can be recovered; mark the run failed.
            keep_session = True
            if await self._run_exists(run_id):
                await self._mark(
                    run_id,
                    status=AgentRunStatus.FAILED,
                    summary=(
                        "agent watcher cancelled (API reload/shutdown); "
                        "workspace left on disk for recovery"
                    ),
                    container_id=container_id,
                    teardown=False,
                )
            raise
        except Exception as exc:
            if await self._run_exists(run_id):
                await self._mark(
                    run_id,
                    status=AgentRunStatus.FAILED,
                    summary=str(exc),
                    container_id=container_id,
                )
        finally:
            if not keep_session:
                self._sessions.stop(session_id)

    async def approve(
        self,
        run_id: uuid.UUID,
        *,
        rebase_strategy: RebaseStrategy = RebaseStrategy.AUTO,
    ) -> ApproveResult:
        async with session_scope(self._engine) as session:
            store = PostgresGitStore(session)
            result = await session.execute(
                select(AgentRun).where(AgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None:
                msg = f"agent run {run_id} not found"
                raise LookupError(msg)
            if run.status != AgentRunStatus.AWAITING_APPROVAL.value:
                msg = f"agent run not awaiting approval (status={run.status})"
                raise ValueError(msg)
            if run.head_commit is None:
                msg = "agent run has no head commit"
                raise ValueError(msg)

            main_ref = RefName(value="main")
            main_oid = await store.get_ref_oid(run.repo_id, main_ref)
            if main_oid is None:
                msg = "main missing"
                raise LookupError(msg)
            if main_oid != run.base_commit:
                msg = "main moved since spawn; rebase needed"
                raise ValueError(msg)
            if not await store.is_ancestor(run.repo_id, main_oid, run.head_commit):
                msg = "agent branch is not a fast-forward of main"
                raise ValueError(msg)

            previous = await store.fast_forward_ref(
                run.repo_id, main_ref, run.head_commit
            )
            projected = await project_main_tip_versions(
                session,
                store,
                run.repo_id,
                previous_commit_oid=previous,
                new_commit_oid=run.head_commit,
            )
            run.status = AgentRunStatus.APPROVED.value
            run.finished_at = datetime.now(UTC)
            await session.flush()
            repo = await store.get_repo_by_id(run.repo_id)
            repo_id = run.repo_id

            # Fan-out: awaiting siblings rebase now; still-running ones rebase
            # when they reach awaiting_approval (see _queue_rebase_if_stale).
            siblings = await session.execute(
                select(AgentRun).where(
                    AgentRun.repo_id == repo_id,
                    AgentRun.id != run_id,
                    AgentRun.status.in_(
                        [
                            AgentRunStatus.AWAITING_APPROVAL.value,
                            AgentRunStatus.RUNNING.value,
                        ]
                    ),
                )
            )
            sibling_runs = list(siblings.scalars().all())
            rebase_status = (
                AgentRunStatus.AUTO_REBASING
                if rebase_strategy == RebaseStrategy.AUTO
                else AgentRunStatus.AGENT_REBASING
            )
            rebasing_ids: list[uuid.UUID] = []
            for sibling in sibling_runs:
                note = (
                    f"rebase queued ({rebase_strategy.value}) after approve of {run_id}"
                )
                if sibling.status == AgentRunStatus.AWAITING_APPROVAL.value:
                    sibling.status = rebase_status.value
                    sibling.summary = (
                        f"{sibling.summary}\n{note}" if sibling.summary else note
                    )[-2000:]
                    rebasing_ids.append(sibling.id)
                else:
                    # Still running — remember strategy for when they finish.
                    self._pending_rebase[sibling.id] = rebase_strategy
                    deferred = f"{note} (deferred until this run finishes)"
                    sibling.summary = (
                        f"{sibling.summary}\n{deferred}"
                        if sibling.summary
                        else deferred
                    )[-2000:]
            await session.flush()

        # Approved run no longer needs a warm sandbox.
        await self._teardown_sandbox(run_id)

        if repo is not None:
            await self._warm_refresh(repo.name)
        if self._events is not None:
            await self._events.agent_updated(run_id)
            await self._events.main_updated()
            if projected:
                await self._events.file_versions_created(projected)
            for sid in rebasing_ids:
                await self._events.agent_updated(sid)

        repo_name = repo.name if repo is not None else ""
        for sid in rebasing_ids:
            if rebase_strategy == RebaseStrategy.AUTO:
                asyncio.create_task(self._auto_rebase(sid, repo_name))
            else:
                asyncio.create_task(self._agent_rebase(sid, repo_name))

        return ApproveResult(
            projected_versions=tuple(projected),
            rebasing_run_ids=tuple(rebasing_ids),
        )

    async def _auto_rebase(self, run_id: uuid.UUID, repo_name: str) -> None:
        context: RebaseWorkspace | None = None
        try:
            if not await self._run_exists(run_id):
                return
            context = await self._prepare_rebase_workspace(run_id, repo_name)
            rebase = await asyncio.to_thread(_git_rebase_onto_main, context.cwd)
            if not await self._run_exists(run_id):
                return
            if rebase.returncode != 0:
                await asyncio.to_thread(_git_rebase_abort, context.cwd)
                await self._mark(
                    run_id,
                    status=AgentRunStatus.FAILED,
                    summary=f"auto rebase failed:\n{rebase.output[-1800:]}",
                    container_id=None,
                )
                return
            await self._finish_rebase_success(
                run_id=run_id,
                repo_name=repo_name,
                branch=context.branch,
                cwd=context.cwd,
                main_oid=context.main_oid,
                summary="auto rebase onto main succeeded",
                claude_session_id=context.claude_session_id,
            )
        except Exception as exc:
            if await self._run_exists(run_id):
                await self._mark(
                    run_id,
                    status=AgentRunStatus.FAILED,
                    summary=f"auto rebase error: {exc}",
                    container_id=None,
                )
        finally:
            if context is not None and not context.keep_alive:
                self._sessions.stop(context.session_id)

    async def _agent_rebase(self, run_id: uuid.UUID, repo_name: str) -> None:
        context: RebaseWorkspace | None = None
        container_id: str | None = None
        try:
            if not await self._run_exists(run_id):
                return
            context = await self._prepare_rebase_workspace(run_id, repo_name)
            prompt = await self._semantic_rebase_prompt(run_id, context)
            container_id = await self._docker.start(
                workspace=context.cwd,
                prompt=prompt,
                resume=context.claude_session_id,
            )
            if not await self._run_exists(run_id):
                await self._docker.stop(container_id)
                return
            async with session_scope(self._engine) as session:
                result = await session.execute(
                    select(AgentRun).where(AgentRun.id == run_id)
                )
                run = result.scalar_one_or_none()
                if run is None:
                    await self._docker.stop(container_id)
                    return
                run.container_id = container_id
                await session.flush()
            if self._events is not None:
                await self._events.agent_updated(run_id)

            docker_result = await self._docker.wait(container_id)
            container_id = None
            if not await self._run_exists(run_id):
                return
            if docker_result.exit_code != 0:
                await self._mark(
                    run_id,
                    status=AgentRunStatus.FAILED,
                    summary=docker_result.summary[-2000:],
                    container_id=None,
                )
                return
            await asyncio.to_thread(_commit_leftovers, context.cwd)
            await self._finish_rebase_success(
                run_id=run_id,
                repo_name=repo_name,
                branch=context.branch,
                cwd=context.cwd,
                main_oid=context.main_oid,
                summary=docker_result.summary[-2000:]
                or "agent semantic rebase succeeded",
                claude_session_id=docker_result.claude_session_id
                or context.claude_session_id,
            )
        except Exception as exc:
            if await self._run_exists(run_id):
                await self._mark(
                    run_id,
                    status=AgentRunStatus.FAILED,
                    summary=f"agent rebase error: {exc}",
                    container_id=container_id,
                )
        finally:
            if context is not None and not context.keep_alive:
                self._sessions.stop(context.session_id)

    async def _finish_rebase_success(
        self,
        *,
        run_id: uuid.UUID,
        repo_name: str,
        branch: str,
        cwd: Path,
        main_oid: bytes,
        summary: str,
        claude_session_id: str | None = None,
    ) -> None:
        async with session_scope(self._engine) as session:
            push = await push_and_ingest(
                session,
                repo_name,
                cwd,
                RefName(value=branch),
                allow_main=False,
                require_fast_forward=False,
            )
            result = await session.execute(
                select(AgentRun).where(AgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None:
                return
            run.status = AgentRunStatus.AWAITING_APPROVAL.value
            run.base_commit = main_oid
            run.head_commit = bytes.fromhex(push.head.hex)
            run.summary = summary[-2000:]
            run.container_id = None
            if claude_session_id:
                run.claude_session_id = claude_session_id
            run.finished_at = None
            await session.flush()
        await self._warm_refresh(repo_name)
        if self._events is not None:
            await self._events.agent_updated(run_id)
        await self._queue_rebase_if_stale(run_id, repo_name)

    async def request_rebase(
        self,
        run_id: uuid.UUID,
        *,
        repo_name: str,
        rebase_strategy: RebaseStrategy = RebaseStrategy.AUTO,
    ) -> None:
        """Manually queue a rebase for an awaiting (possibly stale) run."""
        async with session_scope(self._engine) as session:
            result = await session.execute(
                select(AgentRun).where(AgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None:
                msg = f"agent run {run_id} not found"
                raise LookupError(msg)
            if run.status != AgentRunStatus.AWAITING_APPROVAL.value:
                msg = f"agent run not awaiting approval (status={run.status})"
                raise ValueError(msg)
            store = PostgresGitStore(session)
            main_oid = await store.get_ref_oid(run.repo_id, RefName(value="main"))
            if main_oid is None:
                msg = "main missing"
                raise LookupError(msg)
            if main_oid == run.base_commit:
                msg = "already based on current main"
                raise ValueError(msg)
            rebase_status = (
                AgentRunStatus.AUTO_REBASING
                if rebase_strategy == RebaseStrategy.AUTO
                else AgentRunStatus.AGENT_REBASING
            )
            run.status = rebase_status.value
            note = f"rebase requested ({rebase_strategy.value})"
            run.summary = (f"{run.summary}\n{note}" if run.summary else note)[-2000:]
            await session.flush()
        if self._events is not None:
            await self._events.agent_updated(run_id)
        if rebase_strategy == RebaseStrategy.AUTO:
            asyncio.create_task(self._auto_rebase(run_id, repo_name))
        else:
            asyncio.create_task(self._agent_rebase(run_id, repo_name))

    async def _queue_rebase_if_stale(
        self,
        run_id: uuid.UUID,
        repo_name: str,
        *,
        strategy: RebaseStrategy | None = None,
    ) -> None:
        """If main moved while this run was in flight, rebase before approve."""
        preferred = strategy or self._pending_rebase.pop(run_id, None)
        async with session_scope(self._engine) as session:
            result = await session.execute(
                select(AgentRun).where(AgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None:
                return
            if run.status != AgentRunStatus.AWAITING_APPROVAL.value:
                return
            store = PostgresGitStore(session)
            main_oid = await store.get_ref_oid(run.repo_id, RefName(value="main"))
            if main_oid is None or main_oid == run.base_commit:
                return
            if preferred is None:
                preferred = (
                    RebaseStrategy.AGENT
                    if run.claude_session_id
                    else RebaseStrategy.AUTO
                )
            rebase_status = (
                AgentRunStatus.AUTO_REBASING
                if preferred == RebaseStrategy.AUTO
                else AgentRunStatus.AGENT_REBASING
            )
            run.status = rebase_status.value
            note = (
                f"auto-rebase ({preferred.value}) — main moved while this run "
                "was still working"
            )
            run.summary = (f"{run.summary}\n{note}" if run.summary else note)[-2000:]
            await session.flush()
        if self._events is not None:
            await self._events.agent_updated(run_id)
        if preferred == RebaseStrategy.AUTO:
            asyncio.create_task(self._auto_rebase(run_id, repo_name))
        else:
            asyncio.create_task(self._agent_rebase(run_id, repo_name))

    async def _prepare_rebase_workspace(
        self,
        run_id: uuid.UUID,
        repo_name: str,
    ) -> RebaseWorkspace:
        """Reuse the author's warm workspace when present; else clone fresh."""
        async with session_scope(self._engine) as session:
            result = await session.execute(
                select(AgentRun).where(AgentRun.id == run_id)
            )
            run = result.scalar_one()
            branch = run.branch
            store = PostgresGitStore(session)
            main_oid = await store.get_ref_oid(run.repo_id, RefName(value="main"))
            if main_oid is None:
                msg = "main missing during rebase"
                raise LookupError(msg)
            prompt = run.prompt
            old_base = run.base_commit
            head = run.head_commit
            session_cwd = run.session_cwd
            claude_session_id = run.claude_session_id
            author_session_id = run_id.hex[:12]

        keep_alive = False
        if session_cwd and Path(session_cwd).is_dir():
            cwd = Path(session_cwd)
            session_id = author_session_id
            keep_alive = True
        else:
            handle = await self._sessions.start(
                SessionStartRequest(
                    repo=repo_name,
                    ref=branch,
                    session_id=f"rb-{run_id.hex[:10]}",
                )
            )
            cwd = Path(handle.cwd)
            session_id = handle.session_id
            async with session_scope(self._engine) as session:
                result = await session.execute(
                    select(AgentRun).where(AgentRun.id == run_id)
                )
                run = result.scalar_one_or_none()
                if run is not None:
                    run.session_cwd = handle.cwd
                    await session.flush()

        # Checkout agent branch first so fetch can update refs/heads/main.
        await asyncio.to_thread(_ensure_branch_checkout, cwd, branch)
        with tempfile.TemporaryDirectory(prefix="git-pg-main-export-") as tmp:
            main_export = Path(tmp) / "main"
            async with session_scope(self._engine) as session:
                store = PostgresGitStore(session)
                repo = await store.get_repo(repo_name)
                if repo is None:
                    msg = f"repo {repo_name} missing"
                    raise LookupError(msg)
                await store.export_to_local(repo.id, main_export, RefName(value="main"))
            await asyncio.to_thread(_fetch_main_into, cwd, main_export)
        return RebaseWorkspace(
            session_id=session_id,
            cwd=cwd,
            branch=branch,
            main_oid=main_oid,
            prompt=prompt,
            old_base=old_base,
            head=head,
            claude_session_id=claude_session_id,
            keep_alive=keep_alive,
        )

    async def _semantic_rebase_prompt(
        self,
        run_id: uuid.UUID,
        context: RebaseWorkspace,
    ) -> str:
        main_paths: list[str] = []
        async with session_scope(self._engine) as session:
            store = PostgresGitStore(session)
            result = await session.execute(
                select(AgentRun).where(AgentRun.id == run_id)
            )
            run = result.scalar_one()
            old_tree = await _commit_tree(store, run.repo_id, context.old_base)
            new_tree = await _commit_tree(store, run.repo_id, context.main_oid)
            if old_tree is not None and new_tree is not None:
                old_blobs = {
                    b.path: b.blob_oid
                    for b in await map_tree_blobs(store, run.repo_id, old_tree)
                }
                new_blobs = {
                    b.path: b.blob_oid
                    for b in await map_tree_blobs(store, run.repo_id, new_tree)
                }
                for path in sorted(set(old_blobs) | set(new_blobs)):
                    if old_blobs.get(path) != new_blobs.get(path):
                        main_paths.append(path)

        original = context.prompt or DEFAULT_AGENT_PROMPT
        changed = "\n".join(f"- {p}" for p in main_paths) or "- (no path diff)"
        resume_note = (
            "This is a continuation of your earlier work in this workspace "
            "(same Claude session). You already authored the branch changes.\n"
            if context.claude_session_id
            else ""
        )
        return (
            "You are reconciling an agent branch onto an updated main in /workspace.\n"
            f"{resume_note}"
            f"Original task:\n{original}\n\n"
            f"Your previous base commit: {context.old_base.hex()}\n"
            f"Your previous head commit: "
            f"{context.head.hex() if context.head else '(unknown)'}\n"
            f"Current main tip: {context.main_oid.hex()}\n"
            "Paths that changed on main since your base:\n"
            f"{changed}\n\n"
            "Local branch is already checked out. "
            "main is available as refs/heads/main.\n"
            "Rebase or otherwise reconcile your work onto current main with the "
            "intent of your original task in mind. Resolve conflicts thoughtfully.\n"
            "Prefer atomic commits if you need new commits (one purpose each). "
            "Do not push. Do not modify .git configuration."
        )

    async def reject(self, run_id: uuid.UUID) -> None:
        async with session_scope(self._engine) as session:
            result = await session.execute(
                select(AgentRun).where(AgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None:
                msg = f"agent run {run_id} not found"
                raise LookupError(msg)
            run.status = AgentRunStatus.REJECTED.value
            run.finished_at = datetime.now(UTC)
            container_id = run.container_id
            await session.flush()
        if container_id:
            await self._docker.stop(container_id)
        await self._teardown_sandbox(run_id)
        if self._events is not None:
            await self._events.agent_updated(run_id)

    async def delete(self, run_id: uuid.UUID) -> None:
        """Remove a non-approved run and tear down its sandbox/container."""
        async with session_scope(self._engine) as session:
            result = await session.execute(
                select(AgentRun).where(AgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None:
                msg = f"agent run {run_id} not found"
                raise LookupError(msg)
            if run.status == AgentRunStatus.APPROVED.value:
                msg = "approved agent runs cannot be deleted"
                raise ValueError(msg)
            container_id = run.container_id
            branch = run.branch
            repo_id = run.repo_id
            await session.execute(delete(AgentRun).where(AgentRun.id == run_id))
            await session.execute(
                delete(GitRef).where(
                    GitRef.repo_id == repo_id,
                    GitRef.name == f"refs/heads/{branch}",
                )
            )
            await session.flush()

        if container_id:
            await self._docker.stop(container_id)
        await self._teardown_sandbox(run_id)
        if self._events is not None:
            await self._events.agent_updated(run_id)

    async def _teardown_sandbox(self, run_id: uuid.UUID) -> None:
        self._sessions.stop(run_id.hex[:12])
        self._sessions.stop(f"rb-{run_id.hex[:10]}")
        async with session_scope(self._engine) as session:
            result = await session.execute(
                select(AgentRun).where(AgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None:
                return
            run.session_cwd = None
            run.claude_session_id = None
            run.container_id = None
            await session.flush()

    async def _run_exists(self, run_id: uuid.UUID) -> bool:
        async with session_scope(self._engine) as session:
            result = await session.execute(
                select(AgentRun.id).where(AgentRun.id == run_id)
            )
            return result.scalar_one_or_none() is not None

    async def _mark(
        self,
        run_id: uuid.UUID,
        *,
        status: AgentRunStatus,
        summary: str,
        container_id: str | None,
        teardown: bool | None = None,
    ) -> None:
        async with session_scope(self._engine) as session:
            result = await session.execute(
                select(AgentRun).where(AgentRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if run is None:
                return
            run.status = status.value
            run.summary = summary[-2000:]
            run.container_id = container_id
            run.finished_at = datetime.now(UTC)
            await session.flush()
        do_teardown = (
            teardown if teardown is not None else status == AgentRunStatus.FAILED
        )
        if do_teardown:
            await self._teardown_sandbox(run_id)
        if self._events is not None:
            await self._events.agent_updated(run_id)

    async def _warm_refresh(self, repo_name: str) -> None:
        async with session_scope(self._engine) as session:
            store = PostgresGitStore(session)
            repo = await store.get_repo(repo_name)
            if repo is None:
                return
            await self._warm.refresh_repo(store, repo.id, repo.name)


class EventSink:
    async def agent_updated(self, run_id: uuid.UUID) -> None:
        return None

    async def main_updated(self) -> None:
        return None

    async def file_versions_created(self, versions: list[ProjectedVersion]) -> None:
        return None


@dataclass(frozen=True)
class RebaseWorkspace:
    session_id: str
    cwd: Path
    branch: str
    main_oid: bytes
    prompt: str | None
    old_base: bytes
    head: bytes | None
    claude_session_id: str | None
    keep_alive: bool


@dataclass(frozen=True)
class _CmdResult:
    returncode: int
    output: str


def _ensure_branch_checkout(cwd: Path, branch: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "checkout", "-B", branch],
        check=True,
        capture_output=True,
    )


def _fetch_main_into(cwd: Path, main_export: Path) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "fetch",
            str(main_export),
            "+refs/heads/main:refs/heads/main",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_rebase_onto_main(cwd: Path) -> _CmdResult:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "rebase", "main"],
        check=False,
        capture_output=True,
        text=True,
    )
    return _CmdResult(
        returncode=proc.returncode,
        output=(proc.stdout or "") + (proc.stderr or ""),
    )


def _git_rebase_abort(cwd: Path) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), "rebase", "--abort"],
        check=False,
        capture_output=True,
    )


def _commit_leftovers(cwd: Path) -> None:
    status = subprocess.run(
        ["git", "-C", str(cwd), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    )
    if not status.stdout.strip():
        return
    subprocess.run(["git", "-C", str(cwd), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(cwd), "commit", "-m", "agent sandbox edits"],
        check=True,
    )


async def _commit_tree(
    store: PostgresGitStore,
    repo_id: int,
    commit_oid: bytes,
) -> bytes | None:
    content = await store.get_object(repo_id, commit_oid)
    if content is None:
        return None
    _parents, tree = _parse_commit_links(content)
    return tree
