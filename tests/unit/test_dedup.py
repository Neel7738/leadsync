"""Tests for conversation deduplication."""

import json
import os
import time
import pytest
from unittest.mock import patch

from core.dedup import DeduplicationStore, _content_hash, _normalize_text, reset_dedup_store


@pytest.fixture(autouse=True)
def clean_dedup():
    """Reset global dedup store before each test."""
    reset_dedup_store()
    yield
    reset_dedup_store()


# ── Helper Functions ───────────────────────────────────────────


class TestNormalizeText:
    def test_basic_normalization(self):
        assert _normalize_text("  Hello   World  ") == "hello world"

    def test_empty_string(self):
        assert _normalize_text("") == ""
        assert _normalize_text(None) == ""

    def test_newlines_and_tabs(self):
        assert _normalize_text("line1\nline2\tline3") == "line1 line2 line3"

    def test_case_insensitive(self):
        assert _normalize_text("UPPER") == _normalize_text("upper")


class TestContentHash:
    def test_same_content_same_hash(self):
        h1 = _content_hash("alice@co.com", "Meeting Notes", "Let's discuss the proposal.")
        h2 = _content_hash("alice@co.com", "Meeting Notes", "Let's discuss the proposal.")
        assert h1 == h2

    def test_different_content_different_hash(self):
        h1 = _content_hash("alice@co.com", "Meeting Notes", "Body A")
        h2 = _content_hash("alice@co.com", "Meeting Notes", "Body B")
        assert h1 != h2

    def test_different_sender_different_hash(self):
        h1 = _content_hash("alice@co.com", "Subject", "Body")
        h2 = _content_hash("bob@co.com", "Subject", "Body")
        assert h1 != h2

    def test_case_insensitive(self):
        h1 = _content_hash("Alice@CO.com", "SUBJECT", "Body")
        h2 = _content_hash("alice@co.com", "subject", "body")
        assert h1 == h2

    def test_whitespace_insensitive(self):
        h1 = _content_hash("a@b.com", "  Subject  ", "Body  with   spaces")
        h2 = _content_hash("a@b.com", "Subject", "Body with spaces")
        assert h1 == h2


# ── DeduplicationStore ─────────────────────────────────────────


class TestDeduplicationStore:
    def test_first_time_not_duplicate(self):
        store = DeduplicationStore(path=None)
        assert store.is_duplicate(message_id="msg-001") is False
        assert store.is_duplicate(sender="a@b.com", subject="Hi", body="Hello") is False

    def test_mark_seen_and_check(self):
        store = DeduplicationStore(path=None)
        store.mark_seen(message_id="msg-001")
        assert store.is_duplicate(message_id="msg-001") is True

    def test_content_hash_dedup(self):
        store = DeduplicationStore(path=None)
        store.mark_seen(sender="a@b.com", subject="Hello", body="World")
        assert store.is_duplicate(sender="a@b.com", subject="Hello", body="World") is True
        assert store.is_duplicate(sender="a@b.com", subject="Hello", body="Different") is False

    def test_both_message_id_and_content_hash(self):
        store = DeduplicationStore(path=None)
        store.mark_seen(message_id="msg-123", sender="a@b.com", subject="Hi", body="Body")
        # Either match should flag as duplicate
        assert store.is_duplicate(message_id="msg-123") is True
        assert store.is_duplicate(sender="a@b.com", subject="Hi", body="Body") is True
        assert store.is_duplicate(message_id="other") is False

    def test_different_messages_independent(self):
        store = DeduplicationStore(path=None)
        store.mark_seen(message_id="msg-1")
        store.mark_seen(message_id="msg-2")
        assert store.is_duplicate(message_id="msg-1") is True
        assert store.is_duplicate(message_id="msg-2") is True
        assert store.is_duplicate(message_id="msg-3") is False

    def test_clear(self):
        store = DeduplicationStore(path=None)
        store.mark_seen(message_id="msg-1")
        store.mark_seen(sender="a@b.com", subject="S", body="B")
        removed = store.clear()
        assert removed >= 2
        assert store.is_duplicate(message_id="msg-1") is False

    def test_get_stats(self):
        store = DeduplicationStore(path=None)
        store.mark_seen(message_id="msg-1")
        stats = store.get_stats()
        assert stats["message_ids_tracked"] == 1
        assert stats["storage"] == "memory"

    def test_is_seen(self):
        store = DeduplicationStore(path=None)
        assert store.is_seen("msg-1") is False
        store.mark_seen(message_id="msg-1")
        assert store.is_seen("msg-1") is True


# ── File Persistence ───────────────────────────────────────────


class TestFilePersistence:
    def test_save_and_load(self, tmp_path):
        path = str(tmp_path / "dedup.json")
        store1 = DeduplicationStore(path=path)
        store1.mark_seen(message_id="msg-persistent")
        store1.mark_seen(sender="x@y.com", subject="S", body="B")

        # Create new store from same file
        store2 = DeduplicationStore(path=path)
        assert store2.is_duplicate(message_id="msg-persistent") is True
        assert store2.is_duplicate(sender="x@y.com", subject="S", body="B") is True

    def test_atomic_write(self, tmp_path):
        path = str(tmp_path / "dedup.json")
        store = DeduplicationStore(path=path)
        store.mark_seen(message_id="msg-1")
        assert os.path.exists(path)
        # No .tmp file should remain
        assert not os.path.exists(path + ".tmp")

    def test_missing_file_no_error(self, tmp_path):
        path = str(tmp_path / "nonexistent.json")
        store = DeduplicationStore(path=path)
        assert store.is_duplicate(message_id="msg-1") is False


# ── Expiry ─────────────────────────────────────────────────────


class TestExpiry:
    def test_expired_entries_not_duplicate(self):
        store = DeduplicationStore(path=None, max_age_days=0)
        store.mark_seen(message_id="msg-old")
        # max_age_days=0 means everything is expired immediately
        time.sleep(0.01)
        assert store.is_duplicate(message_id="msg-old") is False

    def test_cleanup_expired(self):
        store = DeduplicationStore(path=None, max_age_days=0)
        store.mark_seen(message_id="msg-1")
        store.mark_seen(message_id="msg-2")
        time.sleep(0.01)
        removed = store.cleanup_expired()
        assert removed >= 2
        assert store.is_duplicate(message_id="msg-1") is False

    def test_non_expired_entries_survive(self):
        store = DeduplicationStore(path=None, max_age_days=90)
        store.mark_seen(message_id="msg-recent")
        assert store.is_duplicate(message_id="msg-recent") is True


# ── Eviction ───────────────────────────────────────────────────


class TestEviction:
    def test_max_entries_evicts_oldest(self):
        store = DeduplicationStore(path=None, max_entries=5)
        for i in range(10):
            store.mark_seen(message_id=f"msg-{i:03d}")
        stats = store.get_stats()
        assert stats["message_ids_tracked"] <= 5


# ── Integration: Store-Level Dedup ───────────────────────────


class TestEmailIngestionDedup:
    """Store-level dedup tests (no email module import to avoid LLM hang)."""

    def test_dedup_prevents_double_processing(self):
        """Simulate what fetch_emails does: check → process → mark."""
        store = DeduplicationStore(path=None)

        # Simulate processing email msg-1
        msg_id = "msg-123@outlook.com"
        sender = "alice@co.com"
        subject = "Proposal Update"
        body = "Here is the updated proposal."

        # First pass — not duplicate
        assert store.is_duplicate(message_id=msg_id, sender=sender, subject=subject, body=body) is False
        store.mark_seen(message_id=msg_id, sender=sender, subject=subject, body=body)

        # Second pass — duplicate
        assert store.is_duplicate(message_id=msg_id, sender=sender, subject=subject, body=body) is True

    def test_content_hash_catches_forwarded_emails(self):
        """Same body forwarded with different Message-ID should be caught by content hash."""
        store = DeduplicationStore(path=None)

        body = "Please review the attached proposal."
        store.mark_seen(sender="a@b.com", subject="Proposal", body=body)

        # Same body, different message ID (forwarded)
        assert store.is_duplicate(message_id="new-msg-id", sender="a@b.com", subject="Proposal", body=body) is True

    def test_different_bodies_pass_through(self):
        """Different content should not be flagged."""
        store = DeduplicationStore(path=None)

        store.mark_seen(sender="a@b.com", subject="Re: Thread", body="First reply")
        assert store.is_duplicate(sender="a@b.com", subject="Re: Thread", body="Second reply") is False
