"""
Audit trail logging — records every significant system action.

Usage:
    from core.database.audit import audit

    audit.log("queue:added", entity_type="prospect", entity_id="c1",
              details={"priority_score": 0.8, "urgency": "high"})
    audit.log("email:sent", entity_type="draft", entity_id="d1",
              details={"to": "john@example.com"}, actor="rep:john")
    audit.log("sla:breach", entity_type="prospect", entity_id="c1",
              details={"times_requeued": 2})

Logs are buffered in memory and flushed to DB periodically or on
explicit flush. This avoids a DB write on every single operation.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Audit")


class AuditLogger:
    """
    Buffered audit logger that writes to the database.

    Events are buffered in memory and flushed to the database
    periodically (every 5 seconds) or when the buffer reaches 50 entries.
    This amortizes DB writes while keeping latency low.
    """

    def __init__(self, flush_interval: float = 5.0, max_buffer: int = 50):
        self._buffer: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._flush_interval = flush_interval
        self._max_buffer = max_buffer
        self._last_flush = time.time()
        self._total_logged = 0
        self._total_flushed = 0

    def log(
        self,
        action: str,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        actor: Optional[str] = "system",
        success: bool = True,
        error_message: Optional[str] = None,
    ) -> None:
        """
        Log an audit event.

        Args:
            action: Action type (e.g. "queue:added", "email:sent")
            entity_type: Type of entity affected (e.g. "prospect", "draft")
            entity_id: ID of the entity affected
            details: Free-form JSON details
            actor: Who performed the action (e.g. "system", "rep:john")
            success: Whether the action succeeded
            error_message: Error details if action failed
        """
        entry = {
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "details": details or {},
            "actor": actor,
            "success": success,
            "error_message": error_message,
            "timestamp": datetime.utcnow(),
        }

        with self._lock:
            self._buffer.append(entry)
            self._total_logged += 1

            # Flush if buffer is full or interval elapsed
            should_flush = (
                len(self._buffer) >= self._max_buffer
                or (time.time() - self._last_flush) >= self._flush_interval
            )

        if should_flush:
            self.flush()

    def flush(self) -> int:
        """Flush buffered entries to the database. Returns count flushed."""
        with self._lock:
            if not self._buffer:
                return 0
            entries = self._buffer[:]
            self._buffer.clear()
            self._last_flush = time.time()

        # Write to DB outside the lock
        count = 0
        try:
            from . import get_db
            from .models import AuditLog

            with get_db() as db:
                for entry in entries:
                    log = AuditLog(
                        action=entry["action"],
                        entity_type=entry.get("entity_type"),
                        entity_id=entry.get("entity_id"),
                        details=entry.get("details"),
                        actor=entry.get("actor"),
                        success=entry.get("success", True),
                        error_message=entry.get("error_message"),
                        timestamp=entry.get("timestamp", datetime.utcnow()),
                    )
                    db.add(log)
                    count += 1

            self._total_flushed += count
            logger.debug(f"Flushed {count} audit entries to database")

        except Exception as e:
            logger.error(f"Failed to flush audit log: {e}")
            # Put entries back in buffer for retry
            with self._lock:
                self._buffer = entries + self._buffer

        return count

    def query(
        self,
        action: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        since: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Query audit log entries.

        Args:
            action: Filter by action type (supports prefix matching)
            entity_type: Filter by entity type
            entity_id: Filter by entity ID
            since: Only entries after this datetime
            limit: Max entries to return

        Returns:
            List of audit log entries as dicts
        """
        try:
            from . import get_db
            from .models import AuditLog
            from sqlalchemy import desc

            with get_db() as db:
                query = db.query(AuditLog)

                if action:
                    query = query.filter(AuditLog.action.startswith(action))
                if entity_type:
                    query = query.filter(AuditLog.entity_type == entity_type)
                if entity_id:
                    query = query.filter(AuditLog.entity_id == entity_id)
                if since:
                    query = query.filter(AuditLog.timestamp >= since)

                entries = (
                    query.order_by(desc(AuditLog.timestamp))
                    .limit(limit)
                    .all()
                )

                return [
                    {
                        "id": e.id,
                        "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                        "action": e.action,
                        "entity_type": e.entity_type,
                        "entity_id": e.entity_id,
                        "details": e.details,
                        "actor": e.actor,
                        "success": e.success,
                        "error_message": e.error_message,
                    }
                    for e in entries
                ]

        except Exception as e:
            logger.error(f"Failed to query audit log: {e}")
            return []

    def get_stats(self) -> Dict[str, Any]:
        """Get audit logging statistics."""
        return {
            "buffered": len(self._buffer),
            "total_logged": self._total_logged,
            "total_flushed": self._total_flushed,
        }


# Global singleton
audit = AuditLogger()
