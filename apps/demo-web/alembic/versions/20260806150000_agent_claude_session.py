"""agent_run_claude_session_id

Revision ID: 20260806150000
Revises: 20260806140000
Create Date: 2026-08-06 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806150000"
down_revision: str | None = "20260806140000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("claude_session_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "claude_session_id")
