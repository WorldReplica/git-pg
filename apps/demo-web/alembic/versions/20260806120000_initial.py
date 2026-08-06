"""Baseline schema (applied via docker/init.sql for local dev).

Revision ID: 20260806120000
Revises:
Create Date: 2026-08-06 12:00:00

"""

from collections.abc import Sequence

revision: str = "20260806120000"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
