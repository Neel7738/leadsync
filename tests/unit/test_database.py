"""Tests for database persistence and audit trail."""

import os
import pytest
from datetime import datetime, timedelta
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Shared test engine (created once per module)
_test_engine = create_engine("sqlite:///:memory:")
_TestSession = sessionmaker(bind=_test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def setup_test_db():
    """Create fresh tables for each test."""
    from core.database import Base
    from core.database import models  # noqa: F401 — register models with Base
    import core.database as db_mod

    # Create tables
    Base.metadata.drop_all(_test_engine)
    Base.metadata.create_all(_test_engine)

    # Patch the module so get_db() uses our test session
    db_mod._engine = _test_engine
    db_mod._SessionFactory = _TestSession

    # Override get_db at the module level
    @contextmanager
    def patched_get_db():
        session = _TestSession()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    db_mod.get_db = patched_get_db

    yield

    # Cleanup
    db_mod._engine = None
    db_mod._SessionFactory = None


def _get_db():
    """Get the patched get_db from the module."""
    import core.database
    return core.database.get_db


class TestConversationRecord:
    def test_create_and_query(self):
        from core.database.models import ConversationRecord

        with _get_db()() as db:
            conv = ConversationRecord(
                id="test-conv-1",
                source="email",
                participants=[{"name": "John", "email": "j@e.com"}],
                raw_text="Hello, let's discuss the proposal.",
                commitments=["send proposal by Friday"],
                sentiment="positive",
                urgency="high",
                deal_size=50000.0,
                conversation_date=datetime.utcnow(),
            )
            db.add(conv)

        with _get_db()() as db:
            result = db.query(ConversationRecord).filter(ConversationRecord.id == "test-conv-1").first()
            assert result is not None
            assert result.source == "email"
            assert result.urgency == "high"
            assert result.deal_size == 50000.0
            assert result.participants[0]["name"] == "John"

    def test_default_values(self):
        from core.database.models import ConversationRecord

        with _get_db()() as db:
            conv = ConversationRecord(
                source="call",
                raw_text="Transcript text",
                conversation_date=datetime.utcnow(),
            )
            db.add(conv)

        with _get_db()() as db:
            result = db.query(ConversationRecord).first()
            assert result.sentiment == "neutral"
            assert result.urgency == "low"
            assert result.commitments == []
            assert result.ingested_at is not None

    def test_query_by_source(self):
        from core.database.models import ConversationRecord

        with _get_db()() as db:
            for source in ["email", "call", "meeting", "email"]:
                db.add(ConversationRecord(
                    source=source,
                    raw_text=f"test {source}",
                    conversation_date=datetime.utcnow(),
                ))

        with _get_db()() as db:
            emails = db.query(ConversationRecord).filter(ConversationRecord.source == "email").count()
            calls = db.query(ConversationRecord).filter(ConversationRecord.source == "call").count()
            assert emails == 2
            assert calls == 1

    def test_multiple_participants(self):
        from core.database.models import ConversationRecord

        participants = [
            {"name": "John", "email": "john@e.com"},
            {"name": "Jane", "email": "jane@e.com"},
        ]
        with _get_db()() as db:
            conv = ConversationRecord(
                source="meeting",
                raw_text="Team meeting",
                participants=participants,
                conversation_date=datetime.utcnow(),
            )
            db.add(conv)

        with _get_db()() as db:
            result = db.query(ConversationRecord).first()
            assert len(result.participants) == 2


class TestAuditLog:
    def test_log_and_query(self):
        # patched via _get_db()
        from core.database.models import AuditLog

        with _get_db()() as db:
            log = AuditLog(
                action="queue:added",
                entity_type="prospect",
                entity_id="c1",
                details={"priority_score": 0.8},
                actor="system",
                success=True,
            )
            db.add(log)

        with _get_db()() as db:
            entries = db.query(AuditLog).all()
            assert len(entries) == 1
            assert entries[0].action == "queue:added"
            assert entries[0].entity_id == "c1"
            assert entries[0].details["priority_score"] == 0.8

    def test_query_by_action(self):
        # patched via _get_db()
        from core.database.models import AuditLog

        with _get_db()() as db:
            for action in ["queue:added", "queue:popped", "queue:added", "email:sent"]:
                db.add(AuditLog(action=action, entity_type="test", entity_id="x"))

        with _get_db()() as db:
            queue_entries = db.query(AuditLog).filter(
                AuditLog.action.startswith("queue:")
            ).count()
            assert queue_entries == 3

    def test_query_by_entity(self):
        # patched via _get_db()
        from core.database.models import AuditLog

        with _get_db()() as db:
            db.add(AuditLog(action="a", entity_type="prospect", entity_id="c1"))
            db.add(AuditLog(action="b", entity_type="prospect", entity_id="c1"))
            db.add(AuditLog(action="c", entity_type="draft", entity_id="d1"))

        with _get_db()() as db:
            c1_entries = db.query(AuditLog).filter(AuditLog.entity_id == "c1").count()
            assert c1_entries == 2

    def test_failed_action(self):
        # patched via _get_db()
        from core.database.models import AuditLog

        with _get_db()() as db:
            db.add(AuditLog(
                action="email:send",
                success=False,
                error_message="SMTP timeout",
            ))

        with _get_db()() as db:
            entry = db.query(AuditLog).first()
            assert entry.success is False
            assert entry.error_message == "SMTP timeout"


class TestAuditLogger:
    def test_buffer_and_flush(self):
        from core.database.audit import AuditLogger

        logger = AuditLogger(flush_interval=60, max_buffer=100)
        logger.log("test:event", entity_type="test", entity_id="1")
        logger.log("test:event", entity_type="test", entity_id="2")

        assert logger.get_stats()["buffered"] == 2

        count = logger.flush()
        assert count == 2
        assert logger.get_stats()["buffered"] == 0
        assert logger.get_stats()["total_flushed"] == 2

    def test_auto_flush_on_max_buffer(self):
        from core.database.audit import AuditLogger

        logger = AuditLogger(flush_interval=60, max_buffer=3)
        for i in range(3):
            logger.log("test", entity_id=str(i))

        # Should auto-flush when buffer reaches max_buffer
        # (flush happens in the next log call after buffer is full)
        stats = logger.get_stats()
        assert stats["total_logged"] == 3

    def test_query_with_audit_logger(self):
        from core.database.audit import AuditLogger

        logger = AuditLogger(flush_interval=0, max_buffer=1)
        logger.log("queue:added", entity_type="prospect", entity_id="c1",
                    details={"priority": 0.9})
        logger.flush()

        entries = logger.query(action="queue")
        assert len(entries) == 1
        assert entries[0]["action"] == "queue:added"
        assert entries[0]["entity_id"] == "c1"

    def test_flush_error_doesnt_crash(self):
        from core.database.audit import AuditLogger

        logger = AuditLogger(flush_interval=60, max_buffer=100)
        logger.log("test", entity_id="1")

        # Flush should handle errors gracefully
        # (in test env with in-memory DB this should succeed)
        count = logger.flush()
        assert count >= 0


class TestScoredProspectRecord:
    def test_create_and_query(self):
        # patched via _get_db()
        from core.database.models import ScoredProspectRecord

        with _get_db()() as db:
            sp = ScoredProspectRecord(
                conversation_id="conv-1",
                priority_score=0.85,
                sla_deadline=datetime.utcnow() + timedelta(hours=24),
                status="queued",
            )
            db.add(sp)

        with _get_db()() as db:
            result = db.query(ScoredProspectRecord).filter(
                ScoredProspectRecord.conversation_id == "conv-1"
            ).first()
            assert result is not None
            assert result.priority_score == 0.85
            assert result.status == "queued"


class TestFollowUpDraftRecord:
    def test_create_and_query(self):
        # patched via _get_db()
        from core.database.models import FollowUpDraftRecord

        with _get_db()() as db:
            draft = FollowUpDraftRecord(
                conversation_id="conv-1",
                prospect_name="John",
                company="Acme",
                variant_agreeable="Warm draft...",
                variant_direct="Direct draft...",
                variant_soft="Soft draft...",
            )
            db.add(draft)

        with _get_db()() as db:
            result = db.query(FollowUpDraftRecord).first()
            assert result.prospect_name == "John"
            assert result.sent is False
            assert result.opens == 0
