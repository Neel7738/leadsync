"""Tests for webhook retry logic with exponential backoff."""

import os
import time
from unittest.mock import MagicMock, patch

import pytest

from core.alerts.retry import WebhookRetry, _calculate_delay


class TestCalculateDelay:
    def test_first_attempt_base_delay(self):
        from core.alerts.retry import RetryConfig
        config = RetryConfig(base_delay=1.0, backoff_factor=2.0, max_delay=60.0, jitter=False)
        delay = _calculate_delay(0, config)
        assert delay == 1.0

    def test_second_attempt_doubles(self):
        from core.alerts.retry import RetryConfig
        config = RetryConfig(base_delay=1.0, backoff_factor=2.0, max_delay=60.0, jitter=False)
        delay = _calculate_delay(1, config)
        assert delay == 2.0

    def test_third_attempt(self):
        from core.alerts.retry import RetryConfig
        config = RetryConfig(base_delay=1.0, backoff_factor=2.0, max_delay=60.0, jitter=False)
        delay = _calculate_delay(2, config)
        assert delay == 4.0

    def test_capped_at_max_delay(self):
        from core.alerts.retry import RetryConfig
        config = RetryConfig(base_delay=1.0, backoff_factor=10.0, max_delay=5.0, jitter=False)
        delay = _calculate_delay(3, config)
        assert delay == 5.0

    def test_jitter_adds_variance(self):
        from core.alerts.retry import RetryConfig
        config = RetryConfig(base_delay=1.0, backoff_factor=2.0, max_delay=60.0, jitter=True)
        delays = [_calculate_delay(0, config) for _ in range(10)]
        # All should be >= 1.0 and <= 1.25 (base + 25% jitter)
        assert all(1.0 <= d <= 1.3 for d in delays)
        # At least some should differ due to jitter
        assert len(set(round(d, 2) for d in delays)) > 1


class TestWebhookRetry:
    def test_success_first_attempt(self):
        retry = WebhookRetry(max_attempts=3, backoff_base=0.01)
        result = retry.execute(lambda: True)
        assert result.success is True
        assert result.attempts == 1

    def test_success_after_retries(self):
        call_count = 0
        def flaky_send():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return False
            return True

        retry = WebhookRetry(max_attempts=3, backoff_base=0.01)
        result = retry.execute(flaky_send)
        assert result.success is True
        assert result.attempts == 3

    def test_failure_after_all_attempts(self):
        retry = WebhookRetry(max_attempts=2, backoff_base=0.01)
        result = retry.execute(lambda: False)
        assert result.success is False
        assert result.attempts == 2

    def test_exception_retry(self):
        call_count = 0
        def failing_then_succeeding():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("timeout")
            return True

        retry = WebhookRetry(max_attempts=3, backoff_base=0.01)
        result = retry.execute(failing_then_succeeding)
        assert result.success is True
        assert result.attempts == 2

    def test_exception_all_fail(self):
        retry = WebhookRetry(max_attempts=2, backoff_base=0.01)
        result = retry.execute(lambda: (_ for _ in ()).throw(ConnectionError("timeout")))
        assert result.success is False
        assert "timeout" in result.error

    def test_stats_tracking(self):
        retry = WebhookRetry(max_attempts=1, backoff_base=0.01)
        retry.execute(lambda: True)
        retry.execute(lambda: False)

        stats = retry.get_stats()
        assert stats["total_calls"] == 2
        assert stats["total_successes"] == 1
        assert stats["total_failures"] == 1
        assert stats["success_rate"] == 50.0

    def test_reset_stats(self):
        retry = WebhookRetry(max_attempts=1, backoff_base=0.01)
        retry.execute(lambda: True)
        retry.reset_stats()
        assert retry.get_stats()["total_calls"] == 0

    def test_result_to_dict(self):
        from core.alerts.retry import RetryResult
        result = RetryResult(success=True, attempts=2, total_delay=1.5)
        d = result.to_dict()
        assert d["success"] is True
        assert d["attempts"] == 2
        assert d["total_delay_seconds"] == 1.5


class TestAlertManagerRetry:
    def test_retry_on_channel_failure(self):
        from core.alerts import AlertManager

        call_count = 0
        def flaky_channel(alert):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise ConnectionError("timeout")
            return True

        am = AlertManager(cooldown_seconds=0, enable_retry=True)
        am._retry = WebhookRetry(max_attempts=3, backoff_base=0.01)
        am.add_channel(flaky_channel)

        alert = {"name": "Test", "body": "test", "subject": "test"}
        result = am.send_alert(alert, "retry-001")

        assert result["status"] == "sent"
        assert result["sent"] == 1
        assert call_count == 2  # Succeeded on second attempt

    def test_no_retry_when_disabled(self):
        from core.alerts import AlertManager

        am = AlertManager(cooldown_seconds=0, enable_retry=False)
        am.add_channel(lambda alert: False)

        alert = {"name": "Test", "body": "test", "subject": "test"}
        result = am.send_alert(alert, "no-retry-001")

        assert result["status"] == "failed"
        assert result["results"][0]["attempts"] == 1

    def test_retry_stats_included(self):
        from core.alerts import AlertManager

        am = AlertManager(cooldown_seconds=0, enable_retry=True)
        am.add_channel(lambda alert: True)

        alert = {"name": "Test", "body": "test", "subject": "test"}
        am.send_alert(alert, "stats-001")

        stats = am.get_stats()
        assert "retry_enabled" in stats
        assert stats["retry_enabled"] is True
        assert "total_retries" in stats

    def test_latency_tracking(self):
        from core.alerts import AlertManager

        am = AlertManager(cooldown_seconds=0, enable_retry=True)
        am._retry = WebhookRetry(max_attempts=1, backoff_base=0.01)
        am.add_channel(lambda alert: True)

        alert = {"name": "Test", "body": "test", "subject": "test"}
        result = am.send_alert(alert, "latency-001")

        assert "latency_ms" in result["results"][0]
        assert result["results"][0]["latency_ms"] >= 0


class TestRetryConfig:
    def test_custom_config(self):
        retry = WebhookRetry(
            max_attempts=5,
            backoff_base=0.5,
            backoff_factor=3.0,
            max_delay=10.0,
            jitter=False,
        )
        assert retry.config.max_attempts == 5
        assert retry.config.base_delay == 0.5
        assert retry.config.backoff_factor == 3.0

    def test_env_based_config(self):
        os.environ["WEBHOOK_MAX_ATTEMPTS"] = "5"
        os.environ["WEBHOOK_BACKOFF_BASE"] = "0.5"
        try:
            from core.alerts.retry import get_webhook_retry, reset_webhook_retry
            reset_webhook_retry()
            retry = get_webhook_retry()
            assert retry.config.max_attempts == 5
            assert retry.config.base_delay == 0.5
            reset_webhook_retry()
        finally:
            os.environ.pop("WEBHOOK_MAX_ATTEMPTS", None)
            os.environ.pop("WEBHOOK_BACKOFF_BASE", None)
