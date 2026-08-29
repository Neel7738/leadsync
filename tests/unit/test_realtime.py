"""Tests for real-time event bus and WebSocket manager."""

import pytest
from datetime import datetime
from core.realtime import EventBus, WebSocketManager, event_bus, ws_manager, format_sse_event


class TestEventBus:
    def test_emit_and_receive(self):
        bus = EventBus()
        received = []
        bus.on("test:event", lambda e: received.append(e))

        bus.emit("test:event", {"key": "value"})

        assert len(received) == 1
        assert received[0]["type"] == "test:event"
        assert received[0]["data"]["key"] == "value"
        assert "timestamp" in received[0]

    def test_wildcard_subscriber(self):
        bus = EventBus()
        received = []
        bus.on("*", lambda e: received.append(e))

        bus.emit("queue:added", {"id": "1"})
        bus.emit("queue:popped", {"id": "2"})

        assert len(received) == 2
        assert received[0]["type"] == "queue:added"
        assert received[1]["type"] == "queue:popped"

    def test_unsubscribe(self):
        bus = EventBus()
        received = []
        callback = lambda e: received.append(e)
        bus.on("test", callback)
        bus.emit("test", {})
        assert len(received) == 1

        bus.off("test", callback)
        bus.emit("test", {})
        assert len(received) == 1  # No new event

    def test_multiple_subscribers(self):
        bus = EventBus()
        r1, r2 = [], []
        bus.on("ev", lambda e: r1.append(e))
        bus.on("ev", lambda e: r2.append(e))

        bus.emit("ev", {})
        assert len(r1) == 1
        assert len(r2) == 1

    def test_history(self):
        bus = EventBus()
        for i in range(5):
            bus.emit("ev", {"i": i})

        recent = bus.get_recent(3)
        assert len(recent) == 3
        assert recent[0]["data"]["i"] == 2
        assert recent[2]["data"]["i"] == 4

    def test_history_capped(self):
        bus = EventBus()
        bus._max_history = 5
        for i in range(10):
            bus.emit("ev", {"i": i})

        assert len(bus.get_recent(100)) == 5

    def test_callback_error_doesnt_crash(self):
        bus = EventBus()
        def bad_callback(e):
            raise ValueError("boom")

        bus.on("test", bad_callback)
        # Should not raise
        bus.emit("test", {})

    def test_emit_no_data(self):
        bus = EventBus()
        received = []
        bus.on("test", lambda e: received.append(e))
        bus.emit("test")
        assert len(received) == 1
        assert received[0]["data"] == {}


class TestWebSocketManager:
    def test_initial_state(self):
        mgr = WebSocketManager()
        assert mgr.connection_count == 0
        assert mgr.broadcast_count == 0


class TestFormatSSE:
    def test_format(self):
        result = format_sse_event("queue:added", {"id": "123"})
        assert "event: queue:added" in result
        assert '"type": "queue:added"' in result
        assert '"id": "123"' in result
        assert result.endswith("\n\n")

    def test_format_empty_data(self):
        result = format_sse_event("heartbeat", {})
        assert "event: heartbeat" in result


class TestGlobalEventBus:
    def test_singleton(self):
        assert event_bus is not None

    def test_emit_and_get_recent(self):
        event_bus.emit("test:global", {"test": True})
        recent = event_bus.get_recent(1)
        assert len(recent) >= 1
        assert recent[-1]["type"] == "test:global"


class TestQueueEventEmission:
    """Test that queue operations emit events."""

    def test_add_emits_event(self):
        from core.queue import PriorityQueue
        from core.models.conversation import Conversation
        from core.models.prospect import ScoredProspect

        bus = EventBus()
        received = []
        bus.on("queue:added", lambda e: received.append(e))

        # Patch the emit function to use our bus
        import core.queue as queue_mod
        original_emit = queue_mod._emit

        def patched_emit(event_type, **kwargs):
            bus.emit(event_type, kwargs)

        queue_mod._emit = patched_emit
        try:
            q = PriorityQueue()
            conv = Conversation(
                source="email",
                participants=[{"name": "Test", "email": "t@e.com"}],
                date=datetime.utcnow(),
                raw_text="test",
            )
            scored = ScoredProspect(
                conversation_id="test-123",
                priority_score=0.8,
                conversation=conv,
                sla_deadline=datetime.utcnow(),
            )
            q.add(scored)

            assert len(received) == 1
            assert received[0]["type"] == "queue:added"
            assert received[0]["data"]["conversation_id"] == "test-123"
        finally:
            queue_mod._emit = original_emit

    def test_pop_emits_event(self):
        from core.queue import PriorityQueue
        from core.models.conversation import Conversation
        from core.models.prospect import ScoredProspect

        bus = EventBus()
        received = []
        bus.on("queue:popped", lambda e: received.append(e))

        import core.queue as queue_mod
        original_emit = queue_mod._emit

        def patched_emit(event_type, **kwargs):
            bus.emit(event_type, kwargs)

        queue_mod._emit = patched_emit
        try:
            q = PriorityQueue()
            conv = Conversation(
                source="email",
                participants=[],
                date=datetime.utcnow(),
                raw_text="test",
            )
            scored = ScoredProspect(
                conversation_id="pop-test",
                priority_score=0.5,
                conversation=conv,
                sla_deadline=datetime.utcnow(),
            )
            q.add(scored)
            q.pop_next()

            assert len(received) == 1
            assert received[0]["type"] == "queue:popped"
            assert received[0]["data"]["conversation_id"] == "pop-test"
        finally:
            queue_mod._emit = original_emit

    def test_remove_emits_event(self):
        from core.queue import PriorityQueue
        from core.models.conversation import Conversation
        from core.models.prospect import ScoredProspect

        bus = EventBus()
        received = []
        bus.on("queue:removed", lambda e: received.append(e))

        import core.queue as queue_mod
        original_emit = queue_mod._emit

        def patched_emit(event_type, **kwargs):
            bus.emit(event_type, kwargs)

        queue_mod._emit = patched_emit
        try:
            q = PriorityQueue()
            conv = Conversation(
                source="email",
                participants=[],
                date=datetime.utcnow(),
                raw_text="test",
            )
            scored = ScoredProspect(
                conversation_id="rm-test",
                priority_score=0.5,
                conversation=conv,
                sla_deadline=datetime.utcnow(),
            )
            q.add(scored)
            q.remove("rm-test")

            assert len(received) == 1
            assert received[0]["type"] == "queue:removed"
        finally:
            queue_mod._emit = original_emit
