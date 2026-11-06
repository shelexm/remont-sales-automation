"""Run migrations only against the explicitly configured PostgreSQL database."""
import os

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"]).set(drivername="postgresql+psycopg")
if context.is_offline_mode():
    raise RuntimeError("Offline SQL generation is not supported for baseline safety checks")
engine = create_engine(url, pool_pre_ping=True)
with engine.connect() as connection:
    context.configure(connection=connection, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
