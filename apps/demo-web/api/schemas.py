from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class AgentRunStatusOut(StrEnum):
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    AUTO_REBASING = "auto_rebasing"
    AGENT_REBASING = "agent_rebasing"
    APPROVED = "approved"
    FAILED = "failed"
    REJECTED = "rejected"


class TreeEntry(BaseModel):
    path: str
    size: int
    content_type: str | None = None


class TreeResponse(BaseModel):
    ref: str
    commit_oid: str
    entries: list[TreeEntry]


class FileVersionOut(BaseModel):
    id: UUID
    path: str
    blob_oid: str
    commit_oid: str
    size: int
    content_type: str | None
    created_at: datetime


class FileVersionList(BaseModel):
    path: str
    versions: list[FileVersionOut]


class FileCommentOut(BaseModel):
    id: UUID
    file_version_id: UUID
    body: str
    author: str
    created_at: datetime


class FileCommentCreate(BaseModel):
    body: str = Field(min_length=1)
    author: str = "demo"


class FileCommentList(BaseModel):
    file_version_id: UUID
    comments: list[FileCommentOut]


class AgentRunOut(BaseModel):
    id: UUID
    branch: str
    status: AgentRunStatusOut
    base_commit: str
    head_commit: str | None
    prompt: str | None
    summary: str | None
    created_at: datetime
    finished_at: datetime | None
    base_stale: bool = False


class AgentRunList(BaseModel):
    runs: list[AgentRunOut]


class AgentSpawnRequest(BaseModel):
    prompt: str | None = None


class AgentSpawnResponse(BaseModel):
    run: AgentRunOut


class ChangedPath(BaseModel):
    path: str
    change: Literal["added", "modified", "deleted"]


class AgentDiffResponse(BaseModel):
    run_id: UUID
    base_commit: str
    head_commit: str | None
    changed_paths: list[ChangedPath]


class AgentCommitOut(BaseModel):
    oid: str
    subject: str
    author: str | None = None


class AgentCommitsResponse(BaseModel):
    run_id: UUID
    base_commit: str
    head_commit: str | None
    commits: list[AgentCommitOut]


class RebaseStrategy(StrEnum):
    AUTO = "auto"
    AGENT = "agent"


class ApproveRequest(BaseModel):
    rebase_strategy: RebaseStrategy = RebaseStrategy.AUTO


class ApproveResponse(BaseModel):
    run: AgentRunOut
    projected_version_ids: list[UUID]
    rebasing_run_ids: list[UUID]


class RejectResponse(BaseModel):
    run: AgentRunOut


class DeleteResponse(BaseModel):
    run_id: UUID


class RebaseRequest(BaseModel):
    rebase_strategy: RebaseStrategy = RebaseStrategy.AUTO


class RebaseResponse(BaseModel):
    run: AgentRunOut


class WsAgentUpdated(BaseModel):
    type: Literal["agent.updated"] = "agent.updated"
    run_id: UUID


class WsMainUpdated(BaseModel):
    type: Literal["main.updated"] = "main.updated"


class WsFileVersionsCreated(BaseModel):
    type: Literal["file_versions.created"] = "file_versions.created"
    version_ids: list[UUID]
    paths: list[str]


class WsCommentCreated(BaseModel):
    type: Literal["comment.created"] = "comment.created"
    comment: FileCommentOut


WsEvent = Annotated[
    WsAgentUpdated | WsMainUpdated | WsFileVersionsCreated | WsCommentCreated,
    Field(discriminator="type"),
]


class ErrorResponse(BaseModel):
    detail: str
