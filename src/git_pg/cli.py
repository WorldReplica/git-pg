from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import typer
from rich import print as rprint

from git_pg.benchmark import (
    BENCHMARK_PRESETS,
    get_preset,
    run_session_lifecycle_benchmark,
)
from git_pg.models.repo import SessionStartRequest
from git_pg.orchestrator import Orchestrator
from git_pg.seed import SeedOptions

app = typer.Typer(no_args_is_help=True, help="Git-PG: Postgres-backed agent sandboxes")

DEFAULT_ALEMBIC_INI = Path("apps/demo-web/alembic.ini")


def _orch() -> Orchestrator:
    return Orchestrator()


@app.command("session")
def session_cmd(
    action: str = typer.Argument(help="start|stop|push"),
    repo: str = typer.Option("demo", help="Repository name"),
    ref: str = typer.Option("main", help="Git ref"),
    session_id: str | None = typer.Option(None, help="Session id"),
    cwd: str | None = typer.Option(None, help="Local path for push"),
) -> None:
    orchestrator = _orch()

    async def _run() -> None:
        if action == "start":
            handle = await orchestrator.session_start(
                SessionStartRequest(repo=repo, ref=ref, session_id=session_id)
            )
            rprint(
                f"session_id={handle.session_id} cwd={handle.cwd} "
                f"spin_up_ms={handle.spin_up_ms:.1f}"
            )
        elif action == "stop":
            if session_id is None:
                raise typer.BadParameter("session_id required for stop")
            orchestrator.session_stop(session_id)
            rprint(f"stopped session {session_id}")
        elif action == "push":
            if cwd is None:
                raise typer.BadParameter("cwd required for push")
            await orchestrator.session_push(repo, Path(cwd), ref=ref)
            rprint(f"pushed {repo} from {cwd}")
        else:
            raise typer.BadParameter(f"unknown action {action}")

    asyncio.run(_run())


@app.command("migrate")
def migrate_cmd(
    action: str = typer.Argument(help="apply|downgrade"),
    repo: str = typer.Option("demo", help="Repository name"),
    ref: str = typer.Option("main", help="Git ref"),
    alembic_ini: Path = typer.Option(
        DEFAULT_ALEMBIC_INI,
        help="Alembic config (alembic.ini)",
    ),
    revision: str | None = typer.Option(
        None,
        help="Target Alembic revision (apply: up to; downgrade: required)",
    ),
) -> None:
    if action not in {"apply", "downgrade"}:
        raise typer.BadParameter(f"unknown action {action}")

    async def _run() -> None:
        if action == "downgrade":
            if revision is None:
                raise typer.BadParameter("revision required for downgrade")
            result = await _orch().migrate_downgrade(
                repo,
                alembic_ini,
                target_revision=revision,
                ref=ref,
            )
        else:
            result = await _orch().migrate_apply(
                repo,
                alembic_ini,
                ref=ref,
                target_revision=revision,
            )
        if result is None:
            rprint("no pending migrations")
            return
        rprint(
            f"{action} {result.migration_revision}: "
            f"{result.new_commit.hex[:8]} (was {result.previous_commit.hex[:8]})"
        )

    asyncio.run(_run())


def _select_benchmark_preset() -> str:
    rprint("[bold]Select benchmark preset[/bold]\n")
    for i, preset in enumerate(BENCHMARK_PRESETS, start=1):
        clone = (
            f"main-only depth={preset.depth}"
            if preset.depth is not None
            else "main-only full"
        )
        blobs = f", blobs={','.join(preset.blobs)}" if preset.blobs else ""
        rprint(
            f"  [cyan]{i}[/cyan]  {preset.id} — {preset.description} ({clone}{blobs})"
        )
    rprint("")
    while True:
        raw = str(typer.prompt("Enter number or preset id", default="1"))
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(BENCHMARK_PRESETS):
                return BENCHMARK_PRESETS[idx - 1].id
        try:
            get_preset(raw)
            return raw
        except KeyError:
            rprint(f"[red]Invalid choice: {raw!r}[/red]")


@app.command("benchmark")
def benchmark_cmd(
    preset: str | None = typer.Option(
        None,
        "--preset",
        "-p",
        help=(
            "Preset id (hello, requests, requests-shallow, flask, flask-shallow, "
            "icons, twemoji-heavy)"
        ),
    ),
    repo: str | None = typer.Option(None, help="Postgres repo name override"),
) -> None:
    """Run session lifecycle benchmark (seed → spin-up → edit → push → re-spin-up)."""
    preset_id = preset or _select_benchmark_preset()
    bench_preset = get_preset(preset_id)

    async def _run() -> None:
        from git_pg.db.engine import create_engine

        engine = create_engine()
        await run_session_lifecycle_benchmark(
            engine,
            bench_preset,
            repo_name=repo,
            log=True,
        )
        await engine.dispose()

    asyncio.run(_run())


@app.command("seed")
def seed_cmd(
    url: str = typer.Option(..., help="Public git URL"),
    repo: str = typer.Option(..., help="Target repo name in Postgres"),
    depth: int | None = typer.Option(None, help="Shallow clone depth"),
    blobs: str = typer.Option("", help="Comma-separated blob sizes e.g. 10mb,50mb"),
) -> None:
    blob_specs = tuple(b.strip() for b in blobs.split(",") if b.strip())

    async def _run() -> None:
        t0 = time.perf_counter()
        result = await _orch().seed(
            SeedOptions(url=url, repo=repo, depth=depth, blobs=blob_specs)
        )
        report = {
            "repo": result.repo,
            "object_count": result.object_count,
            "total_blob_bytes": result.total_blob_bytes,
            "seed_push_s": result.duration_s,
            "wall_s": time.perf_counter() - t0,
        }
        rprint(json.dumps(report, indent=2))

    asyncio.run(_run())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
