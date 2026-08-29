"""Tests for Pydantic data models."""

import pytest
from datetime import datetime, timedelta
from core.models.conversation import Conversation, ExtractedEntity
from core.models.prospect import ScoredProspect
from core.models.message import FollowUpDraft


class TestExtractedEntity:
    def test_defaults(self):
        entity = ExtractedEntity()
        assert entity.name is None
        assert entity.company is None
        assert entity.sentiment_score == 0.0
        assert entity.commitment is None

    def test_full_entity(self):
        entity = ExtractedEntity(
            name="John Doe",
            company="Acme Corp",
            sentiment_score=0.85,
            commitment="send proposal",
        )
        assert entity.name == "John Doe"
        assert entity.company == "Acme Corp"
        assert entity.sentiment_score == 0.85

    def test_sentiment_score_bounds(self):
        entity = ExtractedEntity(sentiment_score=1.0)
        assert entity.sentiment_score == 1.0
        with pytest.raises(Exception):
            ExtractedEntity(sentiment_score=1.5)

    def test_json_serialization(self):
        entity = ExtractedEntity(name="Test", sentiment_score=0.5)
        data = entity.model_dump()
        assert data["name"] == "Test"
        assert data["sentiment_score"] == 0.5


class TestConversation:
    def test_valid_conversation(self):
        conv = Conversation(
            source="email",
            participants=[{"name": "John", "email": "john@test.com"}],
            date=datetime.utcnow(),
            raw_text="Hello, let's discuss the proposal.",
            commitments=["send proposal by Friday"],
        )
        assert conv.id  # Auto-generated UUID
        assert conv.source == "email"
        assert len(conv.participants) == 1
        assert conv.raw_text == "Hello, let's discuss the proposal."

    def test_empty_raw_text_raises(self):
        with pytest.raises(ValueError):
            Conversation(
                source="email",
                participants=[],
                raw_text="",
            )

    def test_invalid_source_raises(self):
        with pytest.raises(ValueError):
            Conversation(
                source="invalid_source",
                participants=[],
                raw_text="test",
            )

    def test_valid_sources(self):
        for source in ["email", "call", "meeting"]:
            conv = Conversation(source=source, participants=[], raw_text="test")
            assert conv.source == source

    def test_default_values(self):
        conv = Conversation(source="email", participants=[], raw_text="test")
        assert conv.sentiment == "neutral"
        assert conv.urgency == "low"
        assert conv.deal_size is None
        assert len(conv.commitments) == 0

    def test_custom_id(self):
        conv = Conversation(
            source="email",
            participants=[],
            raw_text="test",
            id="custom-123",
        )
        assert conv.id == "custom-123"

    def test_json_roundtrip(self):
        conv = Conversation(
            source="email",
            participants=[{"name": "Test", "email": "t@e.com"}],
            raw_text="test content",
            commitments=["commitment1"],
            urgency="high",
            deal_size=50000.0,
        )
        data = conv.model_dump()
        conv2 = Conversation(**data)
        assert conv2.source == "email"
        assert conv2.urgency == "high"
        assert conv2.deal_size == 50000.0


class TestScoredProspect:
    def _make_conv(self, urgency="high", deal_size=50000.0):
        return Conversation(
            source="email",
            participants=[{"name": "John", "email": "j@e.com"}],
            date=datetime.utcnow(),
            raw_text="Test conversation with commitments.",
            commitments=["send proposal"],
            urgency=urgency,
            deal_size=deal_size,
        )

    def test_scoring_produces_valid_score(self):
        conv = self._make_conv(urgency="high", deal_size=50000)
        scored = ScoredProspect(
            conversation_id=conv.id,
            priority_score=0.0,  # Will be recalculated
            conversation=conv,
            sla_deadline=datetime.utcnow() + timedelta(hours=24),
        )
        assert 0.0 <= scored.priority_score <= 1.0

    def test_urgency_high_gives_higher_score(self):
        conv_high = self._make_conv(urgency="high")
        conv_low = self._make_conv(urgency="low")

        scored_high = ScoredProspect(
            conversation_id="h",
            priority_score=0.0,
            conversation=conv_high,
            sla_deadline=datetime.utcnow() + timedelta(hours=24),
        )
        scored_low = ScoredProspect(
            conversation_id="l",
            priority_score=0.0,
            conversation=conv_low,
            sla_deadline=datetime.utcnow() + timedelta(hours=72),
        )
        assert scored_high.priority_score >= scored_low.priority_score

    def test_sla_deadline_varies_by_urgency(self):
        now = datetime.utcnow()
        for urgency, hours in [("high", 24), ("medium", 48), ("low", 72)]:
            conv = self._make_conv(urgency=urgency)
            scored = ScoredProspect(
                conversation_id=f"test-{urgency}",
                priority_score=0.5,
                conversation=conv,
                sla_deadline=now + timedelta(hours=hours),
            )
            diff = (scored.sla_deadline - now).total_seconds() / 3600
            assert abs(diff - hours) < 1


class TestFollowUpDraft:
    def test_draft_creation(self):
        draft = FollowUpDraft(
            prospect_id="p1",
            conversation_id="c1",
            variant_agreeable="Hello, let's connect!",
            variant_direct="Following up on our meeting.",
            variant_soft="Just checking in!",
        )
        assert draft.prospect_id == "p1"
        assert draft.sent is False
        assert draft.send_attempts == 0
        assert draft.selected_variant is None

    def test_draft_defaults(self):
        draft = FollowUpDraft(
            prospect_id="p1",
            conversation_id="c1",
        )
        assert draft.variant_agreeable == ""
        assert draft.variant_direct == ""
        assert draft.variant_soft == ""
