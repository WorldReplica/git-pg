from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from sqlalchemy import delete

from git_pg.benchmark import get_preset
from git_pg.config import Settings
from git_pg.db.engine import session_scope
from git_pg.db.orm.models import Repository
from git_pg.models.repo import SessionStartRequest
from git_pg.orchestrator import Orchestrator
from git_pg.seed import SeedOptions, seed_repo


def _test_settings(database_url: str, prefix: str) -> Settings:
    root = Path(tempfile.mkdtemp(prefix=prefix))
    return Settings(
        database_url=database_url,
        sessions_root=str(root / "sessions"),
        warm_cache_root=str(root / "warm"),
        warm_cache_enabled=True,
    )


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2), flush=True)


def _read_cache_state(warm_root: Path) -> dict[str, object]:
    state_path = warm_root / "state.json"
    if not state_path.exists():
        return {"status": "missing"}
    return json.loads(state_path.read_text())


def _cache_snapshot(warm_root: Path) -> dict[str, object]:
    state = _read_cache_state(warm_root)
    current = warm_root / "current"
    versions_dir = warm_root / "versions"
    versions = (
        sorted(p.name for p in versions_dir.glob("*") if p.is_dir())
        if versions_dir.exists()
        else []
    )
    return {
        "status": state.get("status"),
        "current_version": state.get("current_version"),
        "current_exists": current.exists(),
        "current_resolved": str(current.resolve()) if current.exists() else None,
        "versions": versions,
        "error": state.get("error"),
    }


def _git_log(cwd: str | Path, *, count: int = 5) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "log", f"-{count}", "--oneline", "--decorate"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


@pytest.mark.integration
async def test_warm_cache_lifecycle_with_timing(
    engine,
    database_url: str,
) -> None:
    """Flask shallow warm-cache lifecycle with timings and git history logs."""
    preset = get_preset("flask-shallow")
    repo_name = "test-warm-flask"
    settings = _test_settings(database_url, "git-pg-warm-flask-")
    warm_root = Path(settings.warm_cache_root) / repo_name
    orch = Orchestrator(settings=settings)

    async with session_scope(engine) as session:
        await session.execute(delete(Repository).where(Repository.name == repo_name))
        await session.flush()

    # Seed Postgres only — leave warm cache cold so first session start builds it.
    _emit(
        {
            "phase": "seed_start",
            "preset": preset.id,
            "url": preset.url,
            "depth": preset.depth,
            "repo": repo_name,
            "note": "Postgres seed only; warm cache intentionally cold",
        }
    )
    t0 = time.perf_counter()
    seed_result = await seed_repo(
        engine,
        SeedOptions(url=preset.url, repo=repo_name, depth=preset.depth),
    )
    seed_s = time.perf_counter() - t0
    _emit(
        {
            "phase": "seed_done",
            "seed_s": seed_s,
            "object_count": seed_result.object_count,
            "total_blob_bytes": seed_result.total_blob_bytes,
            "warm_cache": _cache_snapshot(warm_root),
        }
    )
    assert not (warm_root / "current").exists()

    _emit(
        {
            "phase": "first_warm_start",
            "note": "expected to build warm cache then clone",
        }
    )
    handle1 = await orch.session_start(SessionStartRequest(repo=repo_name, ref="main"))
    first_spin_up_ms = handle1.spin_up_ms
    first_log = _git_log(handle1.cwd)
    readme = Path(handle1.cwd) / "README.md"
    assert readme.exists()
    first_cache = _cache_snapshot(warm_root)
    _emit(
        {
            "phase": "first_warm_start_done",
            "spin_up_ms": first_spin_up_ms,
            "cwd": handle1.cwd,
            "git_log": first_log,
            "warm_cache": first_cache,
        }
    )
    assert first_spin_up_ms > 0
    assert first_cache["status"] == "fresh"
    first_current = Path(str(first_cache["current_resolved"]))

    readme.write_text(readme.read_text() + "\n# git-pg warm cache edit\n")
    subprocess.run(
        ["git", "-C", handle1.cwd, "config", "user.email", "warm@git-pg.local"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", handle1.cwd, "config", "user.name", "git-pg warm test"],
        check=True,
    )
    subprocess.run(["git", "-C", handle1.cwd, "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", handle1.cwd, "commit", "-m", "agent warm-cache edit"],
        check=True,
    )
    _emit(
        {
            "phase": "agent_edit_committed",
            "git_log": _git_log(handle1.cwd),
        }
    )

    _emit(
        {
            "phase": "session_push_and_warm_refresh",
            "note": "DB push then automatic warm cache version bump",
        }
    )
    t1 = time.perf_counter()
    await orch.session_push(repo_name, Path(handle1.cwd), ref="main", allow_main=True)
    push_and_refresh_ms = (time.perf_counter() - t1) * 1000
    orch.session_stop(handle1.session_id)

    after_push = _cache_snapshot(warm_root)
    after_push_current = Path(str(after_push["current_resolved"]))
    _emit(
        {
            "phase": "session_push_and_warm_refresh_done",
            "push_and_refresh_ms": push_and_refresh_ms,
            "warm_cache": after_push,
            "cache_version_changed": after_push_current != first_current,
            "previous_version": first_cache["current_version"],
            "new_version": after_push["current_version"],
        }
    )
    assert after_push["status"] == "fresh"
    assert after_push_current != first_current
    versions = after_push["versions"]
    assert isinstance(versions, list)
    assert len(versions) >= 2

    _emit(
        {
            "phase": "second_warm_start",
            "note": "expected to clone updated warm cache (edits present)",
        }
    )
    handle2 = await orch.session_start(SessionStartRequest(repo=repo_name, ref="main"))
    second_spin_up_ms = handle2.spin_up_ms
    second_log = _git_log(handle2.cwd)
    second_readme = (Path(handle2.cwd) / "README.md").read_text()
    _emit(
        {
            "phase": "second_warm_start_done",
            "spin_up_ms": second_spin_up_ms,
            "cwd": handle2.cwd,
            "git_log": second_log,
            "edit_present": "git-pg warm cache edit" in second_readme,
            "warm_cache": _cache_snapshot(warm_root),
        }
    )
    assert "git-pg warm cache edit" in second_readme
    assert "agent warm-cache edit" in second_log[0]
    assert second_spin_up_ms > 0
    assert second_spin_up_ms < first_spin_up_ms

    _emit(
        {
            "phase": "report",
            "preset": preset.id,
            "seed_s": seed_s,
            "first_spin_up_ms": first_spin_up_ms,
            "push_and_refresh_ms": push_and_refresh_ms,
            "second_spin_up_ms": second_spin_up_ms,
            "speedup_vs_first": first_spin_up_ms / second_spin_up_ms,
            "object_count": seed_result.object_count,
            "final_git_log": second_log,
            "warm_cache": _cache_snapshot(warm_root),
        }
    )
    orch.session_stop(handle2.session_id)
