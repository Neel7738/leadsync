"""Tests for the next-best-action engine."""

import pytest
from datetime import datetime
from core.models.conversation import Conversation
from core.intelligence.action_engine import determine_next_best_action


def _make_conv(urgency="high", deal_size=50000.0, raw_text="test"):
    return Conversation(
        source="email",
        participants=[{"name": "John", "email": "j@e.com"}],
        date=datetime.utcnow(),
        raw_text=raw_text,
        commitments=["send proposal"],
        urgency=urgency,
        deal_size=deal_size,
    )


class TestDetermineNextBestAction:
    def test_basic_action(self):
        conv = _make_conv(urgency="high", deal_size=50000)
        action = determine_next_best_action(conversation=conv)
        assert "action_type" in action
        assert action["action_type"] in ("close", "re-engage", "nurture", "escalate")
        assert "timing_recommendation" in action
        assert "rationale" in action
        assert "priority_score" in action
        assert "escalation_level" in action

    def test_high_urgency_high_value(self):
        conv = _make_conv(urgency="high", deal_size=100000)
        action = determine_next_best_action(conversation=conv)
        # High urgency + high value should produce close or re-engage
        assert action["action_type"] in ("close", "re-engage")

    def test_low_urgency_low_value(self):
        conv = _make_conv(urgency="low", deal_size=100)
        action = determine_next_best_action(conversation=conv)
        # Should be nurture or escalate
        assert action["action_type"] in ("nurture", "escalate")

    def test_with_pipeline_context(self):
        conv = _make_conv(urgency="medium", deal_size=30000)
        context = {"rep_workload": 35, "rep_closing_deals": 5}
        action = determine_next_best_action(conversation=conv, pipeline_context=context)
        assert action["action_type"] in ("close", "re-engage", "nurture", "escalate")
        # With high workload, escalation may be manager level
        assert action["escalation_level"] in ("none", "manager", "executive")

    def test_all_actions_have_required_keys(self):
        conv = _make_conv()
        action = determine_next_best_action(conversation=conv)
        required = {"action_type", "timing_recommendation", "rationale", "priority_score", "escalation_level"}
        assert required.issubset(action.keys())

    def test_escalation_level_valid(self):
        conv = _make_conv()
        action = determine_next_best_action(conversation=conv)
        assert action["escalation_level"] in ("none", "manager", "executive")
