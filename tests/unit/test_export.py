"""Tests for CSV and PDF export functionality."""

import csv
import json
import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Use temporary SQLite database for each test."""
    db_path = str(tmp_path / "test_export.db")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

    # Reset global engine
    import core.database as db_mod
    db_mod._engine = None
    db_mod._SessionFactory = None

    # Initialize tables
    from core.database import init_db
    init_db()

    yield db_path

    db_mod._engine = None
    db_mod._SessionFactory = None
    os.environ.pop("DATABASE_URL", None)


def _seed_conversations(count: int = 3):
    """Insert test conversations into the database."""
    import uuid
    from core.database import get_db
    from core.database.models import ConversationRecord
    from datetime import datetime, timedelta

    sources = ["email", "call", "meeting"]
    urgencies = ["high", "medium", "low"]
    sentiments = ["positive", "negative", "neutral"]

    with get_db() as db:
        for i in range(count):
            conv = ConversationRecord(
                id=str(uuid.uuid4()),
                source=sources[i % 3],
                participants=[{"name": f"Person {i}", "email": f"p{i}@co.com"}],
                raw_text=f"This is conversation number {i}. We discussed the proposal and next steps.",
                commitments=[f"Action item {i}: send proposal"],
                entities={"name": f"Person {i}", "company": f"Company {i}"},
                sentiment=sentiments[i % 3],
                deal_size=10000.0 * (i + 1),
                urgency=urgencies[i % 3],
                conversation_date=datetime.utcnow() - timedelta(days=i),
                ingested_at=datetime.utcnow() - timedelta(hours=i),
                tags=[f"tag{i}"],
            )
            db.add(conv)
        db.commit()


def _seed_audit(count: int = 5):
    """Insert test audit entries."""
    import uuid
    from core.database import get_db
    from core.database.models import AuditLog
    from datetime import datetime, timedelta

    actions = ["queue:added", "queue:popped", "email:sent", "draft:generated", "sla:breach"]

    with get_db() as db:
        for i in range(count):
            log = AuditLog(
                id=str(uuid.uuid4()),
                timestamp=datetime.utcnow() - timedelta(hours=i),
                action=actions[i % len(actions)],
                entity_type="conversation" if i % 2 == 0 else "prospect",
                entity_id=f"entity-{i:03d}",
                details={"key": f"value_{i}"},
                actor=f"system" if i % 3 == 0 else f"rep:user{i}",
                success=i % 4 != 0,
                error_message="test error" if i % 4 == 0 else None,
            )
            db.add(log)
        db.commit()


# ── CSV Export ─────────────────────────────────────────────────


class TestCSVExport:
    def test_export_conversations_csv(self, tmp_path):
        from core.export import ExportManager

        _seed_conversations(3)
        exporter = ExportManager(output_dir=str(tmp_path))
        path = exporter.export_conversations_csv("convs.csv")

        assert os.path.exists(path)
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 3
        assert rows[0]["source"] in ("email", "call", "meeting")
        assert "raw_text" in rows[0]

    def test_export_with_source_filter(self, tmp_path):
        from core.export import ExportManager

        _seed_conversations(3)
        exporter = ExportManager(output_dir=str(tmp_path))
        path = exporter.export_conversations_csv("filtered.csv", source="email")

        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) >= 1
        assert all(r["source"] == "email" for r in rows)

    def test_export_with_urgency_filter(self, tmp_path):
        from core.export import ExportManager

        _seed_conversations(3)
        exporter = ExportManager(output_dir=str(tmp_path))
        path = exporter.export_conversations_csv("urgent.csv", urgency="high")

        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert all(r["urgency"] == "high" for r in rows)

    def test_export_empty_csv(self, tmp_path):
        from core.export import ExportManager

        exporter = ExportManager(output_dir=str(tmp_path))
        path = exporter.export_conversations_csv("empty.csv")
        assert os.path.exists(path)

    def test_export_drafts_csv(self, tmp_path):
        from core.export import ExportManager
        from core.database import get_db
        from core.database.models import FollowUpDraftRecord

        with get_db() as db:
            draft = FollowUpDraftRecord(
                id="draft-001",
                conversation_id="conv-001",
                prospect_name="Test User",
                company="Test Co",
                variant_agreeable="Hi! Friendly follow-up...",
                variant_direct="Following up on our meeting...",
                variant_soft="Just checking in...",
                selected_variant="direct",
                sent=True,
                to_address="test@co.com",
            )
            db.add(draft)
            db.commit()

        exporter = ExportManager(output_dir=str(tmp_path))
        path = exporter.export_drafts_csv("drafts.csv")

        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) >= 1
        assert any(r["prospect_name"] == "Test User" for r in rows)
        assert any(r["sent"] == "True" for r in rows)

    def test_export_audit_csv(self, tmp_path):
        from core.export import ExportManager

        _seed_audit(5)
        exporter = ExportManager(output_dir=str(tmp_path))
        path = exporter.export_audit_csv("audit.csv")

        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) >= 5
        assert all("action" in r for r in rows)

    def test_export_audit_with_action_filter(self, tmp_path):
        from core.export import ExportManager

        _seed_audit(5)
        exporter = ExportManager(output_dir=str(tmp_path))
        path = exporter.export_audit_csv("filtered.csv", action="email:sent")

        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert all(r["action"] == "email:sent" for r in rows)

    def test_export_queue_csv(self, tmp_path):
        from core.export import ExportManager
        from core.queue import PriorityQueue
        from core.models.prospect import ScoredProspect
        from core.models.conversation import Conversation
        from datetime import datetime, timedelta
        import core.queue as queue_mod

        queue = PriorityQueue()
        queue_mod._queue = queue

        conv = Conversation(
            source="email",
            participants=[{"name": "Test", "email": "t@co.com"}],
            raw_text="Test conversation",
            urgency="high",
        )
        prospect = ScoredProspect(
            conversation_id="q-001",
            priority_score=0.85,
            conversation=conv,
            sla_deadline=datetime.utcnow() + timedelta(hours=24),
        )
        queue.add(prospect)

        exporter = ExportManager(output_dir=str(tmp_path))
        path = exporter.export_queue_csv("queue.csv")

        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["conversation_id"] == "q-001"


# ── PDF Export ─────────────────────────────────────────────────


class TestPDFExport:
    def test_export_conversations_pdf(self, tmp_path):
        from core.export import ExportManager

        _seed_conversations(2)
        exporter = ExportManager(output_dir=str(tmp_path))
        path = exporter.export_conversations_pdf("convs.pdf")

        assert os.path.exists(path)
        assert os.path.getsize(path) > 100  # Non-empty PDF
        # Check PDF header
        with open(path, "rb") as f:
            assert f.read(5) == b"%PDF-"

    def test_export_audit_pdf(self, tmp_path):
        from core.export import ExportManager

        _seed_audit(3)
        exporter = ExportManager(output_dir=str(tmp_path))
        path = exporter.export_audit_pdf("audit.pdf")

        assert os.path.exists(path)
        with open(path, "rb") as f:
            assert f.read(5) == b"%PDF-"

    def test_export_summary_pdf(self, tmp_path):
        from core.export import ExportManager

        _seed_conversations(3)
        _seed_audit(5)
        exporter = ExportManager(output_dir=str(tmp_path))
        path = exporter.export_summary_pdf("summary.pdf")

        assert os.path.exists(path)
        assert os.path.getsize(path) > 100
        with open(path, "rb") as f:
            assert f.read(5) == b"%PDF-"

    def test_export_pdf_with_source_filter(self, tmp_path):
        from core.export import ExportManager

        _seed_conversations(3)
        exporter = ExportManager(output_dir=str(tmp_path))
        path = exporter.export_conversations_pdf("email.pdf", source="email")

        assert os.path.exists(path)
        with open(path, "rb") as f:
            assert f.read(5) == b"%PDF-"

    def test_export_pdf_empty(self, tmp_path):
        from core.export import ExportManager

        exporter = ExportManager(output_dir=str(tmp_path))
        path = exporter.export_conversations_pdf("empty.pdf", title="Compliance Report")
        assert os.path.exists(path)
        with open(path, "rb") as f:
            assert f.read(5) == b"%PDF-"


# ── Export Manager ─────────────────────────────────────────────


class TestExportManager:
    def test_creates_output_dir(self, tmp_path):
        from core.export import ExportManager

        out_dir = str(tmp_path / "new_dir")
        ExportManager(output_dir=out_dir)
        assert os.path.isdir(out_dir)

    def test_global_instance(self):
        from core.export import get_export_manager, _export_manager
        import core.export as export_mod

        export_mod._export_manager = None
        mgr = get_export_manager()
        assert mgr is not None
        export_mod._export_manager = None
