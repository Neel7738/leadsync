"""Tests for email ingestion."""

import pytest
from core.ingest.email import (
    parse_email_to_conversation,
    _extract_commitments_from_text,
    _extract_entities_from_text,
    _heuristic_extract,
    _parse_email_address,
)


class TestParseEmailToConversation:
    def test_basic_email(self):
        data = {
            "from": "John Doe <john@example.com>",
            "to": "sales@company.com",
            "date": "2024-06-15T10:00:00",
            "subject": "Follow up on proposal",
            "body": "Hi, I'd like to discuss the proposal we talked about last week.",
        }
        conv = parse_email_to_conversation(data)
        assert conv.source == "email"
        assert len(conv.participants) > 0
        assert conv.raw_text == data["body"]

    def test_urgency_from_subject(self):
        data = {
            "from": "test@test.com",
            "subject": "URGENT: Need proposal ASAP",
            "body": "This is critical.",
        }
        conv = parse_email_to_conversation(data)
        assert conv.urgency == "high"

    def test_medium_urgency(self):
        data = {
            "from": "test@test.com",
            "subject": "Follow up soon",
            "body": "Let's follow up this week.",
        }
        conv = parse_email_to_conversation(data)
        assert conv.urgency == "medium"

    def test_participants_deduplicated(self):
        data = {
            "from": "test@test.com",
            "to": "test@test.com",
            "body": "Self-email",
        }
        conv = parse_email_to_conversation(data)
        assert len(conv.participants) == 1

    def test_missing_fields(self):
        data = {"body": "Just body text"}
        conv = parse_email_to_conversation(data)
        assert conv.source == "email"
        assert conv.participants[0]["email"] == "unknown"


class TestExtractCommitments:
    def test_commitment_keywords(self):
        text = "I will send the proposal by Friday and call the client Monday."
        commitments = _extract_commitments_from_text(text)
        assert len(commitments) > 0

    def test_empty_text(self):
        assert _extract_commitments_from_text("") == []
        assert _extract_commitments_from_text(None) == []


class TestHeuristicExtract:
    def test_positive_sentiment(self):
        text = "Great meeting! I'm excited about the excellent proposal. Thank you!"
        entity = _heuristic_extract(text)
        assert entity.sentiment in ("positive", "neutral")
        assert entity.sentiment_score >= 0.5

    def test_negative_sentiment(self):
        text = "Unfortunately we have concerns about the pricing. The issue is problematic."
        entity = _heuristic_extract(text)
        assert entity.sentiment in ("negative", "neutral")

    def test_neutral_sentiment(self):
        text = "Meeting scheduled for next Tuesday. Agenda items TBD."
        entity = _heuristic_extract(text)
        assert entity.sentiment == "neutral"

    def test_empty_text(self):
        entity = _heuristic_extract("")
        assert entity.sentiment in ("neutral", None)


class TestParseEmailAddress:
    def test_single_address(self):
        result = _parse_email_address("John Doe <john@example.com>")
        assert len(result) == 1
        assert result[0]["email"] == "john@example.com"

    def test_multiple_addresses(self):
        result = _parse_email_address("a@test.com, b@test.com")
        assert len(result) == 2

    def test_no_addresses(self):
        result = _parse_email_address("no email here")
        assert len(result) == 0
