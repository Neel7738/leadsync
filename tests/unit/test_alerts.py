"""Tests for SLA breach checker and alerting system."""

import time
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from core.alerts import (
    AlertManager,
    SLABreachChecker,
    TelegramSender,
    EmailSender,
    SlackSender,
    DiscordSender,
    TeamsSender,
    PagerDutySender,
    OpsgenieSender,
    build_breach_alert,
)
from core.models.conversation import Conversation
from core.models.prospect import ScoredProspect


def _make_prospect(cid="c1", urgency="high", priority=0.8, breached=False, requeues=0):
    conv = Conversation(
        source="email",
        participants=[{"name": "John Doe", "email": "john@example.com"}],
        date=datetime.utcnow(),
        raw_text="Great meeting. Will send proposal.",
        commitments=["send proposal by Friday"],
        urgency=urgency,
        deal_size=50000.0,
    )
    sla_deadline = datetime.utcnow() - timedelta(hours=1) if breached else datetime.utcnow() + timedelta(hours=24)
    return ScoredProspect(
        conversation_id=cid,
        priority_score=priority,
        conversation=conv,
        sla_deadline=sla_deadline,
        sla_breached=breached,
        times_requeued=requeues,
    )


class TestBuildBreachAlert:
    def test_basic_alert(self):
        prospect = _make_prospect(breached=True, requeues=2)
        alert = build_breach_alert(prospect)
        assert "John Doe" in alert["subject"]
        assert "0.80" in alert["subject"]
        assert alert["name"] == "John Doe"
        assert alert["email"] == "john@example.com"
        assert alert["priority"] == 0.8
        assert alert["requeues"] == 2

    def test_escalation_message(self):
        prospect = _make_prospect(breached=True, requeues=5)
        alert = build_breach_alert(prospect)
        assert "ESCALATION" in alert["body"]
        assert "5 times" in alert["body"]

    def test_no_escalation_low_requeues(self):
        prospect = _make_prospect(breached=True, requeues=1)
        alert = build_breach_alert(prospect)
        assert "ESCALATION" not in alert["body"]

    def test_deal_value_included(self):
        prospect = _make_prospect(breached=True)
        alert = build_breach_alert(prospect)
        assert "$50,000" in alert["body"]

    def test_commitments_included(self):
        prospect = _make_prospect(breached=True)
        alert = build_breach_alert(prospect)
        assert "send proposal" in alert["body"]


class TestAlertManager:
    def test_send_alert(self):
        am = AlertManager(cooldown_seconds=0)
        sent = []
        am.add_channel(lambda a: sent.append(a) or True)

        alert = {"name": "Test", "summary": "test alert"}
        result = am.send_alert(alert, "c1")

        assert result["status"] == "sent"
        assert result["sent"] == 1
        assert len(sent) == 1

    def test_cooldown_prevents_resend(self):
        am = AlertManager(cooldown_seconds=60)
        am.add_channel(lambda a: True)

        alert = {"name": "Test"}
        am.send_alert(alert, "c1")
        result = am.send_alert(alert, "c1")

        assert result["status"] == "skipped"
        assert "Cooldown" in result["reason"]

    def test_multiple_channels(self):
        am = AlertManager(cooldown_seconds=0)
        sent1, sent2 = [], []
        def ch1(a): sent1.append(a); return True
        def ch2(a): sent2.append(a); return True
        am.add_channel(ch1)
        am.add_channel(ch2)

        result = am.send_alert({"name": "Test"}, "c1")
        assert result["sent"] == 2
        assert len(sent1) == 1
        assert len(sent2) == 1

    def test_channel_failure_counted(self):
        am = AlertManager(cooldown_seconds=0)
        am.add_channel(lambda a: False)  # Always fails

        result = am.send_alert({"name": "Test"}, "c1")
        assert result["failed"] == 1
        assert result["sent"] == 0

    def test_channel_exception_counted(self):
        am = AlertManager(cooldown_seconds=0)
        def bad_channel(a):
            raise ValueError("boom")
        am.add_channel(bad_channel)

        result = am.send_alert({"name": "Test"}, "c1")
        assert result["failed"] == 1

    def test_stats(self):
        am = AlertManager(cooldown_seconds=0)
        am.add_channel(lambda a: True)
        am.send_alert({"name": "T"}, "c1")

        stats = am.get_stats()
        assert stats["channels"] == 1
        assert stats["total_sent"] == 1


class TestTelegramSender:
    @patch("httpx.post")
    def test_send_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        sender = TelegramSender("token123", "chat456")
        result = sender.send("Hello world")

        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "token123" in call_args[0][0]
        assert call_args[1]["json"]["chat_id"] == "chat456"

    @patch("httpx.post")
    def test_send_failure(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        mock_post.return_value = mock_resp

        sender = TelegramSender("token", "chat")
        result = sender.send("test")
        assert result is False

    @patch("httpx.post")
    def test_send_exception(self, mock_post):
        mock_post.side_effect = ConnectionError("timeout")

        sender = TelegramSender("token", "chat")
        result = sender.send("test")
        assert result is False


class TestEmailSender:
    @patch("core.alerts.smtplib.SMTP")
    def test_send_success(self, MockSMTP):
        mock_server = MagicMock()
        MockSMTP.return_value.__enter__ = MagicMock(return_value=mock_server)
        MockSMTP.return_value.__exit__ = MagicMock(return_value=False)

        sender = EmailSender(
            to_address="admin@co.com",
            smtp_host="smtp.test.com",
            smtp_port=587,
            smtp_username="user",
            smtp_password="pass",
        )
        result = sender.send("Test Subject", "Test body")
        assert result is True
        mock_server.sendmail.assert_called_once()

    def test_send_no_credentials(self):
        sender = EmailSender(to_address="admin@co.com")
        result = sender.send("Subject", "body")
        assert result is False

    @patch("core.alerts.smtplib.SMTP")
    def test_send_exception(self, MockSMTP):
        MockSMTP.side_effect = ConnectionError("refused")
        sender = EmailSender(
            to_address="admin@co.com",
            smtp_username="user",
            smtp_password="pass",
        )
        result = sender.send("Subject", "body")
        assert result is False


class TestSLABreachChecker:
    def test_check_now_finds_breaches(self):
        from core.queue import PriorityQueue

        test_queue = PriorityQueue()
        prospect = _make_prospect("breach-1", breached=True)
        test_queue.add(prospect)

        # Patch at the import location inside check_now
        with patch("core.queue.get_queue", return_value=test_queue):
            with patch("core.realtime.emit_queue_event"):
                checker = SLABreachChecker(check_interval=999)
                result = checker.check_now()

        assert result["breaches_found"] >= 1
        assert checker._checks_done == 1

    def test_status(self):
        checker = SLABreachChecker(check_interval=30)
        status = checker.get_status()
        assert status["check_interval"] == 30
        assert status["running"] is False
        assert status["checks_done"] == 0

    def test_start_and_stop(self):
        checker = SLABreachChecker(check_interval=999)
        checker.start()
        assert checker._running is True

        time.sleep(0.1)
        checker.stop()
        assert checker._running is False

    def test_check_now_no_breaches(self):
        from core.queue import PriorityQueue
        test_queue = PriorityQueue()

        with patch("core.queue.get_queue", return_value=test_queue):
            with patch("core.realtime.emit_queue_event"):
                checker = SLABreachChecker()
                result = checker.check_now()

        assert result["breaches_found"] == 0
        assert result["alerts_sent"] == 0


class TestSlackSender:
    @patch("httpx.post")
    def test_send_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"
        mock_post.return_value = mock_resp

        sender = SlackSender("https://hooks.slack.com/services/T/B/xoxo")
        result = sender.send({"text": "Hello"})

        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "hooks.slack.com" in call_args[0][0]

    @patch("httpx.post")
    def test_send_with_channel(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"
        mock_post.return_value = mock_resp

        sender = SlackSender("https://hooks.slack.com/services/T/B/xoxo", channel="#alerts")
        sender.send({"text": "Hello"})

        call_kwargs = mock_post.call_args[1]["json"]
        assert call_kwargs["channel"] == "#alerts"

    @patch("httpx.post")
    def test_send_failure(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "no_channel"
        mock_post.return_value = mock_resp

        sender = SlackSender("https://hooks.slack.com/bad")
        result = sender.send({"text": "test"})
        assert result is False

    @patch("httpx.post")
    def test_send_exception(self, mock_post):
        mock_post.side_effect = ConnectionError("timeout")
        sender = SlackSender("https://hooks.slack.com/services/T/B/xoxo")
        result = sender.send({"text": "test"})
        assert result is False

    @patch("httpx.post")
    def test_send_breach_alert(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "ok"
        mock_post.return_value = mock_resp

        sender = SlackSender("https://hooks.slack.com/services/T/B/xoxo")
        alert = {
            "name": "John Doe",
            "priority": 0.85,
            "urgency": "high",
            "requeues": 4,
            "email": "john@example.com",
            "summary": "test",
        }
        result = sender.send_breach_alert(alert)
        assert result is True

        payload = mock_post.call_args[1]["json"]
        assert "attachments" in payload
        assert payload["attachments"][0]["color"] == "#FF0000"
        # Escalation field present because requeues >= 3
        fields = payload["attachments"][0]["fields"]
        assert any("ESCALATION" in f["title"] for f in fields)


class TestDiscordSender:
    @patch("httpx.post")
    def test_send_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_post.return_value = mock_resp

        sender = DiscordSender("https://discord.com/api/webhooks/T/xoxo")
        result = sender.send({"title": "Test"})
        assert result is True

    @patch("httpx.post")
    def test_send_failure(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "Not Found"
        mock_post.return_value = mock_resp

        sender = DiscordSender("https://discord.com/api/webhooks/bad")
        result = sender.send({"title": "test"})
        assert result is False

    @patch("httpx.post")
    def test_send_exception(self, mock_post):
        mock_post.side_effect = ConnectionError("timeout")
        sender = DiscordSender("https://discord.com/api/webhooks/T/xoxo")
        result = sender.send({"title": "test"})
        assert result is False

    @patch("httpx.post")
    def test_send_breach_alert(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_post.return_value = mock_resp

        sender = DiscordSender("https://discord.com/api/webhooks/T/xoxo")
        alert = {
            "name": "Jane Smith",
            "priority": 0.92,
            "urgency": "high",
            "requeues": 2,
            "email": "jane@example.com",
            "summary": "test",
        }
        result = sender.send_breach_alert(alert)
        assert result is True

        payload = mock_post.call_args[1]["json"]
        assert "embeds" in payload
        embed = payload["embeds"][0]
        assert "SLA BREACH" in embed["title"]
        assert embed["color"] == 0xFF0000
        assert len(embed["fields"]) >= 3


class TestTeamsSender:
    @patch("httpx.post")
    def test_send_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        sender = TeamsSender("https://outlook.office.com/webhook/xoxo")
        result = sender.send({"@type": "MessageCard"})
        assert result is True

    @patch("httpx.post")
    def test_send_failure(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        mock_post.return_value = mock_resp

        sender = TeamsSender("https://outlook.office.com/webhook/bad")
        result = sender.send({"@type": "MessageCard"})
        assert result is False

    @patch("httpx.post")
    def test_send_exception(self, mock_post):
        mock_post.side_effect = ConnectionError("timeout")
        sender = TeamsSender("https://outlook.office.com/webhook/xoxo")
        result = sender.send({"@type": "MessageCard"})
        assert result is False

    @patch("httpx.post")
    def test_send_breach_alert(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        sender = TeamsSender("https://outlook.office.com/webhook/xoxo")
        alert = {
            "name": "Bob Wilson",
            "priority": 0.75,
            "urgency": "medium",
            "requeues": 3,
            "email": "bob@example.com",
            "summary": "test",
        }
        result = sender.send_breach_alert(alert)
        assert result is True

        payload = mock_post.call_args[1]["json"]
        assert payload["@type"] == "MessageCard"
        assert payload["themeColor"] == "FFA500"  # medium
        assert len(payload["sections"]) >= 1
        # Escalation section present because requeues >= 3
        assert len(payload["sections"]) == 2
        assert "ESCALATION" in payload["sections"][1]["activityTitle"]


class TestPagerDutySender:
    @patch("httpx.post")
    def test_send_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_post.return_value = mock_resp

        sender = PagerDutySender("integration-key-123")
        result = sender.send({"routing_key": "test", "event_action": "trigger"})
        assert result is True
        mock_post.assert_called_once()

    @patch("httpx.post")
    def test_send_failure(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        mock_post.return_value = mock_resp

        sender = PagerDutySender("key")
        result = sender.send({"test": True})
        assert result is False

    @patch("httpx.post")
    def test_send_exception(self, mock_post):
        mock_post.side_effect = ConnectionError("timeout")
        sender = PagerDutySender("key")
        result = sender.send({"test": True})
        assert result is False

    @patch("httpx.post")
    def test_send_breach_alert(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_post.return_value = mock_resp

        sender = PagerDutySender("integration-key-123", from_email="admin@co.com")
        alert = {
            "name": "John Doe",
            "priority": 0.92,
            "urgency": "high",
            "requeues": 4,
            "email": "john@example.com",
            "summary": "test",
            "body": "SLA breach for John Doe",
            "subject": "Test",
        }
        result = sender.send_breach_alert(alert)
        assert result is True

        payload = mock_post.call_args[1]["json"]
        assert payload["event_action"] == "trigger"
        assert payload["routing_key"] == "integration-key-123"
        assert payload["payload"]["severity"] == "critical"  # high urgency
        assert payload["payload"]["custom_details"]["prospect_name"] == "John Doe"
        # Escalation because requeues >= 3
        assert "sfa-sla-john@example.com" in payload["dedup_key"]

    @patch("httpx.post")
    def test_send_medium_urgency(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_post.return_value = mock_resp

        sender = PagerDutySender("key")
        alert = {
            "name": "Jane", "priority": 0.6, "urgency": "medium",
            "requeues": 1, "email": "jane@co.com",
            "summary": "", "body": "", "subject": "",
        }
        result = sender.send_breach_alert(alert)
        assert result is True
        payload = mock_post.call_args[1]["json"]
        assert payload["payload"]["severity"] == "warning"  # medium

    @patch("httpx.post")
    def test_resolve(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_post.return_value = mock_resp

        sender = PagerDutySender("key")
        result = sender.resolve("sfa-sla-dedup-123")
        assert result is True
        payload = mock_post.call_args[1]["json"]
        assert payload["event_action"] == "resolve"
        assert payload["dedup_key"] == "sfa-sla-dedup-123"


class TestOpsgenieSender:
    @patch("httpx.post")
    def test_send_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_post.return_value = mock_resp

        sender = OpsgenieSender("api-key-123")
        result = sender.send({"message": "test"})
        assert result is True
        # Verify auth header
        headers = mock_post.call_args[1]["headers"]
        assert headers["Authorization"] == "GenieKey api-key-123"

    @patch("httpx.post")
    def test_send_failure(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 422
        mock_resp.text = "Unprocessable"
        mock_post.return_value = mock_resp

        sender = OpsgenieSender("key")
        result = sender.send({"message": "test"})
        assert result is False

    @patch("httpx.post")
    def test_send_exception(self, mock_post):
        mock_post.side_effect = ConnectionError("timeout")
        sender = OpsgenieSender("key")
        result = sender.send({"message": "test"})
        assert result is False

    @patch("httpx.post")
    def test_send_breach_alert(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_post.return_value = mock_resp

        sender = OpsgenieSender("api-key-123", team="sales-oncall")
        alert = {
            "name": "Bob Wilson",
            "priority": 0.85,
            "urgency": "high",
            "requeues": 5,
            "email": "bob@example.com",
            "summary": "test",
            "body": "SLA breach",
            "subject": "Test",
        }
        result = sender.send_breach_alert(alert)
        assert result is True

        payload = mock_post.call_args[1]["json"]
        assert payload["priority"] == "P1"  # high urgency
        assert "Bob Wilson" in payload["message"]
        assert "sla-breach" in payload["tags"]
        assert payload["responders"][0]["name"] == "sales-oncall"
        assert payload["responders"][0]["type"] == "team"

    @patch("httpx.post")
    def test_send_low_urgency(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 202
        mock_post.return_value = mock_resp

        sender = OpsgenieSender("key")
        alert = {
            "name": "Carol", "priority": 0.3, "urgency": "low",
            "requeues": 0, "email": "carol@co.com",
            "summary": "", "body": "", "subject": "",
        }
        result = sender.send_breach_alert(alert)
        assert result is True
        payload = mock_post.call_args[1]["json"]
        assert payload["priority"] == "P3"  # low

    @patch("httpx.post")
    def test_close_alert(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        sender = OpsgenieSender("key")
        result = sender.close_alert("sfa-sla-dedup-456")
        assert result is True
        assert "/close" in mock_post.call_args[0][0]

    def test_no_team_omits_responders(self):
        sender = OpsgenieSender("key")
        # No team set — responders should not be in payload
        # (we check this by constructing the payload manually)
        payload = {
            "message": "test",
            "tags": ["sla-breach"],
        }
        if sender.team:
            payload["responders"] = [{"name": sender.team, "type": "team"}]
        assert "responders" not in payload
