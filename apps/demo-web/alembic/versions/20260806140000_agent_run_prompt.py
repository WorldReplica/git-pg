"""agent_run_prompt

Revision ID: 20260806140000
Revises: 20260806123000
Create Date: 2026-08-06 14:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806140000"
down_revision: str | Sequence[str] | None = "20260806123000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "prompt")
