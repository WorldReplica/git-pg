from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from git_pg.db.engine import create_session_factory

_RESET_RATES_COLUMN_SQL = """
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'rates'
      AND column_name = 'rate'
      AND udt_name = 'float8'
  ) THEN
    ALTER TABLE rates
      ALTER COLUMN rate TYPE text USING rate::text;
  END IF;
END $$;
"""


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "integration: integration tests")
    config.addinivalue_line("markers", "benchmark: performance benchmarks")


@pytest.fixture(scope="session")
def database_url() -> str:
    return os.environ.get(
        "GIT_PG_DATABASE_URL",
        "postgresql+asyncpg://gitpg:gitpg@localhost:54329/gitpg",
    )


@pytest_asyncio.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(database_url)
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"Postgres not available: {exc}")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(autouse=True)
async def reset_db(request: pytest.FixtureRequest, engine: AsyncEngine) -> None:
    if request.node.get_closest_marker("integration") is None:
        return
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version ("
                "version_num character varying(32) NOT NULL PRIMARY KEY)"
            )
        )
        await conn.execute(text("TRUNCATE alembic_version"))
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('20260806120000')")
        )
        await conn.execute(text("TRUNCATE repositories CASCADE"))
        await conn.execute(text(_RESET_RATES_COLUMN_SQL))


@pytest_asyncio.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = create_session_factory(engine)
    async with factory() as session:
        yield session
        await session.rollback()
