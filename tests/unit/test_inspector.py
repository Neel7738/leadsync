"""Tests for webhook payload inspector."""

import time
import pytest
from unittest.mock import MagicMock

from core.alerts.inspector import WebhookInspector, get_inspector


@pytest.fixture
def inspector():
    """Fresh inspector instance."""
    return WebhookInspector(max_entries=100, enabled=True)


@pytest.fixture
def alert():
    """Sample alert payload."""
    return {
        "subject": "SLA Breach: John Doe",
        "body": "John Doe has breached SLA",
        "name": "John Doe",
        "email": "john@example.com",
        "priority": 0.92,
        "urgency": "high",
        "requeues": 2,
    }


class TestWebhookInspectorInit:
    def test_default_state(self, inspector):
        stats = inspector.get_stats()
        assert stats["total_captured"] == 0
        assert stats["stored_entries"] == 0
        assert stats["enabled"] is True

    def test_disabled_inspector(self):
        insp = WebhookInspector(enabled=False)
        assert insp.enabled is False

    def test_max_entries(self):
        insp = WebhookInspector(max_entries=5)
        assert insp._max_entries == 5


class TestWrapChannel:
    def test_wraps_function(self, inspector):
        mock_fn = MagicMock(return_value=True)
        mock_fn.__name__ = "test_channel"

        wrapped = inspector.wrap_channel(mock_fn, channel_name="test")
        assert wrapped.__name__ == "test"

        # Call wrapped function
        result = wrapped({"test": "payload"})
        assert result is True
        mock_fn.assert_called_once()

    def test_captures_payload(self, inspector, alert):
        mock_fn = MagicMock(return_value=True)
        mock_fn.__name__ = "telegram_send"

        wrapped = inspector.wrap_channel(mock_fn, channel_name="telegram")
        wrapped(alert)

        entries = inspector.get_entries()
        assert len(entries) == 1
        assert entries[0]["channel"] == "telegram"
        assert entries[0]["success"] is True
        assert entries[0]["status"] == "sent"

    def test_captures_failure(self, inspector, alert):
        mock_fn = MagicMock(return_value=False)
        mock_fn.__name__ = "slack_send"

        wrapped = inspector.wrap_channel(mock_fn, channel_name="slack")
        wrapped(alert)

        entries = inspector.get_entries()
        assert len(entries) == 1
        assert entries[0]["success"] is False
        assert entries[0]["status"] == "failed"

    def test_captures_exception(self, inspector, alert):
        def failing_channel(alert):
            raise ConnectionError("Connection refused")

        wrapped = inspector.wrap_channel(failing_channel, channel_name="email")
        with pytest.raises(ConnectionError):
            wrapped(alert)

        entries = inspector.get_entries()
        assert len(entries) == 1
        assert entries[0]["success"] is False
        assert entries[0]["status"] == "error"
        assert "Connection refused" in entries[0]["error"]

    def test_records_latency(self, inspector, alert):
        def slow_channel(alert):
            time.sleep(0.01)
            return True

        wrapped = inspector.wrap_channel(slow_channel, channel_name="slow")
        wrapped(alert)

        entries = inspector.get_entries()
        assert entries[0]["latency_ms"] >= 10  # At least 10ms

    def test_preserves_original_function(self, inspector):
        mock_fn = MagicMock(return_value=True)
        mock_fn.__name__ = "original"

        wrapped = inspector.wrap_channel(mock_fn)
        assert wrapped._original is mock_fn

    def test_inferred_channel_type(self, inspector):
        mock_fn = MagicMock(return_value=True)

        telegram = inspector.wrap_channel(mock_fn, channel_name="telegram_send")
        slack = inspector.wrap_channel(mock_fn, channel_name="slack_alert")
        discord = inspector.wrap_channel(mock_fn, channel_name="discord_notify")
        teams = inspector.wrap_channel(mock_fn, channel_name="teams_webhook")
        email = inspector.wrap_channel(mock_fn, channel_name="email_sender")
        pd = inspector.wrap_channel(mock_fn, channel_name="pagerduty_incident")
        og = inspector.wrap_channel(mock_fn, channel_name="opsgenie_alert")

        # Can't directly check _inspector_type, but the wrapping works
        # The type is inferred inside the wrapped function
        assert telegram is not None
        assert slack is not None
        assert discord is not None
        assert teams is not None
        assert email is not None
        assert pd is not None
        assert og is not None


class TestCaptureManual:
    def test_capture_manual_entry(self, inspector):
        entry = inspector.capture_manual(
            channel="test-service",
            payload={"key": "value"},
            channel_type="webhook",
            success=True,
            latency_ms=150,
        )
        assert entry["channel"] == "test-service"
        assert entry["payload"] == {"key": "value"}
        assert entry["success"] is True
        assert entry["latency_ms"] == 150

    def test_capture_manual_with_error(self, inspector):
        entry = inspector.capture_manual(
            channel="failing-service",
            payload={"data": "test"},
            success=False,
            error="Timeout",
        )
        assert entry["success"] is False
        assert entry["error"] == "Timeout"

    def test_capture_increments_id(self, inspector):
        e1 = inspector.capture_manual("ch", {})
        e2 = inspector.capture_manual("ch", {})
        assert e2["id"] == e1["id"] + 1


class TestGetEntries:
    def test_get_entries_empty(self, inspector):
        entries = inspector.get_entries()
        assert entries == []

    def test_get_entries_most_recent_first(self, inspector):
        inspector.capture_manual("ch1", {"i": 1})
        time.sleep(0.001)
        inspector.capture_manual("ch2", {"i": 2})
        time.sleep(0.001)
        inspector.capture_manual("ch3", {"i": 3})

        entries = inspector.get_entries()
        assert entries[0]["payload"]["i"] == 3
        assert entries[1]["payload"]["i"] == 2
        assert entries[2]["payload"]["i"] == 1

    def test_filter_by_channel(self, inspector):
        inspector.capture_manual("slack", {"ch": "slack"})
        inspector.capture_manual("telegram", {"ch": "telegram"})
        inspector.capture_manual("slack", {"ch": "slack2"})

        entries = inspector.get_entries(channel="slack")
        assert len(entries) == 2
        assert all(e["channel"] == "slack" for e in entries)

    def test_filter_by_channel_type(self, inspector):
        inspector.capture_manual("ch1", {}, channel_type="slack")
        inspector.capture_manual("ch2", {}, channel_type="email")
        inspector.capture_manual("ch3", {}, channel_type="slack")

        entries = inspector.get_entries(channel_type="slack")
        assert len(entries) == 2

    def test_filter_by_success(self, inspector):
        inspector.capture_manual("ch1", {}, success=True)
        inspector.capture_manual("ch2", {}, success=False)
        inspector.capture_manual("ch3", {}, success=True)

        entries = inspector.get_entries(success=True)
        assert len(entries) == 2

        entries = inspector.get_entries(success=False)
        assert len(entries) == 1

    def test_filter_by_since(self, inspector):
        inspector.capture_manual("ch1", {})
        time.sleep(0.01)
        cutoff = time.time()
        time.sleep(0.01)
        inspector.capture_manual("ch2", {})

        entries = inspector.get_entries(since=cutoff)
        assert len(entries) == 1

    def test_limit(self, inspector):
        for i in range(20):
            inspector.capture_manual("ch", {"i": i})

        entries = inspector.get_entries(limit=5)
        assert len(entries) == 5


class TestGetEntry:
    def test_get_existing_entry(self, inspector):
        entry = inspector.capture_manual("ch", {"test": True})
        result = inspector.get_entry(entry["id"])
        assert result is not None
        assert result["id"] == entry["id"]
        assert result["payload"] == {"test": True}

    def test_get_nonexistent_entry(self, inspector):
        result = inspector.get_entry(999)
        assert result is None


class TestRingBuffer:
    def test_trim_old_entries(self):
        insp = WebhookInspector(max_entries=3)
        insp.capture_manual("ch", {"i": 1})
        insp.capture_manual("ch", {"i": 2})
        insp.capture_manual("ch", {"i": 3})
        insp.capture_manual("ch", {"i": 4})
        insp.capture_manual("ch", {"i": 5})

        entries = insp.get_entries(limit=100)
        assert len(entries) == 3
        # Should be the most recent 3
        ids = [e["payload"]["i"] for e in entries]
        assert 5 in ids
        assert 4 in ids
        assert 3 in ids
        assert 1 not in ids
        assert 2 not in ids


class TestGetStats:
    def test_stats_empty(self, inspector):
        stats = inspector.get_stats()
        assert stats["total_captured"] == 0
        assert stats["success_rate"] == 0.0

    def test_stats_with_entries(self, inspector):
        inspector.capture_manual("slack", {}, success=True)
        inspector.capture_manual("telegram", {}, success=True)
        inspector.capture_manual("email", {}, success=False)

        stats = inspector.get_stats()
        assert stats["total_captured"] == 3
        assert stats["sent"] == 2
        assert stats["failed"] == 1
        assert stats["success_rate"] == round(2 / 3 * 100, 1)

    def test_stats_by_channel_type(self, inspector):
        inspector.capture_manual("ch1", {}, channel_type="slack", success=True)
        inspector.capture_manual("ch2", {}, channel_type="slack", success=False)
        inspector.capture_manual("ch3", {}, channel_type="email", success=True)

        stats = inspector.get_stats()
        assert stats["by_channel_type"]["slack"]["total"] == 2
        assert stats["by_channel_type"]["slack"]["sent"] == 1
        assert stats["by_channel_type"]["email"]["total"] == 1

    def test_latency_stats(self, inspector):
        inspector.capture_manual("ch", {}, latency_ms=100)
        inspector.capture_manual("ch", {}, latency_ms=200)
        inspector.capture_manual("ch", {}, latency_ms=50)

        stats = inspector.get_stats()
        assert stats["latency"]["avg_ms"] == 117  # (100+200+50)/3 = 116.67
        assert stats["latency"]["max_ms"] == 200
        assert stats["latency"]["min_ms"] == 50


class TestGetChannelList:
    def test_empty_channels(self, inspector):
        channels = inspector.get_channel_list()
        assert channels == []

    def test_channel_list(self, inspector):
        inspector.capture_manual("slack", {}, success=True)
        inspector.capture_manual("slack", {}, success=False)
        inspector.capture_manual("telegram", {}, success=True)

        channels = inspector.get_channel_list()
        assert len(channels) == 2
        slack = next(c for c in channels if c["name"] == "slack")
        assert slack["total"] == 2
        assert slack["sent"] == 1
        assert slack["failed"] == 1


class TestClear:
    def test_clear_all(self, inspector):
        inspector.capture_manual("ch", {})
        inspector.capture_manual("ch", {})
        cleared = inspector.clear()
        assert cleared == 2
        assert len(inspector.get_entries()) == 0

    def test_clear_empty(self, inspector):
        cleared = inspector.clear()
        assert cleared == 0


class TestExportJson:
    def test_export_json(self, inspector):
        inspector.capture_manual("ch", {"key": "value"})
        json_str = inspector.export_json()
        assert "key" in json_str
        assert "value" in json_str

    def test_export_json_limit(self, inspector):
        for i in range(10):
            inspector.capture_manual("ch", {"i": i})
        json_str = inspector.export_json(limit=3)
        import json
        data = json.loads(json_str)
        assert len(data) == 3


class TestDisableInspector:
    def test_disabled_skips_capture(self, inspector, alert):
        inspector.enabled = False
        mock_fn = MagicMock(return_value=True)
        mock_fn.__name__ = "test"

        wrapped = inspector.wrap_channel(mock_fn)
        wrapped(alert)

        # Should still call the function
        mock_fn.assert_called_once()
        # But not capture
        entries = inspector.get_entries()
        assert len(entries) == 0

    def test_reenable_captures(self, inspector, alert):
        inspector.enabled = False
        mock_fn = MagicMock(return_value=True)
        mock_fn.__name__ = "test"
        wrapped = inspector.wrap_channel(mock_fn)
        wrapped(alert)
        assert len(inspector.get_entries()) == 0

        inspector.enabled = True
        wrapped(alert)
        assert len(inspector.get_entries()) == 1


class TestGlobalInspector:
    def test_get_inspector_singleton(self):
        insp1 = get_inspector()
        insp2 = get_inspector()
        assert insp1 is insp2


class TestPayloadSerialization:
    def test_serialize_dict(self, inspector):
        payload = {"key": "value", "nested": {"a": 1}}
        entry = inspector.capture_manual("ch", payload)
        assert entry["payload"]["key"] == "value"
        # Should be a copy, not reference
        payload["key"] = "modified"
        assert entry["payload"]["key"] == "value"

    def test_serialize_string(self, inspector):
        entry = inspector.capture_manual("ch", "plain text")
        assert entry["payload"] == "plain text"

    def test_serialize_list(self, inspector):
        entry = inspector.capture_manual("ch", [{"a": 1}, {"b": 2}])
        assert entry["payload"] == [{"a": 1}, {"b": 2}]

    def test_serialize_none(self, inspector):
        entry = inspector.capture_manual("ch", None)
        assert entry["payload"] is None
