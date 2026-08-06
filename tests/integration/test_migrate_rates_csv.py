from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import ClassVar

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from git_pg.config import get_settings
from git_pg.db.engine import session_scope
from git_pg.db.orm.models import Repository, SpecialRule
from git_pg.migrate import apply_migrations, downgrade_migrations
from git_pg.models.repo import RefName, SessionStartRequest
from git_pg.session import SessionManager
from git_pg.special.registry import HandlerRegistry
from git_pg.store.postgres import PostgresGitStore
from git_pg.sync import push_and_ingest

ALEMBIC_INI = Path("apps/demo-web/alembic.ini")
BASELINE_REVISION = "20260806120000"
RATES_REVISION = "20260806120001"


def _init_rates_repo() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="git-pg-migrate-"))
    csv_content = "name,rate\nalpha,12.5%\nbeta,3%\n"
    (tmp / "data").mkdir(parents=True)
    (tmp / "data" / "rates.csv").write_text(csv_content)
    subprocess.run(["git", "init", "-b", "main", str(tmp)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp), "config", "user.email", "test@git-pg.local"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp), "config", "user.name", "git-pg test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(tmp), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-m", "initial rates"], check=True)
    return tmp


async def _seed_rates_repo(engine, repo_name: str) -> str:
    """Push rates CSV repo; return pre-migrate HEAD hex."""
    local = _init_rates_repo()
    async with session_scope(engine) as session:
        await session.execute(delete(Repository).where(Repository.name == repo_name))
        await session.flush()
        store = PostgresGitStore(session)
        repo = await store.get_or_create_repo(repo_name)
        session.add(
            SpecialRule(repo_id=repo.id, path="data/rates.csv", handler="csv:rates")
        )
        await session.flush()
        await push_and_ingest(session, repo_name, local, RefName(value="main"))
        oid = await store.get_ref_oid(repo.id, RefName(value="main"))
        assert oid is not None
        return oid.hex()


async def _rate_column_type(engine) -> str:
    async with session_scope(engine) as session:
        col = await session.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'rates' AND column_name = 'rate'"
            )
        )
        return str(col.scalar_one())


async def _alembic_version(engine) -> str | None:
    async with session_scope(engine) as session:
        result = await session.execute(
            text("SELECT version_num FROM alembic_version LIMIT 1")
        )
        return result.scalar_one_or_none()


@pytest.mark.integration
async def test_migrate_rates_csv_roundtrip(engine) -> None:
    repo_name = "test-migrate-rates"
    pre_migrate = await _seed_rates_repo(engine, repo_name)

    mgr = SessionManager(engine)
    handle = await mgr.start(SessionStartRequest(repo=repo_name, ref="main"))
    assert "12.5%" in (Path(handle.cwd) / "data" / "rates.csv").read_text()
    mgr.stop(handle.session_id)

    async with session_scope(engine) as session:
        result = await apply_migrations(
            session,
            repo_name,
            RefName(value="main"),
            ALEMBIC_INI,
            target_revision=RATES_REVISION,
            settings=get_settings(),
        )
        assert result is not None
        assert result.migration_revision == RATES_REVISION
        assert result.previous_commit.hex == pre_migrate
        assert result.new_commit.hex != pre_migrate

    handle2 = await mgr.start(SessionStartRequest(repo=repo_name, ref="main"))
    csv_text = (Path(handle2.cwd) / "data" / "rates.csv").read_text()
    assert "0.125" in csv_text
    assert "0.03" in csv_text
    assert "%" not in csv_text

    log = subprocess.run(
        ["git", "-C", handle2.cwd, "log", "-1", "--format=%an <%ae>%n%s"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    settings = get_settings()
    assert settings.migration_author_name in log
    assert settings.migration_author_email in log
    assert "rates_pct_to_float" in log

    parent = subprocess.run(
        ["git", "-C", handle2.cwd, "rev-parse", "HEAD^"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert parent == pre_migrate
    assert await _rate_column_type(engine) == "double precision"
    mgr.stop(handle2.session_id)


@pytest.mark.integration
async def test_migrate_downgrade_restores_percentage_csv(engine) -> None:
    """Upgrade → rematerialize floats; downgrade → new rematerialize commit with %."""
    repo_name = "test-migrate-downgrade"
    pre_migrate = await _seed_rates_repo(engine, repo_name)

    async with session_scope(engine) as session:
        up = await apply_migrations(
            session,
            repo_name,
            RefName(value="main"),
            ALEMBIC_INI,
            target_revision=RATES_REVISION,
            settings=get_settings(),
        )
        assert up is not None
        upgrade_commit = up.new_commit.hex

    async with session_scope(engine) as session:
        down = await downgrade_migrations(
            session,
            repo_name,
            RefName(value="main"),
            ALEMBIC_INI,
            target_revision=BASELINE_REVISION,
            settings=get_settings(),
        )
        assert down is not None
        assert down.migration_revision == BASELINE_REVISION
        assert down.previous_commit.hex == upgrade_commit
        downgrade_commit = down.new_commit.hex

    assert await _rate_column_type(engine) == "text"
    assert await _alembic_version(engine) == BASELINE_REVISION

    mgr = SessionManager(engine)
    handle = await mgr.start(SessionStartRequest(repo=repo_name, ref="main"))
    csv_text = (Path(handle.cwd) / "data" / "rates.csv").read_text()
    assert "12.5%" in csv_text
    assert "3%" in csv_text

    head = subprocess.run(
        ["git", "-C", handle.cwd, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == downgrade_commit

    # Append-only history: C0 (initial) → C1 (upgrade floats) → C2 (downgrade %)
    parents = (
        subprocess.run(
            ["git", "-C", handle.cwd, "rev-list", "--max-count=3", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .splitlines()
    )
    assert parents[0] == downgrade_commit
    assert parents[1] == upgrade_commit
    assert parents[2] == pre_migrate

    log = subprocess.run(
        ["git", "-C", handle.cwd, "log", "-1", "--format=%s"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert "downgrade" in log
    assert BASELINE_REVISION in log
    mgr.stop(handle.session_id)


class _BoomHandler:
    """Handler that fails during rematerialize to simulate mid-migration failure."""

    handler_id: ClassVar[str] = "csv:rates"
    table_names: ClassVar[tuple[str, ...]] = ("rates",)

    async def ingest(
        self,
        session: AsyncSession,
        repo_id: int,
        path: str,
        blob: bytes,
    ) -> None:
        del session, repo_id, path, blob

    async def materialize(
        self,
        session: AsyncSession,
        repo_id: int,
        path: str,
    ) -> bytes:
        del session, repo_id, path
        msg = "simulated rematerialize failure"
        raise RuntimeError(msg)


@pytest.mark.integration
async def test_migrate_failure_rolls_back(engine) -> None:
    """If rematerialize fails after Alembic DDL, roll back schema + refs + version."""
    repo_name = "test-migrate-fail"
    pre_migrate = await _seed_rates_repo(engine, repo_name)

    boom = HandlerRegistry()
    boom.register(_BoomHandler())

    with pytest.raises(RuntimeError, match="simulated rematerialize failure"):
        async with session_scope(engine) as session:
            await apply_migrations(
                session,
                repo_name,
                RefName(value="main"),
                ALEMBIC_INI,
                target_revision=RATES_REVISION,
                registry=boom,
                settings=get_settings(),
            )

    assert await _rate_column_type(engine) == "text"
    assert await _alembic_version(engine) == BASELINE_REVISION

    mgr = SessionManager(engine)
    handle = await mgr.start(SessionStartRequest(repo=repo_name, ref="main"))
    head = subprocess.run(
        ["git", "-C", handle.cwd, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert head == pre_migrate
    assert "12.5%" in (Path(handle.cwd) / "data" / "rates.csv").read_text()
    mgr.stop(handle.session_id)
