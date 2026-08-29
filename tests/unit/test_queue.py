"""Tests for the priority queue."""

import pytest
from datetime import datetime, timedelta
from core.models.conversation import Conversation
from core.models.prospect import ScoredProspect
from core.queue import PriorityQueue, get_queue


def _make_conv(urgency="high"):
    return Conversation(
        source="email",
        participants=[{"name": "Test", "email": "t@e.com"}],
        date=datetime.utcnow(),
        raw_text="Test conversation.",
        urgency=urgency,
        deal_size=50000.0,
    )


def _make_scored(conv_id="c1", urgency="high"):
    conv = _make_conv(urgency=urgency)
    conv.id = conv_id
    return ScoredProspect(
        conversation_id=conv_id,
        priority_score=0.8 if urgency == "high" else 0.3,
        conversation=conv,
        sla_deadline=datetime.utcnow() + timedelta(hours=24),
    )


class TestPriorityQueue:
    def test_add_and_size(self):
        q = PriorityQueue()
        assert q.size() == 0
        q.add(_make_scored("c1"))
        assert q.size() == 1
        q.add(_make_scored("c2"))
        assert q.size() == 2

    def test_pop_next(self):
        q = PriorityQueue()
        q.add(_make_scored("c1", urgency="high"))
        q.add(_make_scored("c2", urgency="low"))
        item = q.pop_next()
        assert item is not None
        assert item.conversation_id == "c1"  # Higher priority first
        assert q.size() == 1

    def test_pop_empty_queue(self):
        q = PriorityQueue()
        assert q.pop_next() is None

    def test_list_sorted(self):
        q = PriorityQueue()
        q.add(_make_scored("c1", urgency="low"))
        q.add(_make_scored("c2", urgency="high"))
        q.add(_make_scored("c3", urgency="medium"))
        items = q.list()
        assert len(items) == 3
        assert items[0].conversation_id == "c2"  # Highest priority first

    def test_remove(self):
        q = PriorityQueue()
        q.add(_make_scored("c1"))
        assert q.remove("c1") is True
        assert q.size() == 0
        assert q.remove("nonexistent") is False

    def test_get_by_id(self):
        q = PriorityQueue()
        q.add(_make_scored("c1"))
        item = q.get_by_id("c1")
        assert item is not None
        assert q.get_by_id("nonexistent") is None

    def test_increment_requeue(self):
        q = PriorityQueue()
        q.add(_make_scored("c1"))
        result = q.increment_requeue("c1")
        assert result["times_requeued"] == 1
        result = q.increment_requeue("c1")
        assert result["times_requeued"] == 2
        assert q.increment_requeue("nonexistent") is None

    def test_queue_stats(self):
        q = PriorityQueue()
        q.add(_make_scored("c1", urgency="high"))
        q.add(_make_scored("c2", urgency="low"))
        stats = q.get_queue_stats()
        assert stats["total_items"] == 2
        assert stats["avg_priority"] > 0

    def test_invalid_type_raises(self):
        q = PriorityQueue()
        with pytest.raises(TypeError):
            q.add("not a ScoredProspect")

    def test_sla_breach_detection(self):
        q = PriorityQueue()
        scored = _make_scored("c1")
        scored.sla_deadline = datetime.utcnow() - timedelta(hours=1)  # Already breached
        q.add(scored)
        breached = q.get_breached()
        assert len(breached) == 1
        assert breached[0].conversation_id == "c1"


class TestGlobalQueue:
    def test_singleton(self):
        q1 = get_queue()
        q2 = get_queue()
        assert q1 is q2
