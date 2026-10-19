"""add X account watchlist to preferences

Revision ID: 5f3b9c2a1d7e
Revises: c12e3b3639a7
Create Date: 2026-10-19 15:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5f3b9c2a1d7e"
down_revision: str | None = "c12e3b3639a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "preference_profiles",
        sa.Column("x_accounts", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    op.drop_column("preference_profiles", "x_accounts")
