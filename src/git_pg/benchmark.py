from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine

from git_pg.db.engine import session_scope
from git_pg.db.orm.models import GitRef, Repository
from git_pg.models.repo import SessionStartRequest
from git_pg.orchestrator import Orchestrator
from git_pg.seed import SeedOptions
from git_pg.store.postgres import PostgresGitStore


@dataclass(frozen=True)
class BenchmarkPreset:
    id: str
    name: str
    description: str
    url: str
    depth: int | None = None
    blobs: tuple[str, ...] = ()


BENCHMARK_PRESETS: tuple[BenchmarkPreset, ...] = (
    BenchmarkPreset(
        id="hello",
        name="Hello-World",
        description="Tiny code repo, shallow clone. Fast smoke test.",
        url="https://github.com/octocat/Hello-World.git",
        depth=1,
    ),
    BenchmarkPreset(
        id="requests",
        name="requests",
        description="Normal Python library, main branch full history. Mostly text.",
        url="https://github.com/psf/requests.git",
    ),
    BenchmarkPreset(
        id="requests-shallow",
        name="requests (shallow)",
        description="Normal Python library, shallow clone. Faster baseline.",
        url="https://github.com/psf/requests.git",
        depth=1,
    ),
    BenchmarkPreset(
        id="flask",
        name="Flask",
        description="Small web framework, main branch full history.",
        url="https://github.com/pallets/flask.git",
    ),
    BenchmarkPreset(
        id="flask-shallow",
        name="Flask (shallow)",
        description="Small web framework, shallow clone. Faster baseline.",
        url="https://github.com/pallets/flask.git",
        depth=1,
    ),
    BenchmarkPreset(
        id="icons",
        name="SuperTinyIcons",
        description="Many small PNG/SVG binaries. Shallow clone.",
        url="https://github.com/edent/SuperTinyIcons.git",
        depth=1,
    ),
    BenchmarkPreset(
        id="twemoji-heavy",
        name="twemoji + blobs",
        description="Large emoji PNG repo + 10MB/50MB synthetic blobs (slow).",
        url="https://github.com/twitter/twemoji.git",
        depth=1,
        blobs=("10mb", "50mb"),
    ),
)


def get_preset(preset_id: str) -> BenchmarkPreset:
    for preset in BENCHMARK_PRESETS:
        if preset.id == preset_id:
            return preset
    ids = ", ".join(p.id for p in BENCHMARK_PRESETS)
    msg = f"unknown benchmark preset {preset_id!r}; choose from: {ids}"
    raise KeyError(msg)


def _is_binary_sample(sample: bytes) -> bool:
    return b"\x00" in sample


def catalogue_workdir(root: Path) -> dict[str, int]:
    text_files = 0
    binary_files = 0
    text_bytes = 0
    binary_bytes = 0
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        size = path.stat().st_size
        sample = path.read_bytes()[:8192]
        if _is_binary_sample(sample):
            binary_files += 1
            binary_bytes += size
        else:
            text_files += 1
            text_bytes += size
    return {
        "text_files": text_files,
        "binary_files": binary_files,
        "total_files": text_files + binary_files,
        "text_bytes": text_bytes,
        "binary_bytes": binary_bytes,
        "total_bytes": text_bytes + binary_bytes,
    }


async def run_session_lifecycle_benchmark(
    engine: AsyncEngine,
    preset: BenchmarkPreset,
    *,
    repo_name: str | None = None,
    log: bool = True,
) -> dict[str, object]:
    if preset.depth is None and preset.blobs:
        msg = "blobs require a working-tree clone (depth set)"
        raise ValueError(msg)

    bench_repo = repo_name or f"bench-{preset.id}"
    orchestrator = Orchestrator()

    async with session_scope(engine) as session:
        await session.execute(delete(Repository).where(Repository.name == bench_repo))
        await session.flush()

    def emit(payload: dict[str, object]) -> None:
        if log:
            print(json.dumps(payload, indent=2), flush=True)

    emit(
        {
            "phase": "seed_start",
            "preset": preset.id,
            "url": preset.url,
            "depth": preset.depth,
            "blobs": list(preset.blobs),
            "clone": (
                f"main-only-shallow-{preset.depth}"
                if preset.depth is not None
                else "main-only-full"
            ),
        }
    )

    t0 = time.perf_counter()
    seed_result = await orchestrator.seed(
        SeedOptions(
            url=preset.url,
            repo=bench_repo,
            depth=preset.depth,
            blobs=preset.blobs,
        )
    )
    seed_push_s = time.perf_counter() - t0
    emit(
        {
            "phase": "seed_done",
            "seed_push_s": seed_push_s,
            "object_count": seed_result.object_count,
            "total_blob_bytes": seed_result.total_blob_bytes,
        }
    )

    async with session_scope(engine) as session:
        store = PostgresGitStore(session)
        repo = await store.get_repo(bench_repo)
        if repo is None:
            msg = f"benchmark repo {bench_repo!r} missing after seed"
            raise LookupError(msg)
        refs = await session.execute(
            select(GitRef.name).where(
                GitRef.repo_id == repo.id,
                GitRef.name.like("refs/heads/%"),
            )
        )
        head_names = [n.removeprefix("refs/heads/") for n in refs.scalars().all()]

    ref_name = "main" if "main" in head_names else head_names[0]

    t1 = time.perf_counter()
    handle = await orchestrator.session_start(
        SessionStartRequest(repo=bench_repo, ref=ref_name)
    )
    spin_up_ms = (time.perf_counter() - t1) * 1000

    file_catalogue = catalogue_workdir(Path(handle.cwd))
    emit({"phase": "catalogue", "ref": ref_name, **file_catalogue})

    edit_path = Path(handle.cwd) / "README.md"
    if not edit_path.exists():
        edit_path = Path(handle.cwd) / "README.rst"
    if not edit_path.exists():
        edit_path = Path(handle.cwd) / "GIT_PG_BENCH.txt"
        edit_path.write_text("bench\n")
    else:
        edit_path.write_text(edit_path.read_text() + "\n# git-pg bench edit\n")

    subprocess.run(
        ["git", "-C", handle.cwd, "config", "user.email", "bench@git-pg.local"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", handle.cwd, "config", "user.name", "git-pg bench"],
        check=True,
    )
    subprocess.run(["git", "-C", handle.cwd, "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", handle.cwd, "commit", "-m", "agent edit"],
        check=True,
    )

    blob_sizes: dict[str, int] = {}
    for spec in preset.blobs:
        blob = Path(handle.cwd) / "bench" / "blobs" / f"{spec}.bin"
        if not blob.exists():
            msg = f"missing injected blob {spec}"
            raise AssertionError(msg)
        blob_sizes[spec] = blob.stat().st_size

    t2 = time.perf_counter()
    await orchestrator.session_push(
        bench_repo, Path(handle.cwd), ref=ref_name, allow_main=True
    )
    commit_push_ms = (time.perf_counter() - t2) * 1000

    t3 = time.perf_counter()
    orchestrator.session_stop(handle.session_id)
    teardown_ms = (time.perf_counter() - t3) * 1000

    t4 = time.perf_counter()
    handle2 = await orchestrator.session_start(
        SessionStartRequest(repo=bench_repo, ref=ref_name)
    )
    re_spin_up_ms = (time.perf_counter() - t4) * 1000

    verify_path = Path(handle2.cwd) / edit_path.name
    if edit_path.name in {"README.md", "README.rst"}:
        assert "git-pg bench edit" in verify_path.read_text()
    else:
        assert verify_path.exists()
    for spec, expected_size in blob_sizes.items():
        blob2 = Path(handle2.cwd) / "bench" / "blobs" / f"{spec}.bin"
        assert blob2.stat().st_size == expected_size

    orchestrator.session_stop(handle2.session_id)

    report: dict[str, object] = {
        "preset": preset.id,
        "name": preset.name,
        "url": preset.url,
        "depth": preset.depth,
        "ref": ref_name,
        "blobs": list(preset.blobs),
        "object_count": seed_result.object_count,
        "total_blob_bytes": seed_result.total_blob_bytes,
        **file_catalogue,
        "seed_push_s": seed_push_s,
        "spin_up_ms": spin_up_ms,
        "commit_push_ms": commit_push_ms,
        "teardown_ms": teardown_ms,
        "re_spin_up_ms": re_spin_up_ms,
    }
    emit({"phase": "report", **report})
    return report
