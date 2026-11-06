"""Database table mappings; Alembic revisions own schema changes."""
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, CHAR, CheckConstraint, DateTime, ForeignKey, Identity, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class LeadEvent(Base):
    __tablename__ = "lead_events"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'processing', 'completed', 'failed')"),
        CheckConstraint("attempts >= 0"),
        Index("lead_events_queue_idx", "available_at", "id", postgresql_where=text("status IN ('pending', 'processing')")),
    )

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
    case_id: Mapped[int | None] = mapped_column(ForeignKey("lead_cases.id"))


class LeadCase(Base):
    __tablename__ = "lead_cases"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    phone: Mapped[str] = mapped_column(Text)
    service: Mapped[str] = mapped_column(Text)
    first_event_id: Mapped[int] = mapped_column(ForeignKey("lead_events.id"))
    last_event_id: Mapped[int] = mapped_column(ForeignKey("lead_events.id"))
    occurrences: Mapped[int] = mapped_column(Integer, server_default="1")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    __table_args__ = (Index("lead_cases_dedup_idx", "phone", "service", text("last_seen_at DESC")),)


class IntegrationJob(Base):
    __tablename__ = "integration_jobs"
    __table_args__ = (
        CheckConstraint("destination IN ('crm', 'telegram', 'telegram_reminder')"),
        CheckConstraint("status IN ('pending', 'processing', 'done', 'failed', 'uncertain')"),
        UniqueConstraint("event_id", "destination"),
        Index("integration_jobs_pending_idx", "available_at", "id", postgresql_where=text("status IN ('pending', 'processing')")),
        Index("integration_one_active_case", "case_id", "destination", unique=True, postgresql_where=text("status = 'processing'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("lead_events.id"))
    case_id: Mapped[int] = mapped_column(ForeignKey("lead_cases.id"))
    destination: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class CRMEntity(Base):
    __tablename__ = "crm_entities"

    entity_type: Mapped[str] = mapped_column(Text, primary_key=True)
    external_key: Mapped[str] = mapped_column(Text, primary_key=True)
    create_started: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    remote_id: Mapped[str | None] = mapped_column(Text)


class DemoCRM(Base):
    __tablename__ = "demo_crm"

    case_id: Mapped[int] = mapped_column(ForeignKey("lead_cases.id"), primary_key=True)
    contact_id: Mapped[str] = mapped_column(Text)
    deal_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default="new")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class CRMTracking(Base):
    __tablename__ = "crm_tracking"

    case_id: Mapped[int] = mapped_column(ForeignKey("lead_cases.id"), primary_key=True)
    remote_id: Mapped[str] = mapped_column(Text)
    stage: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
