# Git-PG

Postgres-backed git repositories for sandboxed Claude agents, with special-file table projections, durable file-version UUIDs, and Alembic migrations.

See [docs/SPEC.md](docs/SPEC.md) for architecture.

## Quick start

```bash
docker compose up -d postgres
uv sync --dev
cd apps/demo-web && uv run alembic -c alembic.ini upgrade head && cd ../..
uv run pytest
```

## Demo web (file versions + agent approve)

Terminal 1 — API (seeds the `demo` repo, WebSocket at `/api/ws`):

```bash
docker compose up -d postgres
cd apps/demo-web && uv run alembic -c alembic.ini upgrade head && cd ../..
# optional: build the agent image for real Claude sandboxes
docker build -t git-pg-agent:local -f docker/agent/Dockerfile .
cp -n .env.example .env   # then set ANTHROPIC_API_KEY in .env
PYTHONPATH=apps/demo-web uv run uvicorn api.app:app --reload --reload-dir apps/demo-web/api --reload-exclude '.env' --app-dir apps/demo-web --port 8001
```

Terminal 2 — React UI (Rsbuild + TanStack Query; proxies `/api` to port 8000):

```bash
corepack enable
pnpm --dir apps/demo-web/web install
pnpm --dir apps/demo-web/web dev
```

Open http://localhost:3010 — browse main’s file tree and version history, comment on a version UUID, spawn agents (Docker + Claude), preview diffs, approve/reject. Non-approved agent cards show a trash control to delete the session (kills container/workspace when present); approved runs cannot be deleted.

### Concurrent agents and approve

Agents never write `main`; approve is a **fast-forward only** merge from the spawn base tip. Parallel agents that branched from the same `main` are fine until the first approve.

The Agents panel has a **rebase strategy** toggle (`auto` | `agent`). When you approve one run, every other same-repo `awaiting_approval` run is immediately fan-out-rebased onto the new `main`:

- **Auto rebase** — status `auto_rebasing`, mechanical `git rebase main`; on success returns to `awaiting_approval` (base updated); on conflict → `failed`.
- **Agent rebase** — status `agent_rebasing`, same warm workspace + resumed Claude session reconciles onto new `main`; then `awaiting_approval` or `failed`. Cold clone is only a fallback if the warm tree is gone.

**Spawn** returns immediately with a `running` card; task generation + sandbox + Docker continue in the background and push `agent.updated` over WebSocket.

Live updates use WebSocket (`agent.updated` when rebases start and when each finishes). Approving a rebased run uses the same FF gate (its `base_commit` must match current `main`).

While awaiting approval the agent **workspace stays warm** (Claude session id + checkout on disk). Rebuild the agent image after pulling so resume support is present: `docker build -t git-pg-agent:local -f docker/agent/Dockerfile .`

Regenerate OpenAPI TypeScript types after API schema changes:

```bash
PYTHONPATH=apps/demo-web uv run python -c "from api.app import create_app; import json; json.dump(create_app().openapi(), open('apps/demo-web/web/openapi.json','w'), indent=2)"
pnpm --dir apps/demo-web/web gen:api
```

## CLI

```bash
uv run git-pg session start --repo demo --ref main
uv run git-pg session push --repo demo --cwd /path/to/repo
uv run git-pg migrate apply --repo demo
BENCHMARK=1 BENCHMARK_PRESET=hello uv run pytest tests/integration/test_benchmark.py -v -s
uv run git-pg benchmark
uv run git-pg benchmark --preset requests
```

## Migrations

Schema migrations live in `apps/demo-web/alembic/versions/` (Alembic).

```bash
uv run python apps/demo-web/src/deploy.py
uv run git-pg migrate apply --repo demo --revision 20260806120001
cd apps/demo-web && uv run alembic -c alembic.ini upgrade head
```

`file_versions` / `file_comments` / `agent_runs` are product tables: versions are projected when `main` advances; comments FK to version UUIDs with no cascade delete.
