from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import BYTEA, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from git_pg.db.base import Base


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    AUTO_REBASING = "auto_rebasing"
    AGENT_REBASING = "agent_rebasing"
    APPROVED = "approved"
    FAILED = "failed"
    REJECTED = "rejected"


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)

    objects: Mapped[list[GitObject]] = relationship(back_populates="repository")
    refs: Mapped[list[GitRef]] = relationship(back_populates="repository")
    special_rules: Mapped[list[SpecialRule]] = relationship(back_populates="repository")
    file_versions: Mapped[list[FileVersion]] = relationship(back_populates="repository")
    agent_runs: Mapped[list[AgentRun]] = relationship(back_populates="repository")


class GitObject(Base):
    __tablename__ = "objects"

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    oid: Mapped[bytes] = mapped_column(BYTEA, primary_key=True)
    type: Mapped[int] = mapped_column(Integer, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[bytes] = mapped_column(BYTEA, nullable=False)

    repository: Mapped[Repository] = relationship(back_populates="objects")


class GitRef(Base):
    __tablename__ = "refs"

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    oid: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    symbolic: Mapped[str | None] = mapped_column(Text, nullable=True)

    repository: Mapped[Repository] = relationship(back_populates="refs")


class SpecialRule(Base):
    __tablename__ = "special_rules"
    __table_args__ = (UniqueConstraint("repo_id", "path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    handler: Mapped[str] = mapped_column(Text, nullable=False)

    repository: Mapped[Repository] = relationship(back_populates="special_rules")


class Rate(Base):
    __tablename__ = "rates"

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    name: Mapped[str] = mapped_column(Text, primary_key=True)
    rate: Mapped[str] = mapped_column(Text, nullable=False)


class AppConfig(Base):
    __tablename__ = "app_config"

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id"), primary_key=True
    )
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw: Mapped[object | None] = mapped_column(JSONB, nullable=True)


class FileVersion(Base):
    __tablename__ = "file_versions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    blob_oid: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    commit_oid: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    content_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    repository: Mapped[Repository] = relationship(back_populates="file_versions")
    comments: Mapped[list[FileComment]] = relationship(back_populates="file_version")


class FileComment(Base):
    __tablename__ = "file_comments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    file_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("file_versions.id"),
        nullable=False,
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False, default="demo")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    file_version: Mapped[FileVersion] = relationship(back_populates="comments")


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), nullable=False)
    branch: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    base_commit: Mapped[bytes] = mapped_column(BYTEA, nullable=False)
    head_commit: Mapped[bytes | None] = mapped_column(BYTEA, nullable=True)
    container_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_cwd: Mapped[str | None] = mapped_column(Text, nullable=True)
    claude_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    repository: Mapped[Repository] = relationship(back_populates="agent_runs")
