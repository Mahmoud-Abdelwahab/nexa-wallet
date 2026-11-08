"""Persist payment support alerts without storing provider secrets.

Revision ID: b71e58d42a90
Revises: a903bc521d40
"""
from alembic import op
import sqlalchemy as sa

revision = "b71e58d42a90"
down_revision = "a903bc521d40"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("moyasar_webhook_events", sa.Column(
        "support_alert_at", sa.DateTime(timezone=True), nullable=True,
    ))


def downgrade():
    op.drop_column("moyasar_webhook_events", "support_alert_at")
