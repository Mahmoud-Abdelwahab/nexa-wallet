"""Add per-user idempotency keys to funding requests.

Revision ID: a903bc521d40
Revises: 206b7691902e
"""
from alembic import op
import sqlalchemy as sa

revision = "a903bc521d40"
down_revision = "206b7691902e"
branch_labels = None
depends_on = None


def upgrade():
    # Existing requests have no client key; PostgreSQL permits multiple NULLs.
    op.add_column("topups", sa.Column("idempotency_key", sa.String(128), nullable=True))
    op.create_unique_constraint("uq_topup_user_key", "topups", ["user_id", "idempotency_key"])


def downgrade():
    op.drop_constraint("uq_topup_user_key", "topups", type_="unique")
    op.drop_column("topups", "idempotency_key")
