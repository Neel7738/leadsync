"""Tests for speech-to-text module."""

import pytest
import os
import tempfile
from unittest.mock import patch, MagicMock
from core.ingest.stt import (
    transcribe_audio,
    process_call_audio,
    _extract_entities_from_transcript,
    _determine_urgency,
    _extract_commitments_from_transcript,
)


class TestTranscribeAudio:
    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            transcribe_audio("/nonexistent/file.wav")

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"")
            f.flush()
            path = f.name
        try:
            with pytest.raises(ValueError, match="empty"):
                transcribe_audio(path)
        finally:
            os.unlink(path)

    @patch("core.ingest.stt.WHISPER_AVAILABLE", False)
    def test_no_whisper_returns_metadata(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"fake audio data")
            f.flush()
            path = f.name
        try:
            result = transcribe_audio(path)
            assert result["text"] == ""
            assert result["model_used"] == "none"
            assert "warning" in result
        finally:
            os.unlink(path)


class TestDetermineUrgency:
    def test_high(self):
        assert _determine_urgency("This is urgent and needs immediate attention.") == "high"
        assert _determine_urgency("ASAP we need to close this.") == "high"

    def test_medium(self):
        assert _determine_urgency("Let's follow up soon this week.") == "medium"
        assert _determine_urgency("Next step is to schedule a call.") == "medium"

    def test_low(self):
        assert _determine_urgency("Just wanted to touch base about the project.") == "low"
        assert _determine_urgency("No rush, whenever you get a chance.") == "low"


class TestExtractCommitmentsFromTranscript:
    def test_will_commitments(self):
        text = "I will send the proposal by Friday. We'll schedule a follow-up call."
        commitments = _extract_commitments_from_transcript(text)
        assert len(commitments) > 0

    def test_empty(self):
        assert _extract_commitments_from_transcript("") == []
        assert _extract_commitments_from_transcript("Just chatting about weather.") == []


class TestExtractEntitiesFromTranscript:
    @patch("core.intelligence.llm_manager.llm_manager.generate")
    def test_llm_extraction(self, mock_gen):
        mock_gen.return_value = MagicMock(
            content='{"name": "John", "company": "Acme", "sentiment": "positive", "sentiment_score": 0.8}'
        )
        import json
        mock_gen.return_value.content = json.dumps({
            "name": "John",
            "company": "Acme",
            "sentiment": "positive",
            "sentiment_score": 0.8,
        })
        entity = _extract_entities_from_transcript("Test transcript")
        assert entity.name == "John"

    def test_heuristic_fallback(self):
        with patch("core.intelligence.llm_manager.llm_manager.generate", side_effect=Exception("fail")):
            entity = _extract_entities_from_transcript("Some text")
            assert entity.sentiment == "neutral"
