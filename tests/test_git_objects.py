from git_pg.git.objects import (
    ObjectType,
    build_tree_content,
    hash_object,
    parse_tree_entries,
)


def test_hash_object_blob() -> None:
    content = b"hello\n"
    oid = hash_object(ObjectType.BLOB, content)
    assert len(oid) == 20
    # Stable for this payload; matches git's loose object hash.
    assert oid.hex() == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_tree_roundtrip() -> None:
    blob_a = hash_object(ObjectType.BLOB, b"a\n")
    blob_b = hash_object(ObjectType.BLOB, b"b\n")
    entries = [("100644", "a.txt", blob_a), ("100644", "b.txt", blob_b)]
    tree_content = build_tree_content(entries)
    parsed = parse_tree_entries(tree_content)
    assert len(parsed) == 2
    names = {name for _, name, _ in parsed}
    assert names == {"a.txt", "b.txt"}


def test_is_tree_mode_directories() -> None:
    from git_pg.store.postgres import _is_tree_mode

    assert _is_tree_mode("40000")
    assert not _is_tree_mode("100644")


def test_cat_file_batch_roundtrip(tmp_path) -> None:
    import subprocess
    from pathlib import Path

    from git_pg.store.postgres import _cat_file_batch

    repo = Path(tmp_path)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@l"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "a.txt").write_text("hello\n")
    (repo / "bin.dat").write_bytes(b"\x00\x01\x02" * 1000)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True)

    objects = _cat_file_batch(repo)
    assert len(objects) >= 3  # commit + tree + blobs
    types = {obj_type for _, obj_type, _ in objects}
    assert ObjectType.COMMIT in types
    assert ObjectType.TREE in types
    assert ObjectType.BLOB in types
    contents = {content for _, _, content in objects}
    assert b"hello\n" in contents
    assert b"\x00\x01\x02" * 1000 in contents
