"""
SQLAlchemy ORM models for persistent storage.

Tables:
  - conversations     — every ingested conversation (email/call/meeting)
  - scored_prospects  — scored prospects with priority and SLA data
  - followup_drafts   — generated draft variants
  - audit_log         — immutable audit trail of all system actions
"""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column, String, Float, Boolean, Integer, Text, DateTime, JSON, Index,
)
from sqlalchemy.orm import Mapped, mapped_column

from . import Base


def _uuid() -> str:
    return str(uuid.uuid4())


# ── Conversation Record ────────────────────────────────────────
class ConversationRecord(Base):
    """Persistent record of an ingested conversation."""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # email|call|meeting
    participants: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    commitments: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    entities: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    sentiment: Mapped[str] = mapped_column(String(20), default="neutral")
    deal_size: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    urgency: Mapped[str] = mapped_column(String(10), default="low")

    # Timestamps
    conversation_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Metadata
    created_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    tags: Mapped[Optional[list]] = mapped_column(JSON, default=list)

    __table_args__ = (
        Index("ix_conv_source", "source"),
        Index("ix_conv_urgency", "urgency"),
        Index("ix_conv_ingested", "ingested_at"),
    )


# ── Scored Prospect Record ─────────────────────────────────────
class ScoredProspectRecord(Base):
    """Persistent record of a scored prospect."""

    __tablename__ = "scored_prospects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    priority_score: Mapped[float] = mapped_column(Float, nullable=False)
    recency_days: Mapped[float] = mapped_column(Float, default=0.0)
    engagement_probability: Mapped[float] = mapped_column(Float, default=0.5)
    deal_value_normalized: Mapped[float] = mapped_column(Float, default=0.0)
    urgency_score: Mapped[float] = mapped_column(Float, default=0.0)

    # SLA
    sla_deadline: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    sla_breached: Mapped[bool] = mapped_column(Boolean, default=False)
    times_requeued: Mapped[int] = mapped_column(Integer, default=0)

    # State
    status: Mapped[str] = mapped_column(String(20), default="queued")
    response_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # Timestamps
    scored_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_action_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_sp_status", "status"),
        Index("ix_sp_score", "priority_score"),
        Index("ix_sp_sla", "sla_deadline"),
    )


# ── Follow-Up Draft Record ─────────────────────────────────────
class FollowUpDraftRecord(Base):
    """Persistent record of generated follow-up drafts."""

    __tablename__ = "followup_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    prospect_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    company: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Draft variants
    variant_agreeable: Mapped[str] = mapped_column(Text, default="")
    variant_direct: Mapped[str] = mapped_column(Text, default="")
    variant_soft: Mapped[str] = mapped_column(Text, default="")
    selected_variant: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # Sending
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    send_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    to_address: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Tracking
    opens: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_draft_sent", "sent"),
    )


# ── Audit Log ──────────────────────────────────────────────────
class AuditLog(Base):
    """
    Immutable audit trail of all system actions.

    Every queue operation, email send, draft generation, and
    SLA breach is recorded here for compliance and debugging.
    """

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    # Action classification
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    # e.g. "queue:added", "queue:popped", "email:sent", "draft:generated", "sla:breach"

    # Target
    entity_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # e.g. "conversation", "prospect", "draft"
    entity_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)

    # Details
    details: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    # Free-form JSON with action-specific data

    # Actor
    actor: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # e.g. "system", "rep:john", "api:pipeline"

    # Outcome
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_audit_action_time", "action", "timestamp"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )
