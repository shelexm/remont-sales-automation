"""Create lead inbox, case processing and queue functions.

Revision ID: 20261105_0001
"""
from pathlib import Path

from alembic import op

revision = "20261105_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    existing = connection.exec_driver_sql("""SELECT EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relname IN
          ('lead_events', 'lead_cases', 'integration_jobs', 'crm_entities', 'demo_crm', 'crm_tracking')
    )""").scalar()
    if existing:
        raise RuntimeError("Existing unversioned project tables: stop and follow the legacy adoption guide")
    sql = (Path(__file__).resolve().parents[1] / "sql" / "001_schema.sql").read_text()
    with connection.connection.driver_connection.cursor() as cursor:
        cursor.execute(sql, prepare=False)


def downgrade():
    raise RuntimeError("Destructive downgrade is not supported; restore a verified backup")
