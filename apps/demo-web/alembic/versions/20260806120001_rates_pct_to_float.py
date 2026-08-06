"""rates_pct_to_float

Revision ID: 20260806120001
Revises: 20260806120000
Create Date: 2026-08-06 12:00:01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806120001"
down_revision: str | Sequence[str] | None = "20260806120000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Use chr(37) instead of '%' so psycopg does not treat it as a placeholder.
    op.alter_column(
        "rates",
        "rate",
        type_=sa.Float(),
        postgresql_using="translate(rate, chr(37), '')::double precision / 100.0",
    )


def downgrade() -> None:
    # Restore percentage-string form so rematerialize writes "12.5%" not "0.125".
    op.alter_column(
        "rates",
        "rate",
        type_=sa.Text(),
        postgresql_using=(
            "trim(trailing '.' from trim(trailing '0' from "
            "(rate * 100)::numeric::text)) || chr(37)"
        ),
    )
