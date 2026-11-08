"""allow external topups and add review status

Revision ID: 206b7691902e
Revises: ee20e59308b2
Create Date: 2026-11-07 16:57:06.114186

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '206b7691902e'
down_revision: Union[str, Sequence[str], None] = 'ee20e59308b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # External funding has no sender wallet inside Nexa.
    op.alter_column(
        'transactions', 'sender_wallet_id',
        existing_type=sa.INTEGER(), nullable=True,
    )
    # Autogenerate does not reliably detect new PostgreSQL enum values.
    # PostgreSQL 16 makes this value usable after the migration commits.
    op.execute(
        "ALTER TYPE topup_status ADD VALUE IF NOT EXISTS 'REVIEW_REQUIRED'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    # This fails safely if external transactions still have a NULL sender.
    # Existing records must be resolved explicitly; never delete them here.
    op.alter_column(
        'transactions', 'sender_wallet_id',
        existing_type=sa.INTEGER(), nullable=False,
    )
    # PostgreSQL cannot DROP VALUE: recreate the previous enum instead.
    # Casting fails if REVIEW_REQUIRED rows remain, rolling back the downgrade.
    op.execute("ALTER TYPE topup_status RENAME TO topup_status_with_review")
    op.execute("CREATE TYPE topup_status AS ENUM ('PENDING', 'COMPLETED', 'FAILED')")
    op.execute(
        "ALTER TABLE topups ALTER COLUMN status TYPE topup_status "
        "USING status::text::topup_status"
    )
    op.execute("DROP TYPE topup_status_with_review")
