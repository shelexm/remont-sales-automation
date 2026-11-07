"""Persistence boundary for incoming events."""
import hmac
from functools import lru_cache

from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url

from app.core.leads import fingerprint
from app.db.models import LeadEvent


@lru_cache(maxsize=4)
def _engine(dsn):
    url = make_url(dsn).set(drivername="postgresql+psycopg")
    return create_engine(url, pool_pre_ping=True, pool_timeout=2, connect_args={"connect_timeout": 2})


def database_ready(dsn):
    """Confirm that PostgreSQL and the inbox table are readable within a short deadline."""
    with _engine(dsn).connect() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '2s'"))
        conn.execute(select(LeadEvent.id).limit(1)).all()
    return True

def save_lead(dsn, lead):
    digest = fingerprint(lead)
    with _engine(dsn).begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = '2s'"))
        statement = (insert(LeadEvent).values(source_id=lead["source_id"], payload_hash=digest, payload=lead)
                     .on_conflict_do_nothing(index_elements=[LeadEvent.source_id]).returning(LeadEvent.id))
        if conn.execute(statement).scalar_one_or_none() is not None:
            return "created"
        existing = conn.execute(select(LeadEvent.payload_hash).where(LeadEvent.source_id == lead["source_id"])).scalar_one_or_none()
        return "duplicate" if existing and hmac.compare_digest(existing, digest) else "conflict"
