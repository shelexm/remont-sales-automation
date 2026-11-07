"""Add an audit trail for manually confirmed integration deliveries.

Revision ID: 20261107_0003
"""
from alembic import op

revision = "20261107_0003"
down_revision = "20261105_0002"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE integration_resolutions (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            job_id bigint NOT NULL UNIQUE REFERENCES integration_jobs(id),
            decision text NOT NULL CHECK (decision = 'confirmed_delivered'),
            evidence text NOT NULL CHECK (evidence = 'telegram_chat'),
            operator text NOT NULL CHECK (operator ~ '^[A-Za-z0-9_.-]{1,32}$'),
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)


def downgrade():
    raise RuntimeError("Destructive downgrade is not supported; restore a verified backup")
