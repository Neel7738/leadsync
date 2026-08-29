"""
Conversation deduplication — prevents processing the same email or conversation twice.

Two-layer deduplication strategy:
  1. Message-ID tracking: Uses the email Message-ID header (unique per email)
  2. Content hash: SHA-256 of normalized (sender, subject, body) tuple

Storage options:
  - File-based (JSON): Persistent across restarts
  - In-memory: For tests or ephemeral deployments

Usage:
    store = DeduplicationStore(path=".dedup_cache.json")
    if store.is_duplicate(message_id="abc@outlook.com", sender="a@b.com", subject="Hi", body="..."):
        print("Skip — already processed")
    else:
        store.mark_seen(message_id="abc@outlook.com", sender="a@b.com", subject="Hi", body="...")
        # ... process conversation ...
"""

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Set

logger = logging.getLogger("Dedup")


def _normalize_text(text: str) -> str:
    """Normalize text for hashing: lowercase, collapse whitespace, strip."""
    if not text:
        return ""
    import re
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    return text


def _content_hash(sender: str, subject: str, body: str) -> str:
    """
    Generate a content-based deduplication hash.

    Uses SHA-256 of normalized (sender + subject + body) to detect
    duplicate emails even when Message-ID is missing or changed.
    """
    normalized = f"{_normalize_text(sender)}|{_normalize_text(subject)}|{_normalize_text(body)}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class DeduplicationStore:
    """
    Tracks seen conversations by Message-ID and content hash.

    Supports both file-based persistence and in-memory-only mode.
    Thread-safe for concurrent access.

    Args:
        path: File path for JSON persistence. None = in-memory only.
        max_age_days: Remove entries older than this (default: 90 days).
        max_entries: Maximum entries to keep (LRU eviction beyond this).
    """

    def __init__(
        self,
        path: Optional[str] = None,
        max_age_days: int = 90,
        max_entries: int = 50_000,
    ):
        self._path = path
        self._max_age_days = max_age_days
        self._max_entries = max_entries
        self._lock = threading.Lock()

        # Two indexes:
        self._message_ids: Dict[str, float] = {}   # message_id → first_seen timestamp
        self._content_hashes: Dict[str, float] = {}  # content_hash → first_seen timestamp

        # Load from disk if available
        if path:
            self._load()

    # ── Public API ───────────────────────────────────────────

    def is_duplicate(
        self,
        message_id: Optional[str] = None,
        sender: str = "",
        subject: str = "",
        body: str = "",
    ) -> bool:
        """
        Check if a conversation has already been processed.

        Checks both Message-ID (if provided) and content hash.
        Returns True if either matches a previously seen entry.
        """
        now = time.time()

        with self._lock:
            # Check Message-ID
            if message_id and message_id in self._message_ids:
                first_seen = self._message_ids[message_id]
                if (now - first_seen) < (self._max_age_days * 86400):
                    return True
                # Entry expired — remove and continue
                del self._message_ids[message_id]

            # Check content hash
            if sender or subject or body:
                c_hash = _content_hash(sender, subject, body)
                if c_hash in self._content_hashes:
                    first_seen = self._content_hashes[c_hash]
                    if (now - first_seen) < (self._max_age_days * 86400):
                        return True
                    # Entry expired
                    del self._content_hashes[c_hash]

        return False

    def mark_seen(
        self,
        message_id: Optional[str] = None,
        sender: str = "",
        subject: str = "",
        body: str = "",
    ) -> None:
        """
        Record a conversation as processed.

        Stores both Message-ID (if provided) and content hash.
        """
        now = time.time()

        with self._lock:
            if message_id:
                self._message_ids[message_id] = now

            if sender or subject or body:
                c_hash = _content_hash(sender, subject, body)
                self._content_hashes[c_hash] = now

            # Eviction check
            self._maybe_evict()

        # Persist to disk
        if self._path:
            self._save()

    def is_seen(self, message_id: str) -> bool:
        """Check if a specific Message-ID has been seen."""
        with self._lock:
            if message_id in self._message_ids:
                first_seen = self._message_ids[message_id]
                if (now := time.time()) - first_seen < (self._max_age_days * 86400):
                    return True
                del self._message_ids[message_id]
        return False

    def get_stats(self) -> Dict[str, Any]:
        """Get deduplication statistics."""
        with self._lock:
            return {
                "message_ids_tracked": len(self._message_ids),
                "content_hashes_tracked": len(self._content_hashes),
                "total_tracked": len(self._message_ids) + len(self._content_hashes),
                "max_age_days": self._max_age_days,
                "max_entries": self._max_entries,
                "storage": "file" if self._path else "memory",
                "path": self._path,
            }

    def clear(self) -> int:
        """Clear all entries. Returns number of entries removed."""
        with self._lock:
            count = len(self._message_ids) + len(self._content_hashes)
            self._message_ids.clear()
            self._content_hashes.clear()
        if self._path:
            self._save()
        return count

    def cleanup_expired(self) -> int:
        """Remove expired entries. Returns number removed."""
        now = time.time()
        cutoff = now - (self._max_age_days * 86400)
        removed = 0

        with self._lock:
            expired_ids = [k for k, v in self._message_ids.items() if v < cutoff]
            for k in expired_ids:
                del self._message_ids[k]
                removed += 1

            expired_hashes = [k for k, v in self._content_hashes.items() if v < cutoff]
            for k in expired_hashes:
                del self._content_hashes[k]
                removed += 1

        if removed and self._path:
            self._save()

        return removed

    # ── Internal ─────────────────────────────────────────────

    def _maybe_evict(self) -> None:
        """Evict oldest entries if over max_entries."""
        total = len(self._message_ids) + len(self._content_hashes)
        if total <= self._max_entries:
            return

        # Remove oldest 10% of entries
        to_remove = max(1, int(total * 0.1))

        # Evict from message_ids
        if self._message_ids:
            sorted_ids = sorted(self._message_ids.items(), key=lambda x: x[1])
            for k, _ in sorted_ids[:to_remove]:
                del self._message_ids[k]
                to_remove -= 1
                if to_remove <= 0:
                    break

        # Evict from content hashes if still needed
        if to_remove > 0 and self._content_hashes:
            sorted_hashes = sorted(self._content_hashes.items(), key=lambda x: x[1])
            for k, _ in sorted_hashes[:to_remove]:
                del self._content_hashes[k]
                to_remove -= 1
                if to_remove <= 0:
                    break

    def _load(self) -> None:
        """Load dedup state from JSON file."""
        if not self._path or not os.path.exists(self._path):
            return

        try:
            with open(self._path, "r") as f:
                data = json.load(f)
            self._message_ids = {k: v for k, v in data.get("message_ids", {}).items()}
            self._content_hashes = {k: v for k, v in data.get("content_hashes", {}).items()}
            logger.debug(f"Loaded {len(self._message_ids)} message IDs, {len(self._content_hashes)} content hashes from {self._path}")
        except Exception as e:
            logger.warning(f"Failed to load dedup cache from {self._path}: {e}")

    def _save(self) -> None:
        """Save dedup state to JSON file."""
        if not self._path:
            return

        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            data = {
                "message_ids": self._message_ids,
                "content_hashes": self._content_hashes,
                "saved_at": datetime.utcnow().isoformat(),
            }
            # Write atomically
            tmp_path = self._path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self._path)
        except Exception as e:
            logger.warning(f"Failed to save dedup cache to {self._path}: {e}")


# ── Global singleton ───────────────────────────────────────────

_dedup_store: Optional[DeduplicationStore] = None


def get_dedup_store() -> DeduplicationStore:
    """Get or create the global deduplication store."""
    global _dedup_store
    if _dedup_store is None:
        path = os.environ.get("DEDUP_CACHE_PATH", ".dedup_cache.json")
        max_age = int(os.environ.get("DEDUP_MAX_AGE_DAYS", "90"))
        _dedup_store = DeduplicationStore(path=path, max_age_days=max_age)
    return _dedup_store


def reset_dedup_store() -> None:
    """Reset the global store (for testing)."""
    global _dedup_store
    _dedup_store = None
