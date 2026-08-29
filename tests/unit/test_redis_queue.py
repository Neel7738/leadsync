"""Tests for Redis-backed priority queue.

Uses a mock Redis client so tests run without a live Redis server.
"""

import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, PropertyMock
from core.models.conversation import Conversation
from core.models.prospect import ScoredProspect


def _make_conv(urgency="high"):
    return Conversation(
        source="email",
        participants=[{"name": "Test User", "email": "test@example.com"}],
        date=datetime.utcnow(),
        raw_text="Test conversation text for scoring.",
        urgency=urgency,
        deal_size=50000.0,
        commitments=["send proposal by Friday"],
    )


def _make_scored(cid="c1", urgency="high", priority=0.8):
    conv = _make_conv(urgency=urgency)
    conv.id = cid
    return ScoredProspect(
        conversation_id=cid,
        priority_score=priority,
        conversation=conv,
        sla_deadline=datetime.utcnow() + timedelta(hours=24),
    )


class FakeRedis:
    """In-memory fake Redis for testing without a real server."""

    def __init__(self):
        self._data = {}
        self._sorted_sets = {}
        self._hashes = {}

    def ping(self):
        return True

    def zadd(self, name, mapping):
        if name not in self._sorted_sets:
            self._sorted_sets[name] = {}
        self._sorted_sets[name].update(mapping)

    def zrange(self, name, start, end, withscores=False):
        if name not in self._sorted_sets:
            return []
        items = sorted(self._sorted_sets[name].items(), key=lambda x: x[1])
        if end == -1:
            end = len(items)
        else:
            end = end + 1
        sliced = items[start:end]
        if withscores:
            return [(member, score) for member, score in sliced]
        return [member for member, _ in sliced]

    def zrem(self, name, *members):
        if name not in self._sorted_sets:
            return 0
        removed = 0
        for m in members:
            if m in self._sorted_sets[name]:
                del self._sorted_sets[name][m]
                removed += 1
        return removed

    def zcard(self, name):
        return len(self._sorted_sets.get(name, {}))

    def set(self, key, value):
        self._data[key] = value

    def get(self, key):
        return self._data.get(key)

    def delete(self, *keys):
        removed = 0
        for k in keys:
            if k in self._data:
                del self._data[k]
                removed += 1
            # Remove sorted set containers
            if k in self._sorted_sets:
                del self._sorted_sets[k]
                removed += 1
            # Remove members from sorted sets
            for sname in list(self._sorted_sets.keys()):
                if k in self._sorted_sets[sname]:
                    del self._sorted_sets[sname][k]
                    removed += 1
        return removed

    def scan_iter(self, match):
        import fnmatch
        for key in list(self._data.keys()) + list(self._sorted_sets.keys()):
            if fnmatch.fnmatch(key, match):
                yield key

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    """Fake Redis pipeline that executes commands immediately."""

    def __init__(self, redis):
        self._redis = redis
        self._commands = []

    def zadd(self, name, mapping):
        self._commands.append(("zadd", name, mapping))
        return self

    def set(self, key, value):
        self._commands.append(("set", key, value))
        return self

    def zrem(self, name, *members):
        self._commands.append(("zrem", name, list(members)))
        return self

    def delete(self, *keys):
        self._commands.append(("delete", *keys))
        return self

    def execute(self):
        results = []
        for cmd in self._commands:
            if cmd[0] == "zadd":
                self._redis.zadd(cmd[1], cmd[2])
                results.append(True)
            elif cmd[0] == "zrem":
                self._redis.zrem(cmd[1], *cmd[2])
                results.append(0)
            elif cmd[0] == "set":
                self._redis.set(cmd[1], cmd[2])
                results.append(True)
            elif cmd[0] == "delete":
                self._redis.delete(cmd[1])
                results.append(0)
        self._commands.clear()
        return results


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def redis_queue(fake_redis):
    """Create a RedisPriorityQueue with a fake Redis client."""
    from core.queue.redis_queue import RedisPriorityQueue

    q = RedisPriorityQueue.__new__(RedisPriorityQueue)
    q._prefix = "test"
    q._client = fake_redis
    return q


class TestRedisPriorityQueue:
    def test_add_and_size(self, redis_queue):
        assert redis_queue.size() == 0
        redis_queue.add(_make_scored("c1"))
        assert redis_queue.size() == 1
        redis_queue.add(_make_scored("c2"))
        assert redis_queue.size() == 2

    def test_add_invalid_type_raises(self, redis_queue):
        with pytest.raises(TypeError):
            redis_queue.add("not a prospect")

    def test_pop_next(self, redis_queue):
        redis_queue.add(_make_scored("c1", urgency="high", priority=0.9))
        redis_queue.add(_make_scored("c2", urgency="low", priority=0.3))
        item = redis_queue.pop_next()
        assert item is not None
        assert item.conversation_id == "c1"  # Higher priority first
        assert redis_queue.size() == 1

    def test_pop_empty(self, redis_queue):
        assert redis_queue.pop_next() is None

    def test_list_sorted(self, redis_queue):
        redis_queue.add(_make_scored("c1", priority=0.3))
        redis_queue.add(_make_scored("c2", priority=0.9))
        redis_queue.add(_make_scored("c3", priority=0.6))
        items = redis_queue.list()
        assert len(items) == 3
        assert items[0].conversation_id == "c2"  # Highest priority first

    def test_get_by_id(self, redis_queue):
        redis_queue.add(_make_scored("c1"))
        item = redis_queue.get_by_id("c1")
        assert item is not None
        assert item.conversation_id == "c1"
        assert redis_queue.get_by_id("nonexistent") is None

    def test_remove(self, redis_queue):
        redis_queue.add(_make_scored("c1"))
        assert redis_queue.remove("c1") is True
        assert redis_queue.size() == 0
        assert redis_queue.remove("nonexistent") is False

    def test_increment_requeue(self, redis_queue):
        redis_queue.add(_make_scored("c1"))
        result = redis_queue.increment_requeue("c1")
        assert result["times_requeued"] == 1
        result = redis_queue.increment_requeue("c1")
        assert result["times_requeued"] == 2
        assert redis_queue.increment_requeue("nonexistent") is None

    def test_get_queue_stats(self, redis_queue):
        redis_queue.add(_make_scored("c1", priority=0.9))
        redis_queue.add(_make_scored("c2", priority=0.3))
        stats = redis_queue.get_queue_stats()
        assert stats["total_items"] == 2
        assert stats["avg_priority"] > 0
        assert stats["breached_count"] == 0

    def test_empty_stats(self, redis_queue):
        stats = redis_queue.get_queue_stats()
        assert stats["total_items"] == 0
        assert stats["avg_priority"] == 0.0

    def test_sla_breach_detection(self, redis_queue):
        scored = _make_scored("c1")
        scored.sla_deadline = datetime.utcnow() - timedelta(hours=1)  # Already breached
        redis_queue.add(scored)
        breached = redis_queue.get_breached()
        assert len(breached) == 1
        assert breached[0].conversation_id == "c1"

    def test_check_sla_breaches(self, redis_queue):
        scored = _make_scored("c1")
        scored.sla_deadline = datetime.utcnow() - timedelta(hours=1)
        redis_queue.add(scored)
        breaches = redis_queue.check_sla_breaches()
        assert len(breaches) == 1
        assert "conversation_id" in breaches[0]
        assert "sla_deadline" in breaches[0]

    def test_flush(self, redis_queue):
        redis_queue.add(_make_scored("c1"))
        redis_queue.add(_make_scored("c2"))
        assert redis_queue.size() == 2
        count = redis_queue.flush()
        assert redis_queue.size() == 0

    def test_ping(self, redis_queue):
        assert redis_queue.ping() is True

    def test_pop_breached_first(self, redis_queue):
        """Breached prospects should be popped before normal ones."""
        normal = _make_scored("c_normal", priority=0.9)
        breached = _make_scored("c_breached", priority=0.3)
        breached.sla_deadline = datetime.utcnow() - timedelta(hours=1)
        redis_queue.add(normal)
        redis_queue.add(breached)

        # The breached item should come first despite lower priority
        item = redis_queue.pop_next()
        assert item.conversation_id == "c_breached"

    def test_roundtrip_serialization(self, redis_queue):
        """Prospect data survives serialize/deserialize through Redis."""
        scored = _make_scored("c1", urgency="medium")
        scored.deal_value_normalized = 0.6
        scored.engagement_probability = 0.75
        redis_queue.add(scored)

        retrieved = redis_queue.get_by_id("c1")
        assert retrieved is not None
        assert retrieved.priority_score == scored.priority_score
        assert retrieved.conversation.urgency == "medium"
        assert retrieved.engagement_probability == 0.75

    def test_pop_removes_data(self, redis_queue):
        redis_queue.add(_make_scored("c1"))
        redis_queue.pop_next()
        assert redis_queue.get_by_id("c1") is None
        assert redis_queue.size() == 0
