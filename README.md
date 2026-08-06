# Git-PG

Postgres-backed git repositories for sandboxed Claude agents, with special-file table projections and Alembic migrations.

See [docs/SPEC.md](docs/SPEC.md) for architecture.

## Quick start

```bash
docker compose up -d
uv sync --dev
uv run pytest
```

## CLI

```bash
uv run git-pg session start --repo demo --ref main
uv run git-pg session push --repo demo --cwd /path/to/repo
uv run git-pg migrate apply --repo demo
# Or via pytest (preset: hello | requests | flask | icons | twemoji-heavy)
BENCHMARK=1 BENCHMARK_PRESET=hello uv run pytest tests/integration/test_benchmark.py -v -s

# Interactive benchmark picker
uv run git-pg benchmark
uv run git-pg benchmark --preset requests
```

## Migrations

Schema migrations live in `apps/demo-web/alembic/versions/` and are applied with **Alembic** (not hand-rolled SQL).

```bash
# Platform deploy path (Alembic upgrade + git blob rematerialize)
uv run python apps/demo-web/src/deploy.py

# Or via git-pg CLI (same transaction: Alembic + rematerialize)
uv run git-pg migrate apply --repo demo --revision 20260806120001

# Standalone Alembic CLI (schema only — no git rematerialize)
cd apps/demo-web && uv run alembic -c alembic.ini upgrade head
```

After each Alembic revision, `git-pg` rematerializes special-file git blobs from Postgres tables and writes a migration-authored commit.
