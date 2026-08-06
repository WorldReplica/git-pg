from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import delete

from git_pg.config import Settings
from git_pg.db.engine import session_scope
from git_pg.db.orm.models import Repository
from git_pg.models.repo import RefName, SessionStartRequest
from git_pg.session import SessionManager
from git_pg.sync import push_and_ingest


def _init_local_repo(files: dict[str, str]) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="git-pg-agent-"))
    subprocess.run(["git", "init", "-b", "main", str(tmp)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp), "config", "user.email", "agent@git-pg.local"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp), "config", "user.name", "git-pg agent"],
        check=True,
    )
    for rel, content in files.items():
        path = tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-m", "initial"], check=True)
    return tmp


def _test_settings(prefix: str) -> Settings:
    root = Path(tempfile.mkdtemp(prefix=prefix))
    return Settings(
        sessions_root=str(root / "sessions"),
        warm_cache_root=str(root / "warm"),
    )


@pytest.mark.integration
async def test_agent_edit_commit_push_roundtrip(engine) -> None:
    """Simulate agent edit → commit → push → fresh session start sees change."""
    repo_name = "test-agent-sync"
    local = _init_local_repo({"notes.txt": "v1\n"})
    settings = _test_settings("git-pg-agent-sync-")

    async with session_scope(engine) as session:
        await session.execute(delete(Repository).where(Repository.name == repo_name))
        await session.flush()
        await push_and_ingest(
            session, repo_name, local, RefName(value="main"), allow_main=True
        )

    mgr = SessionManager(engine, settings)
    handle = await mgr.start(SessionStartRequest(repo=repo_name, ref="main"))
    assert (Path(handle.cwd) / "notes.txt").read_text() == "v1\n"
    warm_root = Path(settings.warm_cache_root) / repo_name
    first_state = json.loads((warm_root / "state.json").read_text())
    assert first_state["status"] == "fresh"
    first_current = (warm_root / "current").resolve()

    notes = Path(handle.cwd) / "notes.txt"
    notes.write_text("v2\n")
    subprocess.run(["git", "-C", handle.cwd, "add", "notes.txt"], check=True)
    subprocess.run(
        ["git", "-C", handle.cwd, "commit", "-m", "agent edit"],
        check=True,
    )

    async with session_scope(engine) as session:
        await push_and_ingest(
            session,
            repo_name,
            Path(handle.cwd),
            RefName(value="main"),
            allow_main=True,
        )
    mgr.stop(handle.session_id)

    handle2 = await mgr.start(SessionStartRequest(repo=repo_name, ref="main"))
    assert (Path(handle2.cwd) / "notes.txt").read_text() == "v2\n"
    second_state = json.loads((warm_root / "state.json").read_text())
    assert second_state["status"] == "fresh"
    assert (warm_root / "current").resolve() != first_current
    log = subprocess.run(
        ["git", "-C", handle2.cwd, "log", "-1", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert log == "agent edit"
    mgr.stop(handle2.session_id)
