from __future__ import annotations

import asyncio
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine

from git_pg.db.engine import session_scope
from git_pg.store.postgres import PostgresGitStore


@dataclass(frozen=True)
class SeedOptions:
    url: str
    repo: str
    depth: int | None = None
    blobs: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeedResult:
    repo: str
    object_count: int
    total_blob_bytes: int
    duration_s: float


def _build_clone_cmd(
    url: str,
    dest: Path,
    *,
    depth: int | None,
    bare: bool,
) -> list[str]:
    """Clone a single branch (remote HEAD), never all refs/pull requests."""
    cmd = ["git", "clone", "--single-branch"]
    if bare:
        cmd.append("--bare")
    if depth is not None:
        cmd.append(f"--depth={depth}")
    cmd.extend([url, str(dest)])
    return cmd


async def seed_repo(engine: AsyncEngine, options: SeedOptions) -> SeedResult:
    import time

    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        needs_worktree = bool(options.blobs)
        if needs_worktree:
            work = Path(tmp) / "repo"
            clone_cmd = _build_clone_cmd(
                options.url, work, depth=options.depth, bare=False
            )
        else:
            work = Path(tmp) / "repo.git"
            clone_cmd = _build_clone_cmd(
                options.url, work, depth=options.depth, bare=True
            )
        await asyncio.to_thread(subprocess.run, clone_cmd, check=True)

        if options.blobs:
            await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(work), "config", "user.email", "seed@git-pg.local"],
                check=True,
            )
            await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(work), "config", "user.name", "git-pg seed"],
                check=True,
            )
        for spec in options.blobs:
            size = _parse_blob_size(spec)
            blob_path = work / "bench" / "blobs" / f"{spec}.bin"
            blob_path.parent.mkdir(parents=True, exist_ok=True)
            blob_path.write_bytes(_random_bytes(size))
            await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(work), "add", str(blob_path.relative_to(work))],
                check=True,
            )
            await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(work), "commit", "-m", f"add benchmark blob {spec}"],
                check=True,
            )

        async with session_scope(engine) as session:
            store = PostgresGitStore(session)
            repo = await store.get_or_create_repo(options.repo)
            await store.push_from_local(repo.id, work, allow_main=True)
            count = await store.count_objects(repo.id)
            blob_bytes = await store.total_blob_bytes(repo.id)

    return SeedResult(
        repo=options.repo,
        object_count=count,
        total_blob_bytes=blob_bytes,
        duration_s=time.perf_counter() - t0,
    )


def _parse_blob_size(spec: str) -> int:
    if spec.endswith("mb"):
        return int(spec[:-2]) * 1024 * 1024
    if spec.endswith("kb"):
        return int(spec[:-2]) * 1024
    return int(spec)


def _random_bytes(size: int) -> bytes:
    import os

    return os.urandom(size)
