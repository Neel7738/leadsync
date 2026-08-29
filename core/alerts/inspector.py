"""
Webhook Payload Inspector

Captures and stores outgoing webhook payloads for debugging and inspection.
Provides a complete audit trail of what gets sent to each service.

Features:
- Captures payloads from all alert channels
- Stores request/response details
- Filtering by channel, status, time range
- In-memory ring buffer with configurable max size
- Serializable to JSON for export
"""

import copy
import json
import logging
import time
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("WebhookInspector")


class WebhookInspector:
    """
    Captures and inspects outgoing webhook payloads.

    Wraps channel send functions to record:
    - Outgoing payload (what gets sent)
    - Channel name and type
    - Timestamp
    - Success/failure status
    - Response details
    - Latency
    """

    def __init__(self, max_entries: int = 1000, enabled: bool = True):
        """
        Args:
            max_entries: Max payloads to keep in memory (ring buffer)
            enabled: Whether inspection is active
        """
        self._entries: List[Dict[str, Any]] = []
        self._max_entries = max_entries
        self._enabled = enabled
        self._lock = threading.Lock()
        self._total_captured = 0
        self._total_filtered = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool):
        self._enabled = value

    def wrap_channel(
        self,
        channel_fn: Callable,
        channel_name: Optional[str] = None,
        channel_type: Optional[str] = None,
    ) -> Callable:
        """
        Wrap a channel send function to capture payloads.

        Args:
            channel_fn: The original send function (e.g., telegram.send_breach_alert)
            channel_name: Name for this channel (default: function name)
            channel_type: Type identifier (e.g., "telegram", "slack", "email")

        Returns:
            Wrapped function that captures payloads
        """
        name = channel_name or getattr(channel_fn, "__name__", "unknown")
        ctype = channel_type or self._infer_channel_type(name)

        def wrapped(alert: Dict[str, Any]) -> bool:
            if not self._enabled:
                return channel_fn(alert)

            entry_id = self._total_captured + 1
            entry = {
                "id": entry_id,
                "channel": name,
                "channel_type": ctype,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "timestamp_unix": time.time(),
                "payload": self._serialize_payload(alert),
                "status": "pending",
                "success": None,
                "latency_ms": None,
                "response": None,
                "error": None,
            }

            start = time.time()
            try:
                success = channel_fn(alert)
                entry["success"] = bool(success)
                entry["status"] = "sent" if success else "failed"
                entry["latency_ms"] = round((time.time() - start) * 1000)
                if not success:
                    entry["error"] = "Channel returned False"
            except Exception as e:
                entry["success"] = False
                entry["status"] = "error"
                entry["error"] = str(e)
                entry["latency_ms"] = round((time.time() - start) * 1000)
                # Capture before re-raising
                with self._lock:
                    self._entries.append(entry)
                    self._total_captured += 1
                    if len(self._entries) > self._max_entries:
                        self._entries = self._entries[-self._max_entries:]
                raise  # Re-raise so AlertManager can handle retry

            with self._lock:
                self._entries.append(entry)
                self._total_captured += 1
                # Trim if over max
                if len(self._entries) > self._max_entries:
                    self._entries = self._entries[-self._max_entries:]

            return entry["success"]

        # Preserve function metadata
        wrapped.__name__ = name
        wrapped.__doc__ = channel_fn.__doc__
        wrapped._inspector_wrapped = True
        wrapped._original = channel_fn

        return wrapped

    def capture_manual(
        self,
        channel: str,
        payload: Any,
        channel_type: str = "manual",
        success: bool = True,
        latency_ms: int = 0,
        error: Optional[str] = None,
        response: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Manually capture a payload (for testing or external integrations).

        Returns the captured entry.
        """
        entry_id = self._total_captured + 1
        entry = {
            "id": entry_id,
            "channel": channel,
            "channel_type": channel_type,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "timestamp_unix": time.time(),
            "payload": self._serialize_payload(payload),
            "status": "sent" if success else "failed",
            "success": success,
            "latency_ms": latency_ms,
            "response": response,
            "error": error,
        }

        with self._lock:
            self._entries.append(entry)
            self._total_captured += 1
            if len(self._entries) > self._max_entries:
                self._entries = self._entries[-self._max_entries:]

        return entry

    def get_entries(
        self,
        channel: Optional[str] = None,
        channel_type: Optional[str] = None,
        success: Optional[bool] = None,
        since: Optional[float] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Get captured entries with optional filtering.

        Args:
            channel: Filter by channel name
            channel_type: Filter by channel type
            success: Filter by success status
            since: Unix timestamp - only entries after this time
            limit: Max entries to return

        Returns:
            List of captured entries (most recent first)
        """
        with self._lock:
            entries = list(self._entries)

        # Apply filters
        if channel:
            entries = [e for e in entries if e["channel"] == channel]
        if channel_type:
            entries = [e for e in entries if e["channel_type"] == channel_type]
        if success is not None:
            entries = [e for e in entries if e["success"] == success]
        if since:
            entries = [e for e in entries if e["timestamp_unix"] >= since]

        # Most recent first
        entries.reverse()

        return entries[:limit]

    def get_entry(self, entry_id: int) -> Optional[Dict[str, Any]]:
        """Get a single entry by ID."""
        with self._lock:
            for entry in self._entries:
                if entry["id"] == entry_id:
                    return copy.deepcopy(entry)
        return None

    def get_stats(self) -> Dict[str, Any]:
        """Get inspector statistics."""
        with self._lock:
            entries = list(self._entries)

        total = len(entries)
        sent = sum(1 for e in entries if e["success"] is True)
        failed = sum(1 for e in entries if e["success"] is False)
        errors = sum(1 for e in entries if e["status"] == "error")

        # By channel type
        by_type: Dict[str, Dict[str, int]] = {}
        for e in entries:
            ct = e["channel_type"]
            if ct not in by_type:
                by_type[ct] = {"total": 0, "sent": 0, "failed": 0}
            by_type[ct]["total"] += 1
            if e["success"]:
                by_type[ct]["sent"] += 1
            else:
                by_type[ct]["failed"] += 1

        # Latency stats
        latencies = [e["latency_ms"] for e in entries if e["latency_ms"] is not None]
        avg_latency = round(sum(latencies) / len(latencies)) if latencies else 0
        max_latency = max(latencies) if latencies else 0
        min_latency = min(latencies) if latencies else 0

        return {
            "total_captured": self._total_captured,
            "stored_entries": total,
            "max_entries": self._max_entries,
            "enabled": self._enabled,
            "sent": sent,
            "failed": failed,
            "errors": errors,
            "success_rate": round(sent / max(1, total) * 100, 1),
            "by_channel_type": by_type,
            "latency": {
                "avg_ms": avg_latency,
                "max_ms": max_latency,
                "min_ms": min_latency,
            },
        }

    def get_channel_list(self) -> List[Dict[str, Any]]:
        """Get list of all channels that have sent payloads."""
        with self._lock:
            entries = list(self._entries)

        channels: Dict[str, Dict[str, Any]] = {}
        for e in entries:
            ch = e["channel"]
            if ch not in channels:
                channels[ch] = {
                    "name": ch,
                    "type": e["channel_type"],
                    "total": 0,
                    "sent": 0,
                    "failed": 0,
                    "last_used": e["timestamp"],
                }
            channels[ch]["total"] += 1
            if e["success"]:
                channels[ch]["sent"] += 1
            else:
                channels[ch]["failed"] += 1

        return list(channels.values())

    def clear(self) -> int:
        """Clear all captured entries. Returns count cleared."""
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
        return count

    def export_json(self, limit: int = 1000) -> str:
        """Export entries as JSON string."""
        entries = self.get_entries(limit=limit)
        return json.dumps(entries, indent=2, default=str)

    def _serialize_payload(self, payload: Any) -> Any:
        """
        Serialize a payload for storage.

        Handles dicts, strings, and other types.
        Deep copies to prevent mutation.
        """
        if payload is None:
            return None
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            return copy.deepcopy(payload)
        if isinstance(payload, (list, tuple)):
            return [self._serialize_payload(item) for item in payload]
        return str(payload)

    def _infer_channel_type(self, name: str) -> str:
        """Infer channel type from function/channel name."""
        name_lower = name.lower()
        type_map = {
            "telegram": "telegram",
            "email": "email",
            "slack": "slack",
            "discord": "discord",
            "teams": "teams",
            "pagerduty": "pagerduty",
            "opsgenie": "opsgenie",
        }
        for keyword, ctype in type_map.items():
            if keyword in name_lower:
                return ctype
        return "unknown"


# ── Global Inspector Instance ─────────────────────────────────

_inspector: Optional[WebhookInspector] = None


def get_inspector() -> WebhookInspector:
    """Get the global webhook inspector instance."""
    global _inspector
    if _inspector is None:
        max_entries = int(__import__("os").environ.get("WEBHOOK_INSPECTOR_MAX_ENTRIES", "1000"))
        enabled = __import__("os").environ.get("WEBHOOK_INSPECTOR_ENABLED", "true").lower() == "true"
        _inspector = WebhookInspector(max_entries=max_entries, enabled=enabled)
    return _inspector


def wrap_alert_channels(alert_manager: Any) -> None:
    """
    Wrap all channels in an AlertManager with the inspector.

    This should be called after channels are registered.
    """
    inspector = get_inspector()
    wrapped_channels = []
    for channel in alert_manager._channels:
        if getattr(channel, "_inspector_wrapped", False):
            # Already wrapped
            wrapped_channels.append(channel)
        else:
            name = getattr(channel, "__name__", str(channel))
            wrapped = inspector.wrap_channel(channel, channel_name=name)
            wrapped_channels.append(wrapped)
    alert_manager._channels = wrapped_channels
