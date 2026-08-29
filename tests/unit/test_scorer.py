"""Tests for the prospect scoring engine."""

import pytest
from datetime import datetime, timedelta
from core.models.conversation import Conversation, ExtractedEntity
from core.intelligence.scorer import (
    score_prospect,
    calculate_recency_decay,
    validate_score_inputs,
)


def _make_conv(urgency="high", deal_size=50000.0, days_old=0):
    return Conversation(
        source="email",
        participants=[{"name": "John Doe", "email": "john@test.com"}],
        date=datetime.utcnow() - timedelta(days=days_old),
        raw_text="Great meeting yesterday. Will send proposal by Friday.",
        commitments=["send proposal by Friday"],
        urgency=urgency,
        deal_size=deal_size,
    )


class TestScoreProspect:
    def test_basic_scoring(self):
        conv = _make_conv(urgency="high", deal_size=50000)
        scored = score_prospect(conversation=conv)
        assert scored.priority_score > 0.0
        assert scored.priority_score <= 1.0
        assert scored.conversation_id == conv.id
        assert scored.status == "queued"

    def test_none_conversation_raises(self):
        with pytest.raises(ValueError, match="None"):
            score_prospect(conversation=None)

    def test_high_urgency_higher_score(self):
        conv_high = _make_conv(urgency="high", days_old=0)
        conv_low = _make_conv(urgency="low", days_old=0)
        scored_high = score_prospect(conversation=conv_high)
        scored_low = score_prospect(conversation=conv_low)
        # High urgency should give at least as high a score
        assert scored_high.priority_score >= scored_low.priority_score

    def test_larger_deal_higher_score(self):
        conv_big = _make_conv(deal_size=100000)
        conv_small = _make_conv(deal_size=1000)
        scored_big = score_prospect(conversation=conv_big)
        scored_small = score_prospect(conversation=conv_small)
        assert scored_big.priority_score >= scored_small.priority_score

    def test_fresher_contact_higher_score(self):
        conv_fresh = _make_conv(days_old=0)
        conv_old = _make_conv(days_old=30)
        scored_fresh = score_prospect(conversation=conv_fresh)
        scored_old = score_prospect(conversation=conv_old)
        assert scored_fresh.priority_score >= scored_old.priority_score

    def test_sla_deadline_set_correctly(self):
        conv = _make_conv(urgency="high")
        scored = score_prospect(conversation=conv)
        now = datetime.utcnow()
        hours_until = (scored.sla_deadline - now).total_seconds() / 3600
        assert 20 <= hours_until <= 28  # ~24 hours with tolerance

    def test_custom_engagement_probability(self):
        conv = _make_conv()
        scored_low = score_prospect(conversation=conv, engagement_probability=0.1)
        scored_high = score_prospect(conversation=conv, engagement_probability=0.9)
        assert scored_high.priority_score >= scored_low.priority_score

    def test_custom_deal_value(self):
        conv = _make_conv(deal_size=0)
        scored = score_prospect(conversation=conv, deal_value=75000)
        assert scored.deal_value_normalized > 0

    def test_clamped_engagement(self):
        conv = _make_conv()
        scored = score_prospect(conversation=conv, engagement_probability=2.0)
        assert scored.engagement_probability == 1.0

        scored_neg = score_prospect(conversation=conv, engagement_probability=-0.5)
        assert scored_neg.engagement_probability == 0.0

    def test_negative_recency_days_clamped(self):
        conv = _make_conv()
        scored = score_prospect(conversation=conv, recency_days=-5)
        assert scored.recency_days == 0.0

    def test_score_range_bounds(self):
        for urgency in ["high", "medium", "low"]:
            for deal in [0, 50000, 200000]:
                conv = _make_conv(urgency=urgency, deal_size=deal)
                scored = score_prospect(conversation=conv)
                assert 0.0 <= scored.priority_score <= 1.0, (
                    f"Score {scored.priority_score} out of range for urgency={urgency}, deal={deal}"
                )

    def test_recency_days_calculated(self):
        conv = _make_conv(days_old=5)
        scored = score_prospect(conversation=conv)
        assert scored.recency_days >= 5


class TestCalculateRecencyDecay:
    def test_zero_days(self):
        assert calculate_recency_decay(0) == 1.0

    def test_one_week(self):
        decay = calculate_recency_decay(7)
        # 1/(1+7/7) = 0.5 exactly
        assert 0.45 <= decay <= 0.55

    def test_two_weeks(self):
        decay = calculate_recency_decay(14)
        assert 0.3 < decay < 0.4

    def test_negative_days(self):
        assert calculate_recency_decay(-5) == 1.0

    def test_large_days(self):
        decay = calculate_recency_decay(365)
        assert decay < 0.05

    def test_custom_half_life(self):
        decay_3 = calculate_recency_decay(3, half_life=3)
        # 1/(1+3/3) = 0.5 exactly
        assert 0.45 <= decay_3 <= 0.55


class TestValidateScoreInputs:
    def test_valid_inputs(self):
        result = validate_score_inputs("high", 50000, 0.8, 3)
        assert result["has_errors"] is False
        assert result["urgency"] == "high"

    def test_invalid_urgency(self):
        result = validate_score_inputs("invalid", 50000, 0.5, 0)
        assert result["has_errors"] is True
        assert "Invalid urgency" in result["error_messages"][0]

    def test_clamp_engagement(self):
        result = validate_score_inputs("high", 0, 5.0, 0)
        assert result["engagement"] == 1.0

    def test_clamp_negative_deal(self):
        result = validate_score_inputs("high", -100, 0.5, 0)
        assert result["deal_value"] == 0.0

    def test_none_urgency_defaults(self):
        result = validate_score_inputs(None, 0, 0.5, 0)
        assert result["urgency"] == "low"
