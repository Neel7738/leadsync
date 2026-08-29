"""Integration test for the full autonomous pipeline."""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock
from core.models.conversation import Conversation
from core.intelligence.llm_manager import LLMManager, LLMResponse
from core.generation.prompt import generate_drafts
from core.intelligence.llm_manager import llm_manager
from core.intelligence.orchestrator import SalesAutonomousAgent


def _make_conv(urgency="high"):
    return Conversation(
        source="email",
        participants=[{"name": "John Doe", "email": "john@test.com"}],
        date=datetime.utcnow(),
        raw_text="I am very interested in your product, but I need to see the pricing first. Can we call tomorrow?",
        commitments=["Call tomorrow to discuss pricing"],
        urgency=urgency,
        deal_size=50000.0,
    )


METADATA = {
    "name": "John Doe",
    "company": "Acme Corp",
    "role": "VP Engineering",
    "pain_points": ["Too expensive", "Lack of scalability"],
    "deal_value": 50000,
    "engagement_prob": 0.8,
    "followup_count": 0,
}


class TestAutonomousPipeline:
    def test_full_pipeline_success(self):
        agent = SalesAutonomousAgent()
        conv = _make_conv(urgency="high")

        with patch("core.intelligence.llm_manager.llm_manager.generate") as mock_gen:
            mock_gen.return_value = LLMResponse(
                content="Hi John, I'd love to discuss pricing tomorrow. Does 10am work?",
                provider="ollama",
                model="llama3.2:1b",
                latency=0.1,
            )
            result = agent.process_conversation(conv, METADATA)

        assert result["status"] == "success"
        assert "action_plan" in result
        assert result["action_plan"]["action_type"] in ("close", "re-engage", "nurture", "escalate")
        assert "drafts" in result

    def test_pipeline_with_low_urgency(self):
        agent = SalesAutonomousAgent()
        conv = _make_conv(urgency="low")

        with patch("core.intelligence.llm_manager.llm_manager.generate") as mock_gen:
            mock_gen.return_value = LLMResponse(
                content="Gentle follow-up draft",
                provider="ollama",
                model="llama3.2:1b",
                latency=0.1,
            )
            result = agent.process_conversation(conv, METADATA)

        assert result["status"] == "success"

    def test_local_fallback_trigger(self):
        """Test that when cloud providers fail, local Ollama is tried."""
        with patch("core.intelligence.llm_manager.llm_manager._call_provider") as mock_call:
            mock_call.side_effect = [
                Exception("Cloud timeout"),
                Exception("API Key missing"),
                LLMResponse(
                    content="Local fallback content",
                    provider="ollama",
                    model="llama3.2:1b",
                    latency=0.2,
                ),
            ]
            result = llm_manager.generate("test prompt")
            assert result.provider == "ollama"
            assert result.content == "Local fallback content"

    def test_all_providers_fail_raises(self):
        with patch("core.intelligence.llm_manager.llm_manager._call_provider") as mock_call:
            mock_call.side_effect = Exception("All failed")
            with pytest.raises(RuntimeError, match="All LLM providers"):
                llm_manager.generate("test prompt")

    def test_draft_generation_no_llm(self):
        conv = _make_conv()
        drafts = generate_drafts(
            conversation=conv,
            prospect_name="John",
            company="Acme",
            role="VP",
            pain_points=["cost"],
            followup_count=0,
            urgency_level="high",
            use_llm=False,
        )
        assert "variant_agreeable" in drafts
        assert "variant_direct" in drafts
        assert "variant_soft" in drafts
        # All should have content
        for key, val in drafts.items():
            assert len(val) > 10, f"{key} is too short"

    def test_empty_metadata_handled(self):
        agent = SalesAutonomousAgent()
        conv = _make_conv()

        with patch("core.intelligence.llm_manager.llm_manager.generate") as mock_gen:
            mock_gen.return_value = LLMResponse(
                content="Draft content",
                provider="ollama",
                model="llama3.2:1b",
                latency=0.1,
            )
            result = agent.process_conversation(conv, {})

        assert result["status"] == "success"

    def test_llm_manager_health_report(self):
        report = llm_manager.get_health_report()
        assert isinstance(report, dict)

    def test_llm_manager_cost_estimation(self):
        cost = llm_manager._estimate_cost("gpt-4o-mini", 1000)
        assert cost is not None
        assert cost > 0
        cost_local = llm_manager._estimate_cost("llama3.1:8b", 1000)
        assert cost_local is None  # Unknown model returns None
