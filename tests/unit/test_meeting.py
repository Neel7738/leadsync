"""Tests for meeting notes ingestion."""

import pytest
from core.ingest.meeting import (
    process_meeting_notes,
    _extract_commitments_from_notes,
    _determine_urgency_from_notes,
    _extract_deal_size,
    _extract_participants_from_text,
)


class TestProcessMeetingNotes:
    def test_basic_notes(self):
        notes = """
        Meeting with John Doe from Acme Corp.
        Discussed pricing and timeline.
        Action Item: Send proposal by Friday
        Next Step: Schedule follow-up call
        """
        conv = process_meeting_notes(notes)
        assert conv.source == "meeting"
        assert len(conv.raw_text) > 0
        assert len(conv.participants) > 0

    def test_empty_notes_raises(self):
        with pytest.raises(ValueError):
            process_meeting_notes("")

        with pytest.raises(ValueError):
            process_meeting_notes("   ")

    def test_commitments_extracted(self):
        notes = "Action Item: Send proposal by Friday. Deadline: Monday EOD."
        conv = process_meeting_notes(notes)
        assert len(conv.commitments) > 0

    def test_urgency_detection(self):
        notes_high = "This is urgent! We need this immediately."
        conv_high = process_meeting_notes(notes_high)
        assert conv_high.urgency == "high"

        notes_medium = "Follow up next week with the proposal."
        conv_medium = process_meeting_notes(notes_medium)
        assert conv_medium.urgency == "medium"

    def test_participants_extracted(self):
        notes = "Attendees: John Smith, Jane Doe, Bob Wilson"
        conv = process_meeting_notes(notes)
        names = [p["name"] for p in conv.participants]
        assert any("John" in n for n in names)

    def test_freeform_notes(self):
        notes = """
        Just had a great chat with the team about the new project.
        They're excited about the potential and want to move forward.
        We agreed to review the budget numbers next Tuesday.
        """
        conv = process_meeting_notes(notes)
        assert conv.source == "meeting"
        assert conv.sentiment in ("positive", "negative", "neutral")


class TestExtractCommitments:
    def test_action_items(self):
        text = "Action Item: Send proposal. Next Step: Call client."
        commitments = _extract_commitments_from_notes(text)
        assert len(commitments) > 0

    def test_keyword_based(self):
        text = "We will send the proposal by Friday and call the client Monday."
        commitments = _extract_commitments_from_notes(text)
        assert len(commitments) > 0


class TestDetermineUrgency:
    def test_high_urgency(self):
        assert _determine_urgency_from_notes("This is urgent and needs immediate attention.") == "high"

    def test_medium_urgency(self):
        assert _determine_urgency_from_notes("Follow up next week with the team.") == "medium"

    def test_low_urgency(self):
        assert _determine_urgency_from_notes("We had a nice chat about the weather.") == "low"


class TestExtractDealSize:
    def test_dollar_amount(self):
        assert _extract_deal_size("The deal is worth $50,000") == 50000.0

    def test_k_notation(self):
        result = _extract_deal_size("Budget is 100k")
        assert result is not None
        assert result >= 100000

    def test_no_amount(self):
        assert _extract_deal_size("No money mentioned here.") is None


class TestExtractParticipants:
    def test_attendee_list(self):
        text = "Attendees: John Smith, Jane Doe"
        participants = _extract_participants_from_text(text)
        assert len(participants) > 0

    def test_no_participants(self):
        participants = _extract_participants_from_text("random text without names")
        assert isinstance(participants, list)
