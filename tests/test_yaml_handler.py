from __future__ import annotations

import pytest
from sqlalchemy import delete, select

from git_pg.db.engine import session_scope
from git_pg.db.orm.models import AppConfig, Repository, SpecialRule
from git_pg.models.repo import RefName
from git_pg.special.yaml_app_config import YamlAppConfigHandler
from git_pg.store.postgres import PostgresGitStore
from git_pg.sync import push_and_ingest


@pytest.mark.integration
async def test_yaml_app_config_ingest_materialize(engine) -> None:
    import subprocess
    import tempfile
    from pathlib import Path

    repo_name = "test-yaml"
    yaml_content = "name: demo\nport: 8080\n"
    tmp = Path(tempfile.mkdtemp(prefix="git-pg-yaml-"))
    subprocess.run(["git", "init", "-b", "main", str(tmp)], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@l"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "t"], check=True)
    (tmp / "config.yaml").write_text(yaml_content)
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-m", "init"], check=True)

    async with session_scope(engine) as session:
        await session.execute(delete(Repository).where(Repository.name == repo_name))
        await session.flush()
        store = PostgresGitStore(session)
        repo = await store.get_or_create_repo(repo_name)
        session.add(
            SpecialRule(repo_id=repo.id, path="config.yaml", handler="yaml:app_config")
        )
        await session.flush()
        await push_and_ingest(
            session, repo_name, tmp, RefName(value="main"), allow_main=True
        )

        row = await session.get(AppConfig, repo.id)
        assert row is not None
        assert row.name == "demo"
        assert row.port == 8080

        handler = YamlAppConfigHandler()
        blob = await handler.materialize(session, repo.id, "config.yaml")
        assert b"name: demo" in blob
        assert b"port: 8080" in blob

        result = await session.execute(
            select(AppConfig).where(AppConfig.repo_id == repo.id)
        )
        assert result.scalar_one() is not None
