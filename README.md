# Git-PG

Postgres-backed git for sandboxed Claude agents: real working trees, special-file SQL tables (Alembic), durable UUID file versions, and an approve gate so agents never write `main`.

Architecture details: [docs/SPEC.md](docs/SPEC.md).

## Quick start

```bash
docker compose up -d postgres
uv sync --dev
cd apps/demo-web && uv run alembic -c alembic.ini upgrade head && cd ../..
uv run pytest -m "not benchmark"
```

Postgres listens on **localhost:54329** (`gitpg` / `gitpg` / db `gitpg`).

## Demo web (file versions + agent approve)

**Terminal 1 — API** (seeds the `demo` repo; WebSocket at `/api/ws`):

```bash
docker compose up -d postgres
cd apps/demo-web && uv run alembic -c alembic.ini upgrade head && cd ../..
docker build -t git-pg-agent:local -f docker/agent/Dockerfile .
cp -n .env.example .env   # set ANTHROPIC_API_KEY
mkdir -p /tmp/git-pg/sessions /tmp/git-pg/warm
GIT_PG_CORS_ORIGINS='["http://localhost:3010","http://127.0.0.1:3010"]' \
PYTHONPATH=apps/demo-web \
uv run uvicorn api.app:app --reload --reload-dir apps/demo-web/api --reload-exclude '.env' \
  --app-dir apps/demo-web --host 127.0.0.1 --port 8001
```

**Terminal 2 — UI** (Rsbuild + TanStack Query; proxies `/api` → `http://127.0.0.1:8001`):

```bash
corepack enable
pnpm --dir apps/demo-web/web install
pnpm --dir apps/demo-web/web dev
```

Open **http://localhost:3010**:

- Browse `main`’s file tree, version history, and comments (pinned to version UUIDs).
- Spawn agents (Docker + Claude). Spawn returns immediately; task gen + sandbox continue in the background (`agent.updated` over WS).
- Expand a card for prompt, commits, and diff; approve / reject; delete non-approved runs (trash).
- When `main` moved under a waiting agent, the UI shows **stale base** and **Rebase onto main**.

Rebuild the agent image after pulling so resume support is present:

```bash
docker build -t git-pg-agent:local -f docker/agent/Dockerfile .
```

### Concurrent agents and approve

Agents push only `agent/<id>` branches. Approve is a **fast-forward** of `main` from the run’s spawn base. Parallel agents that branched from the same tip are fine until the first approve lands.

**Rebase strategy** toggle (`auto` | `agent`, remembered in `localStorage` as `git-pg.rebaseStrategy`):

| Strategy | Behavior |
|---|---|
| **Auto** | Mechanical `git rebase main` in the warm workspace (cold clone fallback). Conflict → `failed`. |
| **Agent** | Same warm workspace + resumed Claude session (`claude_session_id`) reconciles onto new `main`. |

On approve, other same-repo `awaiting_approval` runs fan-out-rebase immediately. Siblings still `running` get a pending rebase applied when they finish if `main` moved.

While awaiting approval the **workspace stays warm** (checkout + Claude transcript under `agent-home/`). Tear down on approve / reject / fail / delete.

### Env

Copy `.env.example` → `.env`:

```bash
ANTHROPIC_API_KEY=…
# optional:
# GIT_PG_DATABASE_URL=postgresql+asyncpg://gitpg:gitpg@localhost:54329/gitpg
# GIT_PG_AGENT_DOCKER_IMAGE=git-pg-agent:local
# GIT_PG_TASK_GEN_MODEL=claude-haiku-4-5-20251001
```

### OpenAPI → TypeScript

```bash
PYTHONPATH=apps/demo-web uv run python -c "from api.app import create_app; import json; json.dump(create_app().openapi(), open('apps/demo-web/web/openapi.json','w'), indent=2)"
pnpm --dir apps/demo-web/web gen:api
```

## CLI

```bash
uv run git-pg session start --repo demo --ref main
uv run git-pg session push --repo demo --cwd /path/to/repo
uv run git-pg migrate apply --repo demo
uv run git-pg seed --url https://github.com/pallets/flask.git --repo bench-flask --depth 1
BENCHMARK=1 BENCHMARK_PRESET=hello uv run pytest tests/integration/test_benchmark.py -v -s
uv run git-pg benchmark --preset flask
```

Session start uses the **warm cache** by default (`GIT_PG_WARM_CACHE_ENABLED`, root `GIT_PG_WARM_CACHE_ROOT`).

## Migrations

Schema lives in `apps/demo-web/alembic/versions/` (platform app — not inside sandbox repos).

```bash
cd apps/demo-web && uv run alembic -c alembic.ini upgrade head
uv run git-pg migrate apply --repo demo          # Alembic + rematerialize commit
uv run python apps/demo-web/src/deploy.py        # deploy helper
```

Product tables: `file_versions` / `file_comments` / `agent_runs` — versions project when `main` advances; comments FK version UUIDs with no cascade delete.

## Ports

| Service | Port |
|---|---|
| Postgres (compose) | **54329** |
| Demo API (local uvicorn) | **8001** |
| Demo UI | **3010** |
| Compose `api` service (optional) | 8000 |
