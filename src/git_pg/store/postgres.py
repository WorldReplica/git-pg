from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from git_pg.db.orm.models import GitObject, GitRef, Repository
from git_pg.git.objects import ObjectType, hash_object, parse_tree_entries
from git_pg.models.repo import GitOid, RefName


@dataclass(frozen=True)
class RepoRecord:
    id: int
    name: str


class PostgresGitStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_repo(self, name: str) -> RepoRecord:
        result = await self._session.execute(
            select(Repository).where(Repository.name == name)
        )
        repo = result.scalar_one_or_none()
        if repo is not None:
            return RepoRecord(id=repo.id, name=repo.name)
        repo = Repository(name=name)
        self._session.add(repo)
        await self._session.flush()
        return RepoRecord(id=repo.id, name=repo.name)

    async def get_repo(self, name: str) -> RepoRecord | None:
        result = await self._session.execute(
            select(Repository).where(Repository.name == name)
        )
        repo = result.scalar_one_or_none()
        if repo is None:
            return None
        return RepoRecord(id=repo.id, name=repo.name)

    async def get_repo_by_id(self, repo_id: int) -> RepoRecord | None:
        result = await self._session.execute(
            select(Repository).where(Repository.id == repo_id)
        )
        repo = result.scalar_one_or_none()
        if repo is None:
            return None
        return RepoRecord(id=repo.id, name=repo.name)

    async def put_object(
        self,
        repo_id: int,
        obj_type: ObjectType,
        content: bytes,
        oid: bytes | None = None,
    ) -> bytes:
        final_oid = oid or hash_object(obj_type, content)
        stmt = (
            insert(GitObject)
            .values(
                repo_id=repo_id,
                oid=final_oid,
                type=int(obj_type),
                size=len(content),
                content=content,
            )
            .on_conflict_do_nothing(index_elements=["repo_id", "oid"])
        )
        await self._session.execute(stmt)
        return final_oid

    async def get_object(self, repo_id: int, oid: bytes) -> bytes | None:
        result = await self._session.execute(
            select(GitObject.content).where(
                GitObject.repo_id == repo_id,
                GitObject.oid == oid,
            )
        )
        return result.scalar_one_or_none()

    async def get_object_type(self, repo_id: int, oid: bytes) -> ObjectType | None:
        result = await self._session.execute(
            select(GitObject.type).where(
                GitObject.repo_id == repo_id,
                GitObject.oid == oid,
            )
        )
        raw = result.scalar_one_or_none()
        if raw is None:
            return None
        return ObjectType(raw)

    async def set_ref(self, repo_id: int, name: str, oid: bytes) -> None:
        stmt = (
            insert(GitRef)
            .values(repo_id=repo_id, name=name, oid=oid, symbolic=None)
            .on_conflict_do_update(
                index_elements=["repo_id", "name"],
                set_={"oid": oid, "symbolic": None},
            )
        )
        await self._session.execute(stmt)

    async def get_ref_oid(self, repo_id: int, ref: RefName) -> bytes | None:
        result = await self._session.execute(
            select(GitRef.oid).where(
                GitRef.repo_id == repo_id,
                GitRef.name == ref.heads_name,
            )
        )
        return result.scalar_one_or_none()

    async def list_refs(self, repo_id: int) -> list[tuple[str, bytes]]:
        result = await self._session.execute(
            select(GitRef.name, GitRef.oid).where(GitRef.repo_id == repo_id)
        )
        return [(name, oid) for name, oid in result.all()]

    async def count_objects(self, repo_id: int) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(GitObject)
            .where(GitObject.repo_id == repo_id)
        )
        return int(result.scalar_one())

    async def total_blob_bytes(self, repo_id: int) -> int:
        result = await self._session.execute(
            select(func.coalesce(func.sum(GitObject.size), 0)).where(
                GitObject.repo_id == repo_id,
                GitObject.type == int(ObjectType.BLOB),
            )
        )
        return int(result.scalar_one())

    async def collect_reachable_oids(self, repo_id: int, root_oid: bytes) -> set[bytes]:
        return await self.collect_reachable_from_roots(repo_id, [root_oid])

    async def collect_reachable_from_roots(
        self,
        repo_id: int,
        root_oids: list[bytes],
    ) -> set[bytes]:
        seen: set[bytes] = set()
        stack = list(root_oids)
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            obj_type = await self.get_object_type(repo_id, oid)
            if obj_type is None:
                # Missing object (e.g. shallow parent). Do not mark reachable.
                continue
            seen.add(oid)
            content = await self.get_object(repo_id, oid)
            if content is None:
                continue
            if obj_type == ObjectType.TREE:
                for _, _, child_oid in parse_tree_entries(content):
                    stack.append(child_oid)
            elif obj_type == ObjectType.COMMIT:
                parents, tree_oid = _parse_commit_links(content)
                stack.append(tree_oid)
                stack.extend(parents)
        return seen

    async def put_objects_bulk(
        self,
        repo_id: int,
        objects: list[tuple[bytes, ObjectType, bytes]],
        *,
        max_rows: int = 500,
        max_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        """Bulk-insert objects via Postgres COPY into a staging table."""
        if not objects:
            return
        i = 0
        while i < len(objects):
            chunk: list[tuple[bytes, ObjectType, bytes]] = []
            nbytes = 0
            while i < len(objects) and len(chunk) < max_rows:
                oid, obj_type, content = objects[i]
                if chunk and nbytes + len(content) > max_bytes:
                    break
                chunk.append((oid, obj_type, content))
                nbytes += len(content)
                i += 1
                # Always emit oversized single objects alone.
                if len(content) > max_bytes:
                    break
            await self._copy_object_chunk(repo_id, chunk)

    async def _copy_object_chunk(
        self,
        repo_id: int,
        chunk: list[tuple[bytes, ObjectType, bytes]],
    ) -> None:
        conn = await self._session.connection()
        raw = await conn.get_raw_connection()
        driver = raw.driver_connection
        if driver is None:
            msg = "no asyncpg driver connection available for COPY"
            raise RuntimeError(msg)
        # asyncpg.Connection — same physical connection as the session txn.
        await driver.execute(
            """
            CREATE TEMP TABLE IF NOT EXISTS _git_pg_objects_ingest (
                repo_id integer NOT NULL,
                oid bytea NOT NULL,
                type smallint NOT NULL,
                size integer NOT NULL,
                content bytea NOT NULL
            ) ON COMMIT DROP
            """
        )
        await driver.execute("TRUNCATE _git_pg_objects_ingest")
        records = [
            (repo_id, oid, int(obj_type), len(content), content)
            for oid, obj_type, content in chunk
        ]
        await driver.copy_records_to_table(
            "_git_pg_objects_ingest",
            records=records,
            columns=["repo_id", "oid", "type", "size", "content"],
        )
        await driver.execute(
            """
            INSERT INTO objects (repo_id, oid, type, size, content)
            SELECT repo_id, oid, type, size, content
            FROM _git_pg_objects_ingest
            ON CONFLICT DO NOTHING
            """
        )

    async def push_from_local(
        self,
        repo_id: int,
        local_path: Path,
        *,
        allow_main: bool = False,
        require_fast_forward: bool = True,
    ) -> GitOid:
        objects = await asyncio.to_thread(_cat_file_batch, local_path)
        await self.put_objects_bulk(repo_id, objects)

        head = await asyncio.to_thread(_rev_parse, local_path, "HEAD")
        branch = await asyncio.to_thread(_symbolic_ref, local_path, "HEAD")
        if branch in {"refs/heads/main", "refs/heads/master"} and not allow_main:
            msg = f"refusing to update protected branch {branch} via agent push"
            raise PermissionError(msg)

        new_oid = bytes.fromhex(head)
        current = await self.get_ref_oid(repo_id, RefName(value=branch))
        if (
            require_fast_forward
            and current is not None
            and current != new_oid
            and not await self.is_ancestor(repo_id, current, new_oid)
        ):
            msg = f"non-fast-forward update rejected for {branch}"
            raise ValueError(msg)

        await self.set_ref(repo_id, branch, new_oid)
        return GitOid(hex=head)

    async def is_ancestor(
        self,
        repo_id: int,
        maybe_ancestor: bytes,
        descendant: bytes,
    ) -> bool:
        if maybe_ancestor == descendant:
            return True
        seen: set[bytes] = set()
        stack = [descendant]
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            if oid == maybe_ancestor:
                return True
            content = await self.get_object(repo_id, oid)
            obj_type = await self.get_object_type(repo_id, oid)
            if content is None or obj_type != ObjectType.COMMIT:
                continue
            parents, _tree = _parse_commit_links(content)
            stack.extend(parents)
        return False

    async def fast_forward_ref(
        self,
        repo_id: int,
        ref: RefName,
        new_oid: bytes,
    ) -> bytes | None:
        """Move ref to new_oid if FF. Returns previous oid (or None if created)."""
        current = await self.get_ref_oid(repo_id, ref)
        if (
            current is not None
            and current != new_oid
            and not await self.is_ancestor(repo_id, current, new_oid)
        ):
            msg = f"non-fast-forward update rejected for {ref.heads_name}"
            raise ValueError(msg)
        await self.set_ref(repo_id, ref.heads_name, new_oid)
        return current

    async def export_to_local(
        self,
        repo_id: int,
        dest: Path,
        ref: RefName,
    ) -> GitOid:
        root_oid = await self.get_ref_oid(repo_id, ref)
        if root_oid is None:
            msg = f"ref {ref.heads_name} not found"
            raise LookupError(msg)

        reachable = await self.collect_reachable_oids(repo_id, root_oid)
        if dest.exists():
            import shutil

            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        await asyncio.to_thread(
            subprocess.run,
            ["git", "init", "-b", "main", str(dest)],
            check=True,
            capture_output=True,
        )
        git_dir = dest / ".git"
        (git_dir / "objects").mkdir(parents=True, exist_ok=True)
        (git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)

        for oid in reachable:
            content = await self.get_object(repo_id, oid)
            obj_type = await self.get_object_type(repo_id, oid)
            if content is None or obj_type is None:
                continue
            await asyncio.to_thread(
                _write_loose_object, git_dir, oid, obj_type, content
            )

        shallow = await self._shallow_boundary_oids(repo_id, reachable)
        await asyncio.to_thread(_write_shallow_file, git_dir, shallow)

        branch = ref.heads_name.removeprefix("refs/heads/")
        ref_path = git_dir / "refs" / "heads" / branch
        ref_path.write_text(root_oid.hex() + "\n")
        head_path = git_dir / "HEAD"
        head_path.write_text(f"ref: refs/heads/{branch}\n")

        await asyncio.to_thread(_git_checkout, dest)
        return GitOid(hex=root_oid.hex())

    async def sync_bare_repo(self, repo_id: int, bare_dir: Path) -> None:
        refs = await self.list_refs(repo_id)
        reachable = await self.collect_reachable_from_roots(
            repo_id,
            [oid for _, oid in refs],
        )
        existing = await asyncio.to_thread(_list_git_object_oids, bare_dir)
        missing = reachable - existing
        for oid in missing:
            content = await self.get_object(repo_id, oid)
            obj_type = await self.get_object_type(repo_id, oid)
            if content is None or obj_type is None:
                continue
            await asyncio.to_thread(
                _write_loose_object,
                bare_dir,
                oid,
                obj_type,
                content,
            )

        shallow = await self._shallow_boundary_oids(repo_id, reachable)
        await asyncio.to_thread(_write_shallow_file, bare_dir, shallow)
        await asyncio.to_thread(_clear_git_refs, bare_dir)
        for name, oid in refs:
            await asyncio.to_thread(_write_git_ref, bare_dir, name, oid)
        await asyncio.to_thread(_set_git_head, bare_dir, refs)

    async def _shallow_boundary_oids(
        self,
        repo_id: int,
        reachable: set[bytes],
    ) -> set[bytes]:
        """Commits whose parents are not present — write them into .git/shallow."""
        boundary: set[bytes] = set()
        for oid in reachable:
            obj_type = await self.get_object_type(repo_id, oid)
            if obj_type != ObjectType.COMMIT:
                continue
            content = await self.get_object(repo_id, oid)
            if content is None:
                continue
            parents, _tree = _parse_commit_links(content)
            if any(parent not in reachable for parent in parents):
                boundary.add(oid)
        return boundary

    async def write_commit_from_tree(
        self,
        repo_id: int,
        parent_oid: bytes,
        tree_oid: bytes,
        author_name: str,
        author_email: str,
        message: str,
    ) -> bytes:
        commit_content = _build_commit_content(
            tree_oid=tree_oid,
            parent_oid=parent_oid,
            author_name=author_name,
            author_email=author_email,
            message=message,
        )
        return await self.put_object(repo_id, ObjectType.COMMIT, commit_content)

    async def replace_tree_blob(
        self,
        repo_id: int,
        root_tree_oid: bytes,
        path: str,
        new_blob_content: bytes,
    ) -> bytes:
        parts = path.split("/")
        new_blob_oid = await self.put_object(repo_id, ObjectType.BLOB, new_blob_content)
        return await self._replace_in_tree(repo_id, root_tree_oid, parts, new_blob_oid)

    async def _replace_in_tree(
        self,
        repo_id: int,
        tree_oid: bytes,
        path_parts: list[str],
        new_leaf_oid: bytes,
    ) -> bytes:
        content = await self.get_object(repo_id, tree_oid)
        if content is None:
            msg = "tree not found"
            raise LookupError(msg)
        entries = parse_tree_entries(content)
        if len(path_parts) == 1:
            name = path_parts[0]
            updated = [
                (mode, entry_name, new_leaf_oid if entry_name == name else oid)
                for mode, entry_name, oid in entries
            ]
            if not any(entry_name == name for _, entry_name, _ in entries):
                updated.append(("100644", name, new_leaf_oid))
            from git_pg.git.objects import build_tree_content

            new_content = build_tree_content(updated)
            return await self.put_object(repo_id, ObjectType.TREE, new_content)

        name = path_parts[0]
        for _mode, entry_name, child_oid in entries:
            if entry_name == name:
                new_child = await self._replace_in_tree(
                    repo_id, child_oid, path_parts[1:], new_leaf_oid
                )
                updated = [
                    (
                        m,
                        n,
                        new_child if n == name else oid,
                    )
                    for m, n, oid in entries
                ]
                from git_pg.git.objects import build_tree_content

                new_content = build_tree_content(updated)
                return await self.put_object(repo_id, ObjectType.TREE, new_content)
        msg = f"path component {name} not found in tree"
        raise LookupError(msg)


def _is_tree_mode(mode: str) -> bool:
    return (int(mode, 8) & 0o170000) == 0o040000


def _parse_commit_links(content: bytes) -> tuple[list[bytes], bytes]:
    parents: list[bytes] = []
    tree_oid = b""
    for line in content.split(b"\n"):
        if line.startswith(b"tree "):
            tree_oid = bytes.fromhex(line[5:].decode())
        elif line.startswith(b"parent "):
            parents.append(bytes.fromhex(line[7:].decode()))
        elif line == b"":
            break
    return parents, tree_oid


def _build_commit_content(
    tree_oid: bytes,
    parent_oid: bytes,
    author_name: str,
    author_email: str,
    message: str,
) -> bytes:
    import time

    ts = int(time.time())
    tz = "+0000"
    author = f"{author_name} <{author_email}> {ts} {tz}"
    lines = [
        f"tree {tree_oid.hex()}",
        f"parent {parent_oid.hex()}",
        f"author {author}",
        f"committer {author}",
        "",
        message,
        "",
    ]
    return "\n".join(lines).encode()


def _write_shallow_file(git_dir: Path, boundary: set[bytes]) -> None:
    shallow_path = git_dir / "shallow"
    if not boundary:
        if shallow_path.exists():
            shallow_path.unlink()
        return
    lines = sorted(oid.hex() for oid in boundary)
    shallow_path.write_text("\n".join(lines) + "\n")


def _cat_file_batch(local_path: Path) -> list[tuple[bytes, ObjectType, bytes]]:
    """Load all local ODB objects via cat-file --batch-all-objects --batch."""
    type_map = {
        "commit": ObjectType.COMMIT,
        "tree": ObjectType.TREE,
        "blob": ObjectType.BLOB,
        "tag": ObjectType.TAG,
    }
    proc = subprocess.Popen(
        [
            "git",
            "-C",
            str(local_path),
            "cat-file",
            "--batch-all-objects",
            "--batch",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None
    try:
        results: list[tuple[bytes, ObjectType, bytes]] = []
        stdout = proc.stdout
        while True:
            header = stdout.readline()
            if not header:
                break
            parts = header.decode().rstrip("\n").split()
            if len(parts) == 2 and parts[1] == "missing":
                continue
            if len(parts) != 3:
                msg = f"unexpected cat-file --batch header: {header!r}"
                raise RuntimeError(msg)
            oid_hex, type_name, size_s = parts
            size = int(size_s)
            content = stdout.read(size)
            if len(content) != size:
                msg = f"short read for {oid_hex}: expected {size}, got {len(content)}"
                raise RuntimeError(msg)
            # Objects are followed by a trailing newline.
            nl = stdout.read(1)
            if nl not in (b"\n", b""):
                msg = f"expected newline after {oid_hex}, got {nl!r}"
                raise RuntimeError(msg)
            results.append((bytes.fromhex(oid_hex), type_map[type_name], content))
    finally:
        stderr = proc.stderr.read() if proc.stderr else b""
        code = proc.wait()
        if code != 0:
            msg = f"git cat-file --batch-all-objects failed ({code}): {stderr.decode()}"
            raise RuntimeError(msg)
    return results


def _cat_file(local_path: Path, oid_hex: str) -> tuple[ObjectType, bytes]:
    proc = subprocess.run(
        ["git", "-C", str(local_path), "cat-file", "-t", oid_hex],
        check=True,
        capture_output=True,
        text=True,
    )
    type_name = proc.stdout.strip()
    type_map = {
        "commit": ObjectType.COMMIT,
        "tree": ObjectType.TREE,
        "blob": ObjectType.BLOB,
        "tag": ObjectType.TAG,
    }
    obj_type = type_map[type_name]
    content_proc = subprocess.run(
        ["git", "-C", str(local_path), "cat-file", type_name, oid_hex],
        check=True,
        capture_output=True,
    )
    return obj_type, content_proc.stdout


def _rev_parse(local_path: Path, ref: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(local_path), "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _symbolic_ref(local_path: Path, ref: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(local_path), "symbolic-ref", ref],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _write_loose_object(
    git_dir: Path, oid: bytes, obj_type: ObjectType, content: bytes
) -> None:
    import zlib

    header = f"{obj_type.name.lower()} {len(content)}\0".encode()
    compressed = zlib.compress(header + content)
    obj_path = git_dir / "objects" / oid.hex()[:2] / oid.hex()[2:]
    obj_path.parent.mkdir(parents=True, exist_ok=True)
    obj_path.write_bytes(compressed)


def _list_git_object_oids(git_dir: Path) -> set[bytes]:
    proc = subprocess.run(
        [
            "git",
            "--git-dir",
            str(git_dir),
            "cat-file",
            "--batch-all-objects",
            "--batch-check=%(objectname)",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return {bytes.fromhex(line) for line in proc.stdout.splitlines() if line}


def _clear_git_refs(git_dir: Path) -> None:
    refs_dir = git_dir / "refs"
    if refs_dir.exists():
        import shutil

        shutil.rmtree(refs_dir)
    refs_dir.mkdir(parents=True, exist_ok=True)
    packed_refs = git_dir / "packed-refs"
    if packed_refs.exists():
        packed_refs.unlink()


def _write_git_ref(git_dir: Path, name: str, oid: bytes) -> None:
    ref_path = git_dir / name
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_text(oid.hex() + "\n")


def _set_git_head(git_dir: Path, refs: list[tuple[str, bytes]]) -> None:
    head_path = git_dir / "HEAD"
    branch_names = [name for name, _ in refs if name.startswith("refs/heads/")]
    if "refs/heads/main" in branch_names:
        branch = "refs/heads/main"
    elif branch_names:
        branch = branch_names[0]
    elif refs:
        branch = refs[0][0]
    else:
        head_path.write_text("ref: refs/heads/main\n")
        return
    head_path.write_text(f"ref: {branch}\n")


def _git_checkout(dest: Path) -> None:
    subprocess.run(
        ["git", "-C", str(dest), "checkout", "-f"],
        check=True,
        capture_output=True,
    )


async def walk_tree_files(
    store: PostgresGitStore,
    repo_id: int,
    tree_oid: bytes,
    prefix: str = "",
) -> AsyncIterator[tuple[str, bytes]]:
    content = await store.get_object(repo_id, tree_oid)
    if content is None:
        return
    for mode, name, oid in parse_tree_entries(content):
        path = f"{prefix}{name}" if not prefix else f"{prefix}/{name}"
        if _is_tree_mode(mode):
            async for item in walk_tree_files(store, repo_id, oid, path):
                yield item
        else:
            blob = await store.get_object(repo_id, oid)
            if blob is not None:
                yield path, blob
