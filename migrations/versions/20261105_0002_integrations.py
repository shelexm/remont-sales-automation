"""Create integration journal, outbox and analytics.

Revision ID: 20261105_0002
"""
from pathlib import Path

from alembic import op

revision = "20261105_0002"
down_revision = "20261105_0001"
branch_labels = None
depends_on = None


def upgrade():
    sql = (Path(__file__).resolve().parents[1] / "sql" / "002_integrations.sql").read_text()
    with op.get_bind().connection.driver_connection.cursor() as cursor:
        cursor.execute(sql, prepare=False)


def downgrade():
    raise RuntimeError("Destructive downgrade is not supported; restore a verified backup")
