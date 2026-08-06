"""Deploy: Alembic upgrade + git_pg rematerialize (via Orchestrator)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from git_pg.orchestrator import Orchestrator


async def deploy(repo: str = "demo", ref: str = "main") -> None:
    root = Path(__file__).resolve().parents[1]
    alembic_ini = root / "alembic.ini"
    orchestrator = Orchestrator()
    result = await orchestrator.migrate_apply(repo, alembic_ini, ref=ref)
    if result is None:
        print("no pending migrations")
        return
    print(f"applied {result.migration_revision}: {result.migration_message}")


def main() -> None:
    asyncio.run(deploy())


if __name__ == "__main__":
    main()
