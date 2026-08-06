import hashlib
from dataclasses import dataclass
from enum import IntEnum


class ObjectType(IntEnum):
    COMMIT = 1
    TREE = 2
    BLOB = 3
    TAG = 4


@dataclass(frozen=True)
class GitObjectHeader:
    obj_type: ObjectType
    size: int
    oid: bytes
    content: bytes


def object_header(obj_type: ObjectType, content: bytes) -> bytes:
    return f"{obj_type.name.lower()} {len(content)}\0".encode()


def hash_object(obj_type: ObjectType, content: bytes) -> bytes:
    payload = object_header(obj_type, content) + content
    return hashlib.sha1(payload).digest()


def parse_tree_entries(content: bytes) -> list[tuple[str, str, bytes]]:
    """Return list of (mode, name, oid_bytes) from a tree object."""
    entries: list[tuple[str, str, bytes]] = []
    pos = 0
    while pos < len(content):
        space = content.index(b" ", pos)
        mode = content[pos:space].decode()
        nul = content.index(b"\0", space)
        name = content[space + 1 : nul].decode()
        oid = content[nul + 1 : nul + 21]
        entries.append((mode, name, oid))
        pos = nul + 21
    return entries


def build_tree_content(entries: list[tuple[str, str, bytes]]) -> bytes:
    parts: list[bytes] = []
    for mode, name, oid in sorted(entries, key=lambda e: e[1]):
        parts.append(mode.encode())
        parts.append(b" ")
        parts.append(name.encode())
        parts.append(b"\0")
        parts.append(oid)
    return b"".join(parts)
