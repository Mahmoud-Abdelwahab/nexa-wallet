"""create topups table

Revision ID: 1381fa10a64e
Revises: 587c12b7d3ee
Create Date: 2026-11-01 14:56:07.113236

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '1381fa10a64e'
down_revision: Union[str, Sequence[str], None] = '587c12b7d3ee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    postgresql.ENUM('PENDING', 'COMPLETED', 'FAILED', name='topup_status').create(
        op.get_bind(), checkfirst=True
    )
    # The currency enum already exists; reuse it without creating it again.
    op.create_table('topups',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('wallet_id', sa.Integer(), nullable=False),
        sa.Column('amount_minor', sa.BigInteger(), nullable=False),
        sa.Column('currency', postgresql.ENUM('AED', 'USD', 'EUR', name='currency', create_type=False), nullable=False),
        sa.Column('moyasar_payment_id', sa.String(length=36), nullable=False),
        sa.Column('transaction_id', sa.Integer(), nullable=True),
        sa.Column('status', postgresql.ENUM('PENDING', 'COMPLETED', 'FAILED', name='topup_status', create_type=False), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('amount_minor > 0', name='ck_topup_amount_positive'),
        sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['wallet_id'], ['wallets.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('moyasar_payment_id'),
        sa.UniqueConstraint('transaction_id')
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('topups')
    # This enum belongs only to topups; keep the shared currency enum.
    postgresql.ENUM(name='topup_status').drop(op.get_bind(), checkfirst=True)
