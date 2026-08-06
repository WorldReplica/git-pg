from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from git_pg.config import Settings
from git_pg.models.repo import RefName
from git_pg.store.postgres import PostgresGitStore


class WarmCacheStatus(StrEnum):
    FRESH = "fresh"
    UPDATING = "updating"
    STALE = "stale"
    REBUILDING = "rebuilding"
    FAILED = "failed"


@dataclass(frozen=True)
class WarmCacheState:
    status: WarmCacheStatus
    current_version: str | None = None
    error: str | None = None
    updated_at: float | None = None


class WarmCacheManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._root = Path(settings.warm_cache_root)
        # Serialize refresh in-process so we never block the event loop on flock.
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    @property
    def enabled(self) -> bool:
        return self._settings.warm_cache_enabled

    def _refresh_lock(self, repo_name: str) -> asyncio.Lock:
        lock = self._refresh_locks.get(repo_name)
        if lock is None:
            lock = asyncio.Lock()
            self._refresh_locks[repo_name] = lock
        return lock

    async def export_session(
        self,
        store: PostgresGitStore,
        repo_id: int,
        repo_name: str,
        ref: RefName,
        dest: Path,
    ) -> str:
        if not self.enabled:
            await store.export_to_local(repo_id, dest, ref)
            return "postgres-export"

        try:
            current = await self.ensure_current(store, repo_id, repo_name, ref)
            await asyncio.to_thread(_git_clone_local_branch, current, dest, ref)
            return "warm-cache-clone"
        except Exception:
            await store.export_to_local(repo_id, dest, ref)
            return "postgres-export"

    async def refresh_repo(
        self,
        store: PostgresGitStore,
        repo_id: int,
        repo_name: str,
    ) -> None:
        if not self.enabled:
            return
        await self._refresh_repo(store, repo_id, repo_name, expected_ref=None)

    async def ensure_current(
        self,
        store: PostgresGitStore,
        repo_id: int,
        repo_name: str,
        ref: RefName,
    ) -> Path:
        current = await self._current_path(repo_name)
        expected_oid = await store.get_ref_oid(repo_id, ref)
        if expected_oid is None:
            msg = f"ref {ref.heads_name} not found"
            raise LookupError(msg)
        if current is not None:
            current_oid = await asyncio.to_thread(_read_ref_oid, current, ref)
            if current_oid == expected_oid.hex():
                await self._write_state(
                    repo_name,
                    WarmCacheState(
                        status=WarmCacheStatus.FRESH,
                        current_version=current.name,
                        updated_at=time.time(),
                    ),
                )
                return current
            await self._write_state(
                repo_name,
                WarmCacheState(
                    status=WarmCacheStatus.STALE,
                    current_version=current.name,
                    updated_at=time.time(),
                ),
            )
        return await self._refresh_repo(store, repo_id, repo_name, expected_ref=ref)

    async def _refresh_repo(
        self,
        store: PostgresGitStore,
        repo_id: int,
        repo_name: str,
        *,
        expected_ref: RefName | None,
    ) -> Path:
        repo_root = self._repo_root(repo_name)
        versions_root = repo_root / "versions"
        await asyncio.to_thread(versions_root.mkdir, parents=True, exist_ok=True)
        file_lock = _repo_lock(repo_root / "update.lock")
        # asyncio.Lock first (event-loop safe); flock only in a worker thread so
        # concurrent session starts cannot freeze the API on fcntl.flock.
        async with self._refresh_lock(repo_name):
            await asyncio.to_thread(file_lock.acquire)
            try:
                current = await asyncio.to_thread(_read_current_path, repo_root)
                if expected_ref is not None and current is not None:
                    expected_oid = await store.get_ref_oid(repo_id, expected_ref)
                    if expected_oid is not None:
                        current_oid = await asyncio.to_thread(
                            _read_ref_oid, current, expected_ref
                        )
                        if current_oid == expected_oid.hex():
                            return current

                current_version = current.name if current is not None else None
                next_status = (
                    WarmCacheStatus.UPDATING
                    if current is not None
                    else WarmCacheStatus.REBUILDING
                )
                await self._write_state(
                    repo_name,
                    WarmCacheState(
                        status=next_status,
                        current_version=current_version,
                        updated_at=time.time(),
                    ),
                )

                next_version = versions_root / (
                    f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
                )
                try:
                    if current is not None:
                        await asyncio.to_thread(
                            _git_clone_bare_local,
                            current,
                            next_version,
                        )
                    else:
                        await asyncio.to_thread(_git_init_bare, next_version)
                    await store.sync_bare_repo(repo_id, next_version)
                    await asyncio.to_thread(
                        _swap_current_version, repo_root, next_version
                    )
                    await self._write_state(
                        repo_name,
                        WarmCacheState(
                            status=WarmCacheStatus.FRESH,
                            current_version=next_version.name,
                            updated_at=time.time(),
                        ),
                    )
                    return next_version
                except Exception as exc:
                    await self._write_state(
                        repo_name,
                        WarmCacheState(
                            status=WarmCacheStatus.FAILED,
                            current_version=current_version,
                            error=str(exc),
                            updated_at=time.time(),
                        ),
                    )
                    raise
            finally:
                await asyncio.to_thread(file_lock.release)

    async def _current_path(self, repo_name: str) -> Path | None:
        repo_root = self._repo_root(repo_name)
        return await asyncio.to_thread(_read_current_path, repo_root)

    async def _write_state(self, repo_name: str, state: WarmCacheState) -> None:
        repo_root = self._repo_root(repo_name)
        await asyncio.to_thread(repo_root.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(_write_state_file, repo_root / "state.json", state)

    def _repo_root(self, repo_name: str) -> Path:
        safe_name = repo_name.replace("/", "__")
        return self._root / safe_name


def _git_init_bare(dest: Path) -> None:
    import subprocess

    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", str(dest)],
        check=True,
        capture_output=True,
    )


def _git_clone_bare_local(src: Path, dest: Path) -> None:
    import subprocess

    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--bare", "--local", str(src), str(dest)],
        check=True,
        capture_output=True,
    )


def _git_clone_local_branch(src: Path, dest: Path, ref: RefName) -> None:
    import shutil
    import subprocess

    if dest.exists():
        shutil.rmtree(dest)
    branch = ref.heads_name.removeprefix("refs/heads/")
    subprocess.run(
        ["git", "clone", "--local", "--branch", branch, str(src), str(dest)],
        check=True,
        capture_output=True,
    )


def _swap_current_version(repo_root: Path, next_version: Path) -> None:
    current = repo_root / "current"
    tmp = repo_root / f".current-{uuid.uuid4().hex}"
    rel = os.path.relpath(next_version, repo_root)
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(rel, target_is_directory=True)
    os.replace(tmp, current)


def _read_current_path(repo_root: Path) -> Path | None:
    current = repo_root / "current"
    if not current.exists():
        return None
    return current.resolve()


def _read_ref_oid(repo_path: Path, ref: RefName) -> str | None:
    import subprocess

    proc = subprocess.run(
        ["git", "--git-dir", str(repo_path), "rev-parse", ref.heads_name],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _write_state_file(path: Path, state: WarmCacheState) -> None:
    payload = {
        "status": state.status.value,
        "current_version": state.current_version,
        "error": state.error,
        "updated_at": state.updated_at,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


class _repo_lock:
    """Cross-process file lock. Call acquire/release from a worker thread only."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: TextIO | None = None

    def acquire(self) -> None:
        import fcntl

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)

    def release(self) -> None:
        import fcntl

        if self._fh is None:
            return
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None

    def __enter__(self) -> None:
        self.acquire()
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object | None,
    ) -> None:
        self.release()
