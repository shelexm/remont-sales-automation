"""SQLAlchemy mapping for the lead inbox; migration files own schema changes."""
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CHAR, DateTime, Identity, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class LeadEvent(Base):
    __tablename__ = "lead_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    payload_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    # Defined in the full database schema; the other table is still being moved to mappings.
    case_id: Mapped[int | None] = mapped_column(BigInteger)
