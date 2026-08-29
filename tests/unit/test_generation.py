"""Tests for draft generation and selection."""

import pytest
from datetime import datetime
from core.models.conversation import Conversation
from core.generation.prompt import (
    generate_prompt,
    generate_drafts,
    select_draft,
    generate_llm_completion,
    TONE_GUIDES,
    PROMPT_TEMPLATES,
)


def _make_conv(urgency="medium"):
    return Conversation(
        source="email",
        participants=[{"name": "John Doe", "email": "john@test.com"}],
        date=datetime.utcnow(),
        raw_text="Great meeting. We discussed pricing and timelines.",
        commitments=["send proposal by Friday", "schedule follow-up call"],
        urgency=urgency,
    )


class TestGeneratePrompt:
    def test_agreeable_prompt(self):
        conv = _make_conv()
        prompt = generate_prompt(
            tone="agreeable",
            conversation=conv,
            prospect_name="John",
            company="Acme Corp",
            role="VP Engineering",
            pain_points=["high costs", "slow deployment"],
            followup_count=1,
            urgency_level="high",
        )
        assert "John" in prompt
        assert "Acme Corp" in prompt
        assert "VP Engineering" in prompt
        assert "high costs" in prompt
        assert "send proposal by Friday" in prompt

    def test_direct_prompt(self):
        conv = _make_conv()
        prompt = generate_prompt(
            tone="direct",
            conversation=conv,
            prospect_name="Jane",
            company="TechCo",
            role="CTO",
            pain_points=[],
            followup_count=0,
            urgency_level="low",
        )
        assert "Jane" in prompt
        assert "TechCo" in prompt

    def test_soft_prompt(self):
        conv = _make_conv()
        prompt = generate_prompt(
            tone="soft",
            conversation=conv,
            prospect_name="Bob",
            company="StartupX",
            role="CEO",
            pain_points=["scalability"],
            followup_count=2,
            urgency_level="medium",
        )
        assert "Bob" in prompt

    def test_invalid_tone_raises(self):
        conv = _make_conv()
        with pytest.raises(ValueError, match="Unsupported tone"):
            generate_prompt(
                tone="aggressive",
                conversation=conv,
                prospect_name="Test",
                company="Co",
                role="Role",
                pain_points=[],
                followup_count=0,
                urgency_level="low",
            )

    def test_sequence_position(self):
        conv = _make_conv()
        # First follow-up
        prompt = generate_prompt(
            tone="direct", conversation=conv, prospect_name="T",
            company="", role="", pain_points=[], followup_count=0,
            urgency_level="medium",
        )
        assert "first follow-up" in prompt.lower()

        # Third follow-up
        prompt = generate_prompt(
            tone="direct", conversation=conv, prospect_name="T",
            company="", role="", pain_points=[], followup_count=3,
            urgency_level="medium",
        )
        assert "breakup" in prompt.lower() or "final" in prompt.lower()


class TestGenerateDrafts:
    def test_three_variants_no_llm(self):
        conv = _make_conv()
        drafts = generate_drafts(
            conversation=conv,
            prospect_name="John",
            company="Acme",
            role="VP",
            pain_points=["cost"],
            followup_count=0,
            urgency_level="medium",
            use_llm=False,
        )
        assert "variant_agreeable" in drafts
        assert "variant_direct" in drafts
        assert "variant_soft" in drafts
        assert len(drafts["variant_agreeable"]) > 10
        assert len(drafts["variant_direct"]) > 10
        assert len(drafts["variant_soft"]) > 10

    def test_drafts_contain_prospect_name(self):
        conv = _make_conv()
        drafts = generate_drafts(
            conversation=conv,
            prospect_name="Sarah",
            company="Co",
            role="Role",
            pain_points=[],
            followup_count=0,
            urgency_level="medium",
            use_llm=False,
        )
        assert "Sarah" in drafts["variant_agreeable"]

    def test_different_tones_different_content(self):
        conv = _make_conv()
        drafts = generate_drafts(
            conversation=conv,
            prospect_name="Test",
            company="Co",
            role="Role",
            pain_points=[],
            followup_count=0,
            urgency_level="medium",
            use_llm=False,
        )
        # At least 2 of the 3 should differ
        texts = [drafts["variant_agreeable"], drafts["variant_direct"], drafts["variant_soft"]]
        assert not (texts[0] == texts[1] == texts[2])


class TestSelectDraft:
    def test_rep_preference(self):
        drafts = {
            "variant_agreeable": "a",
            "variant_direct": "d",
            "variant_soft": "s",
        }
        assert select_draft(drafts, rep_preference="direct") == "variant_direct"
        assert select_draft(drafts, rep_preference="soft") == "variant_soft"

    def test_prospect_history(self):
        drafts = {
            "variant_agreeable": "a",
            "variant_direct": "d",
            "variant_soft": "s",
        }
        assert select_draft(drafts, prospect_history={"last_tone": "soft"}) == "variant_soft"

    def test_urgency_based_default(self):
        drafts = {
            "variant_agreeable": "a",
            "variant_direct": "d",
            "variant_soft": "s",
        }
        assert select_draft(drafts, urgency_level="high") == "variant_direct"
        assert select_draft(drafts, urgency_level="medium") == "variant_agreeable"
        assert select_draft(drafts, urgency_level="low") == "variant_soft"

    def test_preference_over_history(self):
        drafts = {
            "variant_agreeable": "a",
            "variant_direct": "d",
            "variant_soft": "s",
        }
        result = select_draft(
            drafts,
            rep_preference="direct",
            prospect_history={"last_tone": "soft"},
        )
        assert result == "variant_direct"

    def test_invalid_preference_falls_back(self):
        drafts = {"variant_agreeable": "a"}
        assert select_draft(drafts, rep_preference="invalid") == "variant_agreeable"


class TestToneGuides:
    def test_all_tones_present(self):
        assert "agreeable" in TONE_GUIDES
        assert "direct" in TONE_GUIDES
        assert "soft" in TONE_GUIDES

    def test_templates_match_tones(self):
        for tone in TONE_GUIDES:
            assert tone in PROMPT_TEMPLATES
