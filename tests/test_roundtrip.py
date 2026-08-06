from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from git_pg.config import Settings
from git_pg.db.engine import session_scope
from git_pg.db.orm.models import Rate, Repository, SpecialRule
from git_pg.models.repo import RefName, SessionStartRequest
from git_pg.session import SessionManager
from git_pg.store.postgres import PostgresGitStore
from git_pg.sync import push_and_ingest


def _init_local_repo(files: dict[str, str]) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="git-pg-test-"))
    subprocess.run(["git", "init", "-b", "main", str(tmp)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp), "config", "user.email", "test@git-pg.local"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp), "config", "user.name", "git-pg test"],
        check=True,
    )
    for rel, content in files.items():
        path = tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp), "commit", "-m", "initial"],
        check=True,
    )
    return tmp


def _test_settings(prefix: str) -> Settings:
    root = Path(tempfile.mkdtemp(prefix=prefix))
    return Settings(
        sessions_root=str(root / "sessions"),
        warm_cache_root=str(root / "warm"),
    )


@pytest.mark.integration
async def test_roundtrip_push_and_session_start(engine) -> None:
    repo_name = "test-roundtrip"
    local = _init_local_repo({"hello.txt": "world\n"})
    settings = _test_settings("git-pg-roundtrip-")

    async with session_scope(engine) as session:
        await session.execute(delete(Repository).where(Repository.name == repo_name))
        await session.flush()
        await push_and_ingest(
            session, repo_name, local, RefName(value="main"), allow_main=True
        )

    mgr = SessionManager(engine, settings)
    handle = await mgr.start(SessionStartRequest(repo=repo_name, ref="main"))
    assert (Path(handle.cwd) / "hello.txt").read_text() == "world\n"
    state_path = Path(settings.warm_cache_root) / repo_name / "state.json"
    payload = json.loads(state_path.read_text())
    assert payload["status"] == "fresh"
    assert (Path(settings.warm_cache_root) / repo_name / "current").exists()
    mgr.stop(handle.session_id)


@pytest.mark.integration
async def test_special_csv_ingest(engine) -> None:
    repo_name = "test-csv"
    csv_content = "name,rate\nalpha,12.5%\nbeta,3%\n"
    local = _init_local_repo({"data/rates.csv": csv_content})

    async with session_scope(engine) as session:
        await session.execute(delete(Repository).where(Repository.name == repo_name))
        await session.flush()
        store = PostgresGitStore(session)
        repo = await store.get_or_create_repo(repo_name)
        session.add(
            SpecialRule(repo_id=repo.id, path="data/rates.csv", handler="csv:rates")
        )
        await session.flush()
        await push_and_ingest(
            session, repo_name, local, RefName(value="main"), allow_main=True
        )

        result = await session.execute(select(Rate).where(Rate.repo_id == repo.id))
        rows = list(result.scalars().all())
        assert len(rows) == 2
        assert rows[0].rate == "12.5%"
