"""file_versions_comments_agent_runs

Revision ID: 20260806123000
Revises: 20260806120001
Create Date: 2026-08-06 12:30:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260806123000"
down_revision: str | Sequence[str] | None = "20260806120001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "file_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Integer(),
            sa.ForeignKey("repositories.id"),
            nullable=False,
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("blob_oid", postgresql.BYTEA(), nullable=False),
        sa.Column("commit_oid", postgresql.BYTEA(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "file_versions_repo_path_created_idx",
        "file_versions",
        ["repo_id", "path", "created_at"],
    )
    op.create_index(
        "file_versions_repo_commit_idx",
        "file_versions",
        ["repo_id", "commit_oid"],
    )

    op.create_table(
        "file_comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "file_version_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("file_versions.id"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False, server_default="demo"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index(
        "file_comments_version_idx",
        "file_comments",
        ["file_version_id", "created_at"],
    )

    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "repo_id",
            sa.Integer(),
            sa.ForeignKey("repositories.id"),
            nullable=False,
        ),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("base_commit", postgresql.BYTEA(), nullable=False),
        sa.Column("head_commit", postgresql.BYTEA(), nullable=True),
        sa.Column("container_id", sa.Text(), nullable=True),
        sa.Column("session_cwd", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("agent_runs_repo_status_idx", "agent_runs", ["repo_id", "status"])


def downgrade() -> None:
    op.drop_index("agent_runs_repo_status_idx", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("file_comments_version_idx", table_name="file_comments")
    op.drop_table("file_comments")
    op.drop_index("file_versions_repo_commit_idx", table_name="file_versions")
    op.drop_index("file_versions_repo_path_created_idx", table_name="file_versions")
    op.drop_table("file_versions")
